"""Operational hardening for the manual live pilot.

Pilot sessions are derived from immutable audit-log events. This keeps the
operator workflow auditable without introducing broker connectivity or hidden
mutable state.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from options_engine.live.pilot import (
    LiveFillEntry,
    LiveFillResult,
    LivePilotError,
    LivePilotGateDecision,
    LivePilotRuleViolationCode,
    LivePilotStatus,
    LiveRiskDashboard,
    build_live_risk_dashboard_from_database,
    record_live_fill,
)
from options_engine.storage.database import record_audit_event
from options_engine.storage.models import AuditEvent, AuditLog

RUNBOOK_VERSION = "live_pilot_runbook_v1"
SESSION_REVIEW_MIN_TRADES = 20
SESSION_REVIEW_MAX_TRADES = 30
SESSION_REVIEW_DAYS = 90


class PilotSessionStatus(StrEnum):
    """Derived pilot session state."""

    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"


class PilotSessionReasonCode(StrEnum):
    """Stable reason codes for pilot session operations."""

    ACTIVE_SESSION_EXISTS = "ACTIVE_SESSION_EXISTS"
    CONFIG_VERSION_MISMATCH = "CONFIG_VERSION_MISMATCH"
    CRITICAL_SYSTEM_ERROR = "CRITICAL_SYSTEM_ERROR"
    DAILY_SIGNOFF_INCOMPLETE = "DAILY_SIGNOFF_INCOMPLETE"
    EMERGENCY_SHUTDOWN_ACTIVE = "EMERGENCY_SHUTDOWN_ACTIVE"
    MULTIPLE_ACTIVE_SESSIONS = "MULTIPLE_ACTIVE_SESSIONS"
    NO_ACTIVE_SESSION = "NO_ACTIVE_SESSION"
    OPERATOR_STOP = "OPERATOR_STOP"
    PILOT_SESSION_ACTIVE = "PILOT_SESSION_ACTIVE"
    PILOT_SESSION_NOT_FOUND = "PILOT_SESSION_NOT_FOUND"
    PILOT_SESSION_STOPPED = "PILOT_SESSION_STOPPED"
    PILOT_TRADE_LIMIT_REACHED = "PILOT_TRADE_LIMIT_REACHED"
    RED_BLACK_STATE = "RED_BLACK_STATE"
    RESET_REVIEW_REQUIRED = "RESET_REVIEW_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RISK_RULE_VIOLATION = "RISK_RULE_VIOLATION"


class DailySignoffStatus(StrEnum):
    """Daily pilot signoff state."""

    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PilotSessionStartRequest:
    """Request to start one immutable pilot session."""

    pilot_id: str
    operator: str
    config_version: str
    started_at: datetime
    review_min_trades: int = SESSION_REVIEW_MIN_TRADES
    review_max_trades: int = SESSION_REVIEW_MAX_TRADES
    review_after_days: int = SESSION_REVIEW_DAYS

    def __post_init__(self) -> None:
        _require_text(self.pilot_id, "pilot_id")
        _require_text(self.operator, "operator")
        _require_text(self.config_version, "config_version")
        _require_aware_datetime(self.started_at, "started_at")
        if self.review_min_trades < 1:
            raise LivePilotError("review_min_trades must be positive")
        if self.review_max_trades < self.review_min_trades:
            raise LivePilotError("review_max_trades must be greater than or equal to review_min_trades")
        if self.review_after_days < 1:
            raise LivePilotError("review_after_days must be positive")


@dataclass(frozen=True, slots=True)
class PilotSession:
    """Derived immutable pilot session state."""

    pilot_id: str
    operator: str
    config_version: str
    started_at: datetime
    review_due_at: datetime
    review_min_trades: int
    review_max_trades: int
    status: PilotSessionStatus
    trade_count: int
    stopped_at: datetime | None = None
    stop_reason_code: str | None = None
    stop_reason: str | None = None
    last_review_note: str | None = None

    @property
    def active(self) -> bool:
        """Return true when the session currently allows gated operations."""
        return self.status == PilotSessionStatus.ACTIVE

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe session payload."""
        return {
            "pilot_id": self.pilot_id,
            "operator": self.operator,
            "config_version": self.config_version,
            "started_at": self.started_at.isoformat(),
            "review_due_at": self.review_due_at.isoformat(),
            "review_min_trades": self.review_min_trades,
            "review_max_trades": self.review_max_trades,
            "status": self.status.value,
            "trade_count": self.trade_count,
            "stopped_at": None if self.stopped_at is None else self.stopped_at.isoformat(),
            "stop_reason_code": self.stop_reason_code,
            "stop_reason": self.stop_reason,
            "last_review_note": self.last_review_note,
        }


@dataclass(frozen=True, slots=True)
class PilotSessionGateDecision:
    """Gate decision requiring exactly one active pilot session."""

    status: LivePilotStatus
    reason_codes: tuple[str, ...]
    message: str
    checked_at: datetime
    pilot_session: PilotSession | None = None

    @property
    def allow_operation(self) -> bool:
        """Return true when the operation may continue."""
        return self.status == LivePilotStatus.READY

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this gate decision to an audit event."""
        _require_text(config_version, "config_version")
        return AuditEvent(
            event_type=f"LIVE_PILOT_SESSION_GATE_{self.status.value}",
            entity_type="live_pilot_session",
            message=self.message,
            metadata={
                "status": self.status.value,
                "reason_codes": list(self.reason_codes),
                "allow_operation": self.allow_operation,
                "checked_at": self.checked_at.isoformat(),
                "pilot_id": None if self.pilot_session is None else self.pilot_session.pilot_id,
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.checked_at,
        )


@dataclass(frozen=True, slots=True)
class PilotSessionLiveFillResult:
    """Result of a session-gated live fill."""

    gate_decision: PilotSessionGateDecision
    fill_result: LiveFillResult
    session_fill_audit_id: int
    stop_audit_id: int | None = None


@dataclass(frozen=True, slots=True)
class DailyPilotSignoff:
    """End-of-day operator signoff."""

    pilot_id: str
    signoff_date: date
    operator: str
    signed_at: datetime
    report_reviewed: bool
    positions_reconciled: bool
    slippage_reviewed: bool
    violations_reviewed: bool
    notes: str

    def __post_init__(self) -> None:
        _require_text(self.pilot_id, "pilot_id")
        _require_text(self.operator, "operator")
        _require_aware_datetime(self.signed_at, "signed_at")
        _require_text(self.notes, "notes")

    def failed_reason_codes(self) -> tuple[str, ...]:
        """Return missing signoff fields as stable reason codes."""
        failed: list[str] = []
        if not self.report_reviewed:
            failed.append("REPORT_NOT_REVIEWED")
        if not self.positions_reconciled:
            failed.append("POSITIONS_NOT_RECONCILED")
        if not self.slippage_reviewed:
            failed.append("SLIPPAGE_NOT_REVIEWED")
        if not self.violations_reviewed:
            failed.append("VIOLATIONS_NOT_REVIEWED")
        return tuple(failed)


@dataclass(frozen=True, slots=True)
class DailyPilotSignoffResult:
    """Recorded daily signoff result."""

    status: DailySignoffStatus
    reason_codes: tuple[str, ...]
    audit_id: int

    @property
    def passed(self) -> bool:
        """Return true when signoff is complete."""
        return self.status == DailySignoffStatus.PASSED


@dataclass(frozen=True, slots=True)
class PilotEvidencePacket:
    """Exportable pilot evidence bundle."""

    pilot_session: PilotSession
    generated_at: datetime
    runbook_version: str
    audit_events: tuple[dict[str, object], ...]
    fills: tuple[dict[str, object], ...]
    slippage_events: tuple[dict[str, object], ...]
    rule_violations: tuple[dict[str, object], ...]
    daily_signoffs: tuple[dict[str, object], ...]
    dashboard_snapshots: tuple[dict[str, object], ...]

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe evidence packet."""
        return {
            "pilot_session": self.pilot_session.to_dict(),
            "generated_at": self.generated_at.isoformat(),
            "runbook_version": self.runbook_version,
            "audit_events": list(self.audit_events),
            "fills": list(self.fills),
            "slippage_events": list(self.slippage_events),
            "rule_violations": list(self.rule_violations),
            "daily_signoffs": list(self.daily_signoffs),
            "dashboard_snapshots": list(self.dashboard_snapshots),
            "broker_orders_submitted_by_system": False,
        }

    def write_json(self, output_path: Path) -> Path:
        """Write the evidence packet to JSON."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(self.to_json_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return output_path


def start_pilot_session(connection: sqlite3.Connection, request: PilotSessionStartRequest) -> PilotSession:
    """Start exactly one active manual live-pilot session."""
    active_sessions = _active_sessions(load_pilot_sessions(connection))
    if active_sessions:
        raise LivePilotError("cannot start pilot session while another session is active")

    review_due_at = request.started_at + timedelta(days=request.review_after_days)
    event = AuditEvent(
        event_type="LIVE_PILOT_SESSION_STARTED",
        entity_type="live_pilot_session",
        message="Manual live pilot session started",
        metadata={
            "pilot_id": request.pilot_id,
            "operator": request.operator.strip(),
            "config_version": request.config_version,
            "started_at": request.started_at.isoformat(),
            "review_due_at": review_due_at.isoformat(),
            "review_min_trades": request.review_min_trades,
            "review_max_trades": request.review_max_trades,
            "broker_order_submitted_by_system": False,
            "auto_execution": False,
        },
        config_version=request.config_version,
        created_at=request.started_at,
    )
    record_audit_event(connection, event)
    return PilotSession(
        pilot_id=request.pilot_id,
        operator=request.operator.strip(),
        config_version=request.config_version,
        started_at=request.started_at,
        review_due_at=review_due_at,
        review_min_trades=request.review_min_trades,
        review_max_trades=request.review_max_trades,
        status=PilotSessionStatus.ACTIVE,
        trade_count=0,
    )


def load_pilot_sessions(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
) -> tuple[PilotSession, ...]:
    """Derive pilot sessions from immutable audit-log events."""
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    rows = _query_audit_rows(connection, as_of=as_of)
    sessions: dict[str, dict[str, object]] = {}
    trade_counts: dict[str, int] = {}

    for audit_log in rows:
        metadata = _audit_metadata(audit_log.payload_json)
        pilot_id = metadata.get("pilot_id")
        if not isinstance(pilot_id, str) or not pilot_id:
            continue

        if audit_log.event_type == "LIVE_PILOT_SESSION_STARTED":
            sessions[pilot_id] = {
                "pilot_id": pilot_id,
                "operator": str(metadata["operator"]),
                "config_version": str(metadata["config_version"]),
                "started_at": _parse_datetime(str(metadata["started_at"])),
                "review_due_at": _parse_datetime(str(metadata["review_due_at"])),
                "review_min_trades": int(metadata["review_min_trades"]),
                "review_max_trades": int(metadata["review_max_trades"]),
                "status": PilotSessionStatus.ACTIVE,
                "stopped_at": None,
                "stop_reason_code": None,
                "stop_reason": None,
                "last_review_note": None,
            }
        elif audit_log.event_type == "LIVE_PILOT_SESSION_STOPPED" and pilot_id in sessions:
            sessions[pilot_id]["status"] = PilotSessionStatus.STOPPED
            sessions[pilot_id]["stopped_at"] = _parse_datetime(str(metadata["stopped_at"]))
            sessions[pilot_id]["stop_reason_code"] = str(metadata["reason_code"])
            sessions[pilot_id]["stop_reason"] = str(metadata["reason"])
        elif audit_log.event_type == "LIVE_PILOT_SESSION_RESUMED" and pilot_id in sessions:
            sessions[pilot_id]["status"] = PilotSessionStatus.ACTIVE
            sessions[pilot_id]["stopped_at"] = None
            sessions[pilot_id]["stop_reason_code"] = None
            sessions[pilot_id]["stop_reason"] = None
            sessions[pilot_id]["last_review_note"] = str(metadata["review_note"])
        elif audit_log.event_type == "LIVE_PILOT_SESSION_FILL_RECORDED":
            if metadata.get("dry_run") is True:
                continue
            trade_counts[pilot_id] = trade_counts.get(pilot_id, 0) + 1

    derived_sessions: list[PilotSession] = []
    for payload in sessions.values():
        pilot_id = str(payload["pilot_id"])
        derived_sessions.append(
            PilotSession(
                pilot_id=pilot_id,
                operator=str(payload["operator"]),
                config_version=str(payload["config_version"]),
                started_at=payload["started_at"],
                review_due_at=payload["review_due_at"],
                review_min_trades=int(payload["review_min_trades"]),
                review_max_trades=int(payload["review_max_trades"]),
                status=payload["status"],
                trade_count=trade_counts.get(pilot_id, 0),
                stopped_at=payload["stopped_at"],
                stop_reason_code=payload["stop_reason_code"],
                stop_reason=payload["stop_reason"],
                last_review_note=payload["last_review_note"],
            )
        )
    return tuple(sorted(derived_sessions, key=lambda session: session.started_at))


def require_active_pilot_session(
    connection: sqlite3.Connection,
    *,
    checked_at: datetime,
    config_version: str,
    pilot_id: str | None = None,
) -> PilotSessionGateDecision:
    """Require exactly one active pilot session before an operation continues."""
    _require_aware_datetime(checked_at, "checked_at")
    _require_text(config_version, "config_version")
    sessions = load_pilot_sessions(connection)
    active_sessions = _active_sessions(sessions)
    if not active_sessions:
        return PilotSessionGateDecision(
            status=LivePilotStatus.BLOCKED,
            reason_codes=(PilotSessionReasonCode.NO_ACTIVE_SESSION.value,),
            message="No active live pilot session is available",
            checked_at=checked_at,
        )
    if len(active_sessions) > 1:
        return PilotSessionGateDecision(
            status=LivePilotStatus.BLOCKED,
            reason_codes=(PilotSessionReasonCode.MULTIPLE_ACTIVE_SESSIONS.value,),
            message="Multiple active pilot sessions found; operator review required",
            checked_at=checked_at,
        )

    session = active_sessions[0]
    if pilot_id is not None and session.pilot_id != pilot_id:
        return PilotSessionGateDecision(
            status=LivePilotStatus.BLOCKED,
            reason_codes=(PilotSessionReasonCode.PILOT_SESSION_NOT_FOUND.value,),
            message="Requested pilot session is not the active session",
            checked_at=checked_at,
            pilot_session=session,
        )
    if session.config_version != config_version:
        return PilotSessionGateDecision(
            status=LivePilotStatus.BLOCKED,
            reason_codes=(PilotSessionReasonCode.CONFIG_VERSION_MISMATCH.value,),
            message="Active pilot session config does not match runtime config",
            checked_at=checked_at,
            pilot_session=session,
        )
    if checked_at >= session.review_due_at or session.trade_count >= session.review_min_trades:
        reason_code = (
            PilotSessionReasonCode.PILOT_TRADE_LIMIT_REACHED
            if session.trade_count >= session.review_max_trades
            else PilotSessionReasonCode.REVIEW_REQUIRED
        )
        status = LivePilotStatus.STOPPED if reason_code == PilotSessionReasonCode.PILOT_TRADE_LIMIT_REACHED else LivePilotStatus.REVIEW_REQUIRED
        return PilotSessionGateDecision(
            status=status,
            reason_codes=(reason_code.value,),
            message="Pilot session requires review before continuing",
            checked_at=checked_at,
            pilot_session=session,
        )

    return PilotSessionGateDecision(
        status=LivePilotStatus.READY,
        reason_codes=(PilotSessionReasonCode.PILOT_SESSION_ACTIVE.value,),
        message="Exactly one active pilot session is available",
        checked_at=checked_at,
        pilot_session=session,
    )


def stop_pilot_session(
    connection: sqlite3.Connection,
    *,
    pilot_id: str,
    stopped_at: datetime,
    reason_code: PilotSessionReasonCode | str,
    reason: str,
    config_version: str,
) -> int:
    """Stop a pilot session with an explicit audit reason."""
    _require_text(pilot_id, "pilot_id")
    _require_aware_datetime(stopped_at, "stopped_at")
    _require_text(reason, "reason")
    _require_text(config_version, "config_version")
    session = _session_by_id(load_pilot_sessions(connection), pilot_id)
    if session is None:
        raise LivePilotError("pilot session does not exist")
    if not session.active:
        raise LivePilotError("pilot session is already stopped")

    code = reason_code.value if isinstance(reason_code, PilotSessionReasonCode) else reason_code.strip().upper()
    event = AuditEvent(
        event_type="LIVE_PILOT_SESSION_STOPPED",
        entity_type="live_pilot_session",
        message="Manual live pilot session stopped",
        metadata={
            "pilot_id": pilot_id,
            "reason_code": code,
            "reason": reason.strip(),
            "stopped_at": stopped_at.isoformat(),
            "config_version": config_version,
        },
        config_version=config_version,
        created_at=stopped_at,
    )
    return record_audit_event(connection, event)


def record_pilot_reset_review(
    connection: sqlite3.Connection,
    *,
    pilot_id: str,
    reviewed_at: datetime,
    review_note: str,
    config_version: str,
) -> int:
    """Record explicit reset review required before resuming after hard stops."""
    _require_text(pilot_id, "pilot_id")
    _require_aware_datetime(reviewed_at, "reviewed_at")
    _require_text(review_note, "review_note")
    _require_text(config_version, "config_version")
    event = AuditEvent(
        event_type="LIVE_PILOT_RESET_REVIEW",
        entity_type="live_pilot_session",
        message="Pilot reset review recorded",
        metadata={
            "pilot_id": pilot_id,
            "review_note": review_note.strip(),
            "reviewed_at": reviewed_at.isoformat(),
            "config_version": config_version,
        },
        config_version=config_version,
        created_at=reviewed_at,
    )
    return record_audit_event(connection, event)


def resume_pilot_session(
    connection: sqlite3.Connection,
    *,
    pilot_id: str,
    resumed_at: datetime,
    review_note: str,
    config_version: str,
) -> int:
    """Resume a stopped pilot session after explicit review."""
    _require_text(review_note, "review_note")
    _require_aware_datetime(resumed_at, "resumed_at")
    _require_text(config_version, "config_version")
    sessions = load_pilot_sessions(connection)
    session = _session_by_id(sessions, pilot_id)
    if session is None:
        raise LivePilotError("pilot session does not exist")
    if session.active:
        raise LivePilotError("pilot session is already active")
    if _active_sessions(sessions):
        raise LivePilotError("another pilot session is already active")
    if session.stop_reason_code in _restricted_resume_reason_codes() and not _has_reset_review_after_stop(
        connection,
        pilot_id=pilot_id,
        stopped_at=session.stopped_at,
    ):
        raise LivePilotError("reset review is required before resuming this pilot session")

    event = AuditEvent(
        event_type="LIVE_PILOT_SESSION_RESUMED",
        entity_type="live_pilot_session",
        message="Manual live pilot session resumed after review",
        metadata={
            "pilot_id": pilot_id,
            "review_note": review_note.strip(),
            "resumed_at": resumed_at.isoformat(),
            "config_version": config_version,
        },
        config_version=config_version,
        created_at=resumed_at,
    )
    return record_audit_event(connection, event)


def record_live_fill_for_active_session(
    connection: sqlite3.Connection,
    entry: LiveFillEntry,
    *,
    config_version: str,
    pilot_id: str | None = None,
) -> PilotSessionLiveFillResult:
    """Record a manual fill only after the active pilot session gate passes."""
    gate = require_active_pilot_session(
        connection,
        checked_at=entry.filled_at,
        config_version=config_version,
        pilot_id=pilot_id,
    )
    record_audit_event(connection, gate.to_audit_event(config_version))
    if not gate.allow_operation or gate.pilot_session is None:
        raise LivePilotError(f"pilot session gate blocked live fill: {', '.join(gate.reason_codes)}")

    fill_result = record_live_fill(connection, entry, config_version=config_version)
    session_fill_audit_id = record_audit_event(
        connection,
        AuditEvent(
            event_type="LIVE_PILOT_SESSION_FILL_RECORDED",
            entity_type="live_pilot_session",
            message="Session-gated manual live fill recorded",
            metadata={
                "pilot_id": gate.pilot_session.pilot_id,
                "fill_id": fill_result.fill_id,
                "ticket_id": entry.ticket_id,
                "position_id": entry.position_id,
                "quantity": entry.quantity,
                "dry_run": entry.dry_run,
                "filled_at": entry.filled_at.isoformat(),
                "config_version": config_version,
                "broker_order_submitted_by_system": False,
            },
            config_version=config_version,
            created_at=entry.filled_at,
        ),
    )

    stop_audit_id: int | None = None
    stop_reasons = _stop_reasons_for_fill(fill_result)
    if stop_reasons:
        stop_audit_id = stop_pilot_session(
            connection,
            pilot_id=gate.pilot_session.pilot_id,
            stopped_at=entry.filled_at,
            reason_code=stop_reasons[0],
            reason="Live pilot stopped after session-gated fill violation",
            config_version=config_version,
        )

    return PilotSessionLiveFillResult(
        gate_decision=gate,
        fill_result=fill_result,
        session_fill_audit_id=session_fill_audit_id,
        stop_audit_id=stop_audit_id,
    )


def build_gated_live_risk_dashboard_from_database(
    connection: sqlite3.Connection,
    report_date: date,
    *,
    generated_at: datetime,
    config_version: str,
    pilot_id: str | None = None,
) -> LiveRiskDashboard:
    """Build a risk dashboard only after the active session gate passes."""
    gate = require_active_pilot_session(
        connection,
        checked_at=generated_at,
        config_version=config_version,
        pilot_id=pilot_id,
    )
    record_audit_event(connection, gate.to_audit_event(config_version))
    if not gate.allow_operation:
        raise LivePilotError(f"pilot session gate blocked dashboard: {', '.join(gate.reason_codes)}")
    return build_live_risk_dashboard_from_database(connection, report_date, generated_at=generated_at)


def record_daily_operator_signoff(
    connection: sqlite3.Connection,
    signoff: DailyPilotSignoff,
    *,
    config_version: str,
) -> DailyPilotSignoffResult:
    """Record the required daily pilot operator signoff."""
    _require_text(config_version, "config_version")
    if _session_by_id(load_pilot_sessions(connection), signoff.pilot_id) is None:
        raise LivePilotError("pilot session does not exist")

    reason_codes = signoff.failed_reason_codes()
    status = DailySignoffStatus.PASSED if not reason_codes else DailySignoffStatus.FAILED
    audit_id = record_audit_event(
        connection,
        AuditEvent(
            event_type=f"LIVE_PILOT_DAILY_SIGNOFF_{status.value}",
            entity_type="live_pilot_session",
            message="Daily live pilot signoff recorded",
            metadata={
                "pilot_id": signoff.pilot_id,
                "signoff_date": signoff.signoff_date.isoformat(),
                "operator": signoff.operator.strip(),
                "signed_at": signoff.signed_at.isoformat(),
                "report_reviewed": signoff.report_reviewed,
                "positions_reconciled": signoff.positions_reconciled,
                "slippage_reviewed": signoff.slippage_reviewed,
                "violations_reviewed": signoff.violations_reviewed,
                "notes": signoff.notes.strip(),
                "status": status.value,
                "reason_codes": list(reason_codes),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=signoff.signed_at,
        ),
    )
    return DailyPilotSignoffResult(status=status, reason_codes=reason_codes, audit_id=audit_id)


def build_pilot_evidence_packet(
    connection: sqlite3.Connection,
    *,
    pilot_id: str,
    generated_at: datetime,
) -> PilotEvidencePacket:
    """Build an exportable JSON evidence packet for one pilot session."""
    _require_aware_datetime(generated_at, "generated_at")
    session = _session_by_id(load_pilot_sessions(connection, as_of=generated_at), pilot_id)
    if session is None:
        raise LivePilotError("pilot session does not exist")

    audit_logs = _query_audit_rows(connection, as_of=generated_at)
    pilot_events = tuple(_audit_log_to_dict(log) for log in audit_logs if _event_belongs_to_pilot(log, pilot_id))
    fill_ids = _session_fill_ids(audit_logs, pilot_id)
    fills = tuple(_fill_rows(connection, fill_ids, as_of=generated_at))
    slippage_events = tuple(
        _audit_log_to_dict(log)
        for log in audit_logs
        if log.event_type == "LIVE_FILL_SLIPPAGE_RECORDED" and _metadata_fill_id(log) in fill_ids
    )
    rule_violations = tuple(
        _audit_log_to_dict(log)
        for log in audit_logs
        if log.event_type == "LIVE_PILOT_RULE_VIOLATION" and _event_belongs_to_pilot_or_session_dates(log, session)
    )
    daily_signoffs = tuple(
        _audit_log_to_dict(log)
        for log in audit_logs
        if log.event_type.startswith("LIVE_PILOT_DAILY_SIGNOFF_") and _event_belongs_to_pilot(log, pilot_id)
    )
    dashboard_snapshots = _dashboard_snapshots_for_fills(connection, session, fills, generated_at, audit_logs)
    return PilotEvidencePacket(
        pilot_session=session,
        generated_at=generated_at,
        runbook_version=RUNBOOK_VERSION,
        audit_events=pilot_events,
        fills=fills,
        slippage_events=slippage_events,
        rule_violations=rule_violations,
        daily_signoffs=daily_signoffs,
        dashboard_snapshots=dashboard_snapshots,
    )


def _active_sessions(sessions: tuple[PilotSession, ...]) -> tuple[PilotSession, ...]:
    return tuple(session for session in sessions if session.active)


def _session_by_id(sessions: tuple[PilotSession, ...], pilot_id: str) -> PilotSession | None:
    return next((session for session in sessions if session.pilot_id == pilot_id), None)


def _query_audit_rows(connection: sqlite3.Connection, *, as_of: datetime | None = None) -> tuple[AuditLog, ...]:
    if as_of is None:
        rows = connection.execute(
            """
            SELECT id, event_type, entity_type, message, payload_json, config_version, created_at
            FROM audit_log
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, event_type, entity_type, message, payload_json, config_version, created_at
            FROM audit_log
            WHERE created_at <= ?
            ORDER BY created_at ASC, id ASC
            """,
            (as_of.isoformat(),),
        ).fetchall()
    return tuple(
        AuditLog(
            id=row[0],
            event_type=row[1],
            entity_type=row[2],
            message=row[3],
            payload_json=row[4],
            config_version=row[5],
            created_at=_parse_datetime(row[6]),
        )
        for row in rows
    )


def _audit_metadata(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("metadata", payload)
    return metadata if isinstance(metadata, dict) else {}


def _audit_log_to_dict(audit_log: AuditLog) -> dict[str, object]:
    return {
        "id": audit_log.id,
        "event_type": audit_log.event_type,
        "entity_type": audit_log.entity_type,
        "message": audit_log.message,
        "metadata": _audit_metadata(audit_log.payload_json),
        "config_version": audit_log.config_version,
        "created_at": audit_log.created_at.isoformat(),
    }


def _event_belongs_to_pilot(audit_log: AuditLog, pilot_id: str) -> bool:
    return _audit_metadata(audit_log.payload_json).get("pilot_id") == pilot_id


def _event_belongs_to_pilot_or_session_dates(audit_log: AuditLog, session: PilotSession) -> bool:
    metadata = _audit_metadata(audit_log.payload_json)
    if metadata.get("pilot_id") == session.pilot_id:
        return True
    return audit_log.created_at >= session.started_at and (
        session.stopped_at is None or audit_log.created_at <= session.stopped_at
    )


def _metadata_fill_id(audit_log: AuditLog) -> int | None:
    fill_id = _audit_metadata(audit_log.payload_json).get("fill_id")
    return fill_id if isinstance(fill_id, int) else None


def _session_fill_ids(audit_logs: tuple[AuditLog, ...], pilot_id: str) -> tuple[int, ...]:
    fill_ids: list[int] = []
    for audit_log in audit_logs:
        if audit_log.event_type != "LIVE_PILOT_SESSION_FILL_RECORDED":
            continue
        metadata = _audit_metadata(audit_log.payload_json)
        if metadata.get("pilot_id") != pilot_id:
            continue
        fill_id = metadata.get("fill_id")
        if isinstance(fill_id, int):
            fill_ids.append(fill_id)
    return tuple(fill_ids)


def _fill_rows(
    connection: sqlite3.Connection,
    fill_ids: tuple[int, ...],
    *,
    as_of: datetime | None = None,
) -> list[dict[str, object]]:
    if not fill_ids:
        return []
    placeholders = ",".join("?" for _ in fill_ids)
    as_of_filter = "" if as_of is None else "AND filled_at <= ?"
    parameters: tuple[object, ...] = fill_ids if as_of is None else (*fill_ids, as_of.isoformat())
    rows = connection.execute(
        f"""
        SELECT id, ticket_id, position_id, filled_at, quantity, price, source, config_version, created_at
        FROM fills
        WHERE id IN ({placeholders})
          {as_of_filter}
        ORDER BY filled_at ASC, id ASC
        """,
        parameters,
    ).fetchall()
    return [
        {
            "id": row[0],
            "ticket_id": row[1],
            "position_id": row[2],
            "filled_at": row[3],
            "quantity": row[4],
            "price": row[5],
            "source": row[6],
            "config_version": row[7],
            "created_at": row[8],
        }
        for row in rows
    ]


def _dashboard_snapshots_for_fills(
    connection: sqlite3.Connection,
    session: PilotSession,
    fills: tuple[dict[str, object], ...],
    generated_at: datetime,
    audit_logs: tuple[AuditLog, ...],
) -> tuple[dict[str, object], ...]:
    dates = {session.started_at.date(), generated_at.date()}
    for fill in fills:
        filled_at = fill.get("filled_at")
        if isinstance(filled_at, str):
            dates.add(_parse_datetime(filled_at).date())
    snapshots: list[dict[str, object]] = []
    for snapshot_date in sorted(dates):
        dashboard = build_live_risk_dashboard_from_database(connection, snapshot_date, generated_at=generated_at)
        snapshots.append(
            {
                "date": snapshot_date.isoformat(),
                "emergency_shutdown_active": dashboard.emergency_shutdown_active,
                "live_fills_today": _count_audit_events_for_date(audit_logs, "LIVE_FILL_RECORDED", snapshot_date),
                "rule_violations_today": _count_audit_events_for_date(
                    audit_logs,
                    "LIVE_PILOT_RULE_VIOLATION",
                    snapshot_date,
                ),
                "account_equity": dashboard.daily_report.account_equity,
                "portfolio_heat": dashboard.daily_report.portfolio_heat,
                "open_positions": dashboard.daily_report.open_positions,
            }
        )
    return tuple(snapshots)


def _count_audit_events_for_date(audit_logs: tuple[AuditLog, ...], event_type: str, event_date: date) -> int:
    return sum(1 for audit_log in audit_logs if audit_log.event_type == event_type and audit_log.created_at.date() == event_date)


def _stop_reasons_for_fill(fill_result: LiveFillResult) -> tuple[PilotSessionReasonCode, ...]:
    reasons: list[PilotSessionReasonCode] = []
    for violation in fill_result.violations:
        if violation.code == LivePilotRuleViolationCode.RISK_RULE_VIOLATION:
            reasons.append(PilotSessionReasonCode.RISK_RULE_VIOLATION)
        elif violation.code == LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR:
            reasons.append(PilotSessionReasonCode.CRITICAL_SYSTEM_ERROR)
        elif violation.code == LivePilotRuleViolationCode.RED_BLACK_OVERRIDE_FORBIDDEN:
            reasons.append(PilotSessionReasonCode.RED_BLACK_STATE)
        elif violation.code == LivePilotRuleViolationCode.EMERGENCY_SHUTDOWN_ACTIVE:
            reasons.append(PilotSessionReasonCode.EMERGENCY_SHUTDOWN_ACTIVE)
    return tuple(dict.fromkeys(reasons))


def _restricted_resume_reason_codes() -> set[str]:
    return {
        PilotSessionReasonCode.RISK_RULE_VIOLATION.value,
        PilotSessionReasonCode.CRITICAL_SYSTEM_ERROR.value,
        PilotSessionReasonCode.RED_BLACK_STATE.value,
        PilotSessionReasonCode.EMERGENCY_SHUTDOWN_ACTIVE.value,
    }


def _has_reset_review_after_stop(
    connection: sqlite3.Connection,
    *,
    pilot_id: str,
    stopped_at: datetime | None,
) -> bool:
    if stopped_at is None:
        return False
    for audit_log in _query_audit_rows(connection):
        if audit_log.event_type != "LIVE_PILOT_RESET_REVIEW":
            continue
        metadata = _audit_metadata(audit_log.payload_json)
        if metadata.get("pilot_id") == pilot_id and audit_log.created_at >= stopped_at:
            return True
    return False


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise LivePilotError(f"{field_name} is required")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LivePilotError(f"{field_name} must be timezone-aware")


def _parse_datetime(raw_value: str) -> datetime:
    normalized = f"{raw_value[:-1]}+00:00" if raw_value.endswith("Z") else raw_value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
