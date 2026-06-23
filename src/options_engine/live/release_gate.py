"""Operator release gate for the one-lot manual live pilot.

The release gate is a read-only final check before a live pilot starts. It
validates local evidence, persistent audit state, and explicit operator
acknowledgements. It never submits broker orders.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from options_engine.live.operations import PilotSessionStatus, load_pilot_sessions
from options_engine.storage.database import connect_database
from options_engine.storage.models import AuditEvent


class ReleaseGateStatus(StrEnum):
    """Final release gate status."""

    GO = "GO"
    NO_GO = "NO_GO"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReleaseGateCheckStatus(StrEnum):
    """Individual release gate check status."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReleaseGateReasonCode(StrEnum):
    """Stable release gate reason codes."""

    ACCOUNT_EQUITY_MISSING = "ACCOUNT_EQUITY_MISSING"
    ACCOUNT_EQUITY_NOT_VERIFIED = "ACCOUNT_EQUITY_NOT_VERIFIED"
    BROKER_ORDER_SUBMISSION_DETECTED = "BROKER_ORDER_SUBMISSION_DETECTED"
    CONFIG_LOCK_MISSING = "CONFIG_LOCK_MISSING"
    DATABASE_MISSING = "DATABASE_MISSING"
    DATABASE_UNREADABLE = "DATABASE_UNREADABLE"
    EMERGENCY_SHUTDOWN_ACTIVE = "EMERGENCY_SHUTDOWN_ACTIVE"
    EVIDENCE_PACKET_INVALID = "EVIDENCE_PACKET_INVALID"
    EVIDENCE_PACKET_MISSING = "EVIDENCE_PACKET_MISSING"
    EVIDENCE_PACKET_STALE = "EVIDENCE_PACKET_STALE"
    FULL_TEST_SUITE_NOT_PASSED = "FULL_TEST_SUITE_NOT_PASSED"
    OPEN_POSITIONS_NOT_VERIFIED = "OPEN_POSITIONS_NOT_VERIFIED"
    PILOT_SESSION_INVALID = "PILOT_SESSION_INVALID"
    READINESS_CONFIG_MISMATCH = "READINESS_CONFIG_MISMATCH"
    READINESS_NOT_READY = "READINESS_NOT_READY"
    READINESS_REPORT_INVALID = "READINESS_REPORT_INVALID"
    READINESS_REPORT_MISSING = "READINESS_REPORT_MISSING"
    READINESS_REPORT_STALE = "READINESS_REPORT_STALE"
    RULE_VIOLATIONS_PRESENT = "RULE_VIOLATIONS_PRESENT"
    RUNBOOK_NOT_ACKNOWLEDGED = "RUNBOOK_NOT_ACKNOWLEDGED"


@dataclass(frozen=True, slots=True)
class ReleaseGateCheck:
    """One explicit release gate check result."""

    name: str
    status: ReleaseGateCheckStatus
    reason_code: str
    message: str

    @property
    def passed(self) -> bool:
        """Return true when the check passed."""
        return self.status == ReleaseGateCheckStatus.PASSED

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-safe check payload."""
        return {
            "name": self.name,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ReleaseGateInput:
    """Inputs for evaluating the pilot release gate."""

    database_path: Path
    readiness_report_path: Path
    evidence_packet_path: Path
    generated_at: datetime
    full_test_suite_passed: bool
    runbook_acknowledged: bool
    account_equity_present: bool
    open_positions_verified: bool
    max_evidence_age_days: int = 7

    def __post_init__(self) -> None:
        _require_aware_datetime(self.generated_at, "generated_at")
        if self.max_evidence_age_days < 0:
            raise ValueError("max_evidence_age_days must be non-negative")


@dataclass(frozen=True, slots=True)
class ReleaseGateReport:
    """Final release gate report."""

    status: ReleaseGateStatus
    checks: tuple[ReleaseGateCheck, ...]
    generated_at: datetime
    database_path: Path
    readiness_report_path: Path
    evidence_packet_path: Path
    config_version: str

    @property
    def ready(self) -> bool:
        """Return true only when the final status is GO."""
        return self.status == ReleaseGateStatus.GO

    @property
    def blocking_reason_codes(self) -> tuple[str, ...]:
        """Return failed check reason codes."""
        return tuple(check.reason_code for check in self.checks if check.status == ReleaseGateCheckStatus.FAILED)

    @property
    def review_reason_codes(self) -> tuple[str, ...]:
        """Return review-required reason codes."""
        return tuple(
            check.reason_code for check in self.checks if check.status == ReleaseGateCheckStatus.REVIEW_REQUIRED
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe release gate payload."""
        return {
            "status": self.status.value,
            "ready": self.ready,
            "generated_at": self.generated_at.isoformat(),
            "database_path": str(self.database_path),
            "readiness_report_path": str(self.readiness_report_path),
            "evidence_packet_path": str(self.evidence_packet_path),
            "config_version": self.config_version,
            "blocking_reason_codes": list(self.blocking_reason_codes),
            "review_reason_codes": list(self.review_reason_codes),
            "checks": [check.to_dict() for check in self.checks],
            "broker_orders_submitted_by_system": False,
        }

    def to_markdown(self) -> str:
        """Render the release gate report as Markdown."""
        lines = [
            f"# Pilot Release Gate - {self.status.value}",
            "",
            f"- Generated at: {self.generated_at.isoformat()}",
            f"- Ready: {self.ready}",
            f"- Database: {self.database_path}",
            f"- Readiness report: {self.readiness_report_path}",
            f"- Evidence packet: {self.evidence_packet_path}",
            f"- Config version: {self.config_version}",
            "- Broker orders submitted by system: False",
            "",
            "## Checks",
        ]
        for check in self.checks:
            lines.append(f"- {check.status.value} {check.reason_code}: {check.message}")
        return "\n".join(lines)

    def to_audit_event(self) -> AuditEvent:
        """Convert the release gate report to a durable audit event."""
        return AuditEvent(
            event_type=f"PILOT_RELEASE_GATE_{self.status.value}",
            entity_type="pilot_release_gate",
            message=f"Pilot release gate evaluated: {self.status.value}",
            metadata={
                "status": self.status.value,
                "ready": self.ready,
                "generated_at": self.generated_at.isoformat(),
                "database_path": str(self.database_path),
                "readiness_report_path": str(self.readiness_report_path),
                "evidence_packet_path": str(self.evidence_packet_path),
                "config_version": self.config_version,
                "blocking_reason_codes": list(self.blocking_reason_codes),
                "review_reason_codes": list(self.review_reason_codes),
                "checks": [check.to_dict() for check in self.checks],
                "broker_orders_submitted_by_system": False,
            },
            config_version=self.config_version,
            created_at=self.generated_at,
        )

    def write_json(self, output_path: Path) -> Path:
        """Write the release gate report to JSON."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(self.to_json_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return output_path

    def write_markdown(self, output_path: Path) -> Path:
        """Write the release gate report to Markdown."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.to_markdown(), encoding="utf-8")
        return output_path


def evaluate_pilot_release_gate(gate_input: ReleaseGateInput) -> ReleaseGateReport:
    """Evaluate the final manual live-pilot release gate."""
    checks: list[ReleaseGateCheck] = [
        _boolean_check(
            name="Full Test Suite",
            passed=gate_input.full_test_suite_passed,
            reason_code=ReleaseGateReasonCode.FULL_TEST_SUITE_NOT_PASSED,
            pass_message="Full test suite was marked passed by the operator",
            fail_message="Full test suite has not been marked passed",
        ),
        _review_check(
            name="Runbook Acknowledgement",
            passed=gate_input.runbook_acknowledged,
            reason_code=ReleaseGateReasonCode.RUNBOOK_NOT_ACKNOWLEDGED,
            pass_message="Operator acknowledged the live pilot runbook",
            review_message="Operator has not acknowledged the live pilot runbook",
        ),
        _boolean_check(
            name="Account Equity",
            passed=gate_input.account_equity_present,
            reason_code=ReleaseGateReasonCode.ACCOUNT_EQUITY_MISSING,
            pass_message="Operator confirmed account equity is present",
            fail_message="Operator has not confirmed account equity is present",
        ),
        _boolean_check(
            name="Open Positions",
            passed=gate_input.open_positions_verified,
            reason_code=ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            pass_message="Operator confirmed open positions are verified",
            fail_message="Operator has not confirmed open positions are verified",
        ),
    ]

    readiness_payload, readiness_checks = _load_readiness_report(
        gate_input.readiness_report_path,
        generated_at=gate_input.generated_at,
        max_age_days=gate_input.max_evidence_age_days,
    )
    checks.extend(readiness_checks)
    evidence_payload, evidence_checks = _load_evidence_packet(
        gate_input.evidence_packet_path,
        generated_at=gate_input.generated_at,
        max_age_days=gate_input.max_evidence_age_days,
    )
    checks.extend(evidence_checks)
    checks.extend(_readiness_evidence_checks(readiness_payload, evidence_payload))
    checks.extend(
        _database_checks(
            gate_input.database_path,
            evidence_payload,
            gate_input.generated_at,
            gate_input.max_evidence_age_days,
        )
    )

    if readiness_payload is not None:
        checks.extend(_broker_submission_checks("Readiness Report", readiness_payload))
    if evidence_payload is not None:
        checks.extend(_broker_submission_checks("Evidence Packet", evidence_payload))

    status = _overall_status(tuple(checks))
    return ReleaseGateReport(
        status=status,
        checks=tuple(checks),
        generated_at=gate_input.generated_at,
        database_path=gate_input.database_path,
        readiness_report_path=gate_input.readiness_report_path,
        evidence_packet_path=gate_input.evidence_packet_path,
        config_version=_evidence_config_version(evidence_payload) or "UNKNOWN",
    )


def _load_readiness_report(
    path: Path,
    *,
    generated_at: datetime,
    max_age_days: int,
) -> tuple[dict[str, Any] | None, tuple[ReleaseGateCheck, ...]]:
    if not path.exists():
        return (
            None,
            (
                _failed(
                    "Readiness Report",
                    ReleaseGateReasonCode.READINESS_REPORT_MISSING,
                    f"Readiness report does not exist: {path}",
                ),
            ),
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            None,
            (
                _failed(
                    "Readiness Report",
                    ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                    f"Readiness report is not valid JSON: {exc}",
                ),
            ),
        )
    if not isinstance(payload, dict):
        return (
            None,
            (
                _failed(
                    "Readiness Report",
                    ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                    "Readiness report must be a JSON object",
                ),
            ),
        )

    checks: list[ReleaseGateCheck] = []
    ready = payload.get("ready")
    if ready is not True:
        checks.append(
            _failed(
                "Readiness Report",
                ReleaseGateReasonCode.READINESS_NOT_READY,
                "Readiness dry run is not marked ready",
            )
        )
    else:
        checks.append(_passed("Readiness Report", "Readiness dry run is marked ready"))

    run_at_raw = payload.get("run_at")
    if not isinstance(run_at_raw, str):
        checks.append(
            _failed(
                "Readiness Timestamp",
                ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                "Readiness report is missing run_at",
            )
        )
    else:
        try:
            run_at = _parse_datetime(run_at_raw)
        except ValueError:
            checks.append(
                _failed(
                    "Readiness Timestamp",
                    ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                    "Readiness report run_at is not a valid timestamp",
                )
            )
        else:
            max_age = timedelta(days=max_age_days)
            age = generated_at.astimezone(UTC) - run_at.astimezone(UTC)
            if age < timedelta(0):
                checks.append(
                    _failed(
                        "Readiness Timestamp",
                        ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                        "Readiness report run_at is after release gate timestamp",
                    )
                )
            elif age > max_age:
                checks.append(
                    _failed(
                        "Readiness Timestamp",
                        ReleaseGateReasonCode.READINESS_REPORT_STALE,
                        f"Readiness report is older than {max_age_days} days",
                    )
                )
            else:
                checks.append(_passed("Readiness Timestamp", "Readiness report timestamp is current"))

    return (payload, tuple(checks))


def _load_evidence_packet(
    path: Path,
    *,
    generated_at: datetime,
    max_age_days: int,
) -> tuple[dict[str, Any] | None, tuple[ReleaseGateCheck, ...]]:
    if not path.exists():
        return (
            None,
            (
                _failed(
                    "Evidence Packet",
                    ReleaseGateReasonCode.EVIDENCE_PACKET_MISSING,
                    f"Evidence packet does not exist: {path}",
                ),
            ),
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            None,
            (
                _failed(
                    "Evidence Packet",
                    ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                    f"Evidence packet is not valid JSON: {exc}",
                ),
            ),
        )
    if not isinstance(payload, dict):
        return (
            None,
            (
                _failed(
                    "Evidence Packet",
                    ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                    "Evidence packet must be a JSON object",
                ),
            ),
        )

    checks: list[ReleaseGateCheck] = []
    generated_raw = payload.get("generated_at")
    if not isinstance(generated_raw, str):
        checks.append(
            _failed(
                "Evidence Packet",
                ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                "Evidence packet is missing generated_at",
            )
        )
    else:
        try:
            evidence_generated_at = _parse_datetime(generated_raw)
        except ValueError:
            checks.append(
                _failed(
                    "Evidence Packet",
                    ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                    "Evidence packet generated_at is not a valid timestamp",
                )
            )
        else:
            max_age = timedelta(days=max_age_days)
            age = generated_at.astimezone(UTC) - evidence_generated_at.astimezone(UTC)
            if age < timedelta(0):
                checks.append(
                    _failed(
                        "Evidence Packet",
                        ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                        "Evidence packet generated_at is after release gate timestamp",
                    )
                )
            elif age > max_age:
                checks.append(
                    _failed(
                        "Evidence Packet",
                        ReleaseGateReasonCode.EVIDENCE_PACKET_STALE,
                        f"Evidence packet is older than {max_age_days} days",
                    )
                )
            else:
                checks.append(_passed("Evidence Packet", "Evidence packet timestamp is current"))

    session = payload.get("pilot_session")
    if not isinstance(session, dict):
        checks.append(
            _failed(
                "Evidence Packet",
                ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                "Evidence packet is missing pilot_session",
            )
        )
    elif session.get("status") != PilotSessionStatus.ACTIVE.value:
        checks.append(
            _failed(
                "Evidence Packet",
                ReleaseGateReasonCode.PILOT_SESSION_INVALID,
                "Evidence packet pilot session is not ACTIVE",
            )
        )
    else:
        checks.append(_passed("Evidence Session", "Evidence packet pilot session is ACTIVE"))

    fills = payload.get("fills")
    if not isinstance(fills, list):
        checks.append(
            _failed(
                "Evidence Packet",
                ReleaseGateReasonCode.EVIDENCE_PACKET_INVALID,
                "Evidence packet fills must be a list",
            )
        )
    else:
        checks.append(_passed("Evidence Fills", "Evidence packet fills are present"))

    return (payload, tuple(checks))


def _database_checks(
    path: Path,
    evidence_payload: dict[str, Any] | None,
    generated_at: datetime,
    max_age_days: int,
) -> tuple[ReleaseGateCheck, ...]:
    if not path.exists():
        return (
            _failed(
                "Database",
                ReleaseGateReasonCode.DATABASE_MISSING,
                f"Database does not exist: {path}",
            ),
        )

    try:
        with connect_database(path) as connection:
            return (
                _config_lock_check(connection, evidence_payload, generated_at),
                _emergency_shutdown_check(connection, generated_at),
                _rule_violation_check(connection, generated_at),
                _pilot_session_check(connection, evidence_payload, generated_at),
                _account_equity_check(connection, evidence_payload, generated_at),
                _position_reconciliation_check(connection, evidence_payload, generated_at, max_age_days),
            )
    except sqlite3.Error as exc:
        return (
            _failed(
                "Database",
                ReleaseGateReasonCode.DATABASE_UNREADABLE,
                f"Database could not be read: {exc}",
            ),
        )


def _config_lock_check(
    connection: sqlite3.Connection,
    evidence_payload: dict[str, Any] | None,
    generated_at: datetime,
) -> ReleaseGateCheck:
    config_version = _evidence_config_version(evidence_payload)
    row = connection.execute(
        """
        SELECT payload_json, created_at
        FROM audit_log
        WHERE event_type IN ('LIVE_PILOT_CONFIG_LOCKED', 'LIVE_PILOT_CONFIG_UNLOCKED')
          AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (generated_at.isoformat(),),
    ).fetchone()
    if row is None:
        return _failed(
            "Config Lock",
            ReleaseGateReasonCode.CONFIG_LOCK_MISSING,
            "No config lock audit event found at or before release gate timestamp",
        )

    metadata = _audit_metadata(row[0])
    if metadata.get("locked") is not True:
        return _failed("Config Lock", ReleaseGateReasonCode.CONFIG_LOCK_MISSING, "Latest config audit event is unlocked")
    lock_timestamp = metadata.get("locked_at")
    if not isinstance(lock_timestamp, str):
        return _failed("Config Lock", ReleaseGateReasonCode.CONFIG_LOCK_MISSING, "Config lock is missing locked_at")
    try:
        locked_at = _parse_datetime(lock_timestamp)
    except ValueError:
        return _failed("Config Lock", ReleaseGateReasonCode.CONFIG_LOCK_MISSING, "Config lock locked_at is malformed")
    if locked_at.astimezone(UTC) > generated_at.astimezone(UTC):
        return _failed(
            "Config Lock",
            ReleaseGateReasonCode.CONFIG_LOCK_MISSING,
            "Config lock is after release gate timestamp",
        )
    locked_version = metadata.get("config_version")
    if config_version is not None and locked_version != config_version:
        return _failed(
            "Config Lock",
            ReleaseGateReasonCode.CONFIG_LOCK_MISSING,
            "Locked config version does not match evidence packet",
        )
    return _passed("Config Lock", "Latest config audit event is locked")


def _emergency_shutdown_check(connection: sqlite3.Connection, generated_at: datetime) -> ReleaseGateCheck:
    row = connection.execute(
        """
        SELECT event_type, payload_json, created_at
        FROM audit_log
        WHERE event_type IN (
            'LIVE_PILOT_EMERGENCY_SHUTDOWN_ACTIVATED',
            'LIVE_PILOT_EMERGENCY_SHUTDOWN_CLEARED'
        )
          AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (generated_at.isoformat(),),
    ).fetchone()
    if row is None:
        return _passed("Emergency Shutdown", "No emergency shutdown flag was active at release gate timestamp")

    metadata = _audit_metadata(row[1])
    event_type = str(row[0])
    if event_type == "LIVE_PILOT_EMERGENCY_SHUTDOWN_ACTIVATED":
        return _failed(
            "Emergency Shutdown",
            ReleaseGateReasonCode.EMERGENCY_SHUTDOWN_ACTIVE,
            f"Emergency shutdown is active: {metadata.get('reason_code', 'UNKNOWN')}",
        )
    return _passed("Emergency Shutdown", "Emergency shutdown was clear at release gate timestamp")


def _rule_violation_check(connection: sqlite3.Connection, generated_at: datetime) -> ReleaseGateCheck:
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM audit_log
        WHERE event_type = 'LIVE_PILOT_RULE_VIOLATION'
          AND created_at <= ?
        """,
        (generated_at.isoformat(),),
    ).fetchone()
    count = int(row[0])
    if count > 0:
        return _failed(
            "Rule Violations",
            ReleaseGateReasonCode.RULE_VIOLATIONS_PRESENT,
            f"{count} live-pilot rule violation event(s) found",
        )
    return _passed("Rule Violations", "No live-pilot rule violations found")


def _pilot_session_check(
    connection: sqlite3.Connection,
    evidence_payload: dict[str, Any] | None,
    generated_at: datetime,
) -> ReleaseGateCheck:
    sessions = load_pilot_sessions(connection, as_of=generated_at)
    active_sessions = tuple(session for session in sessions if session.active)
    if len(active_sessions) != 1:
        return _failed(
            "Pilot Session",
            ReleaseGateReasonCode.PILOT_SESSION_INVALID,
            f"Expected exactly one active pilot session, found {len(active_sessions)}",
        )

    evidence_session = evidence_payload.get("pilot_session") if evidence_payload is not None else None
    if isinstance(evidence_session, dict):
        evidence_pilot_id = evidence_session.get("pilot_id")
        evidence_config_version = evidence_session.get("config_version")
        session = active_sessions[0]
        if session.pilot_id != evidence_pilot_id or session.config_version != evidence_config_version:
            return _failed(
                "Pilot Session",
                ReleaseGateReasonCode.PILOT_SESSION_INVALID,
                "Active database session does not match evidence packet",
            )

    return _passed("Pilot Session", "Exactly one active pilot session matches evidence")


def _account_equity_check(
    connection: sqlite3.Connection,
    evidence_payload: dict[str, Any] | None,
    generated_at: datetime,
) -> ReleaseGateCheck:
    config_version = _evidence_config_version(evidence_payload)
    row = connection.execute(
        """
        SELECT account_equity, as_of, config_version, created_at
        FROM risk_snapshots
        WHERE as_of <= ?
          AND created_at <= ?
        ORDER BY as_of DESC, id DESC
        LIMIT 1
        """,
        (generated_at.isoformat(), generated_at.isoformat()),
    ).fetchone()
    if row is None:
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_MISSING,
            "No risk snapshot found at or before release gate timestamp",
        )
    try:
        account_equity = Decimal(str(row[0]))
    except (InvalidOperation, ValueError):
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_MISSING,
            "Latest risk snapshot account equity is malformed",
        )
    try:
        snapshot_as_of = _parse_datetime(str(row[1]))
    except ValueError:
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED,
            "Latest risk snapshot timestamp is malformed",
        )
    if snapshot_as_of.tzinfo is None or snapshot_as_of.utcoffset() is None:
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED,
            "Latest risk snapshot timestamp is timezone-naive",
        )
    if snapshot_as_of.astimezone(UTC) > generated_at.astimezone(UTC):
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED,
            "Latest risk snapshot is after the release gate timestamp",
        )
    if config_version is not None and row[2] != config_version:
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED,
            "Latest risk snapshot config version does not match evidence packet",
        )
    if account_equity <= Decimal("0"):
        return _failed(
            "Account Equity",
            ReleaseGateReasonCode.ACCOUNT_EQUITY_MISSING,
            "Latest risk snapshot account equity is not positive",
        )
    return _passed("Account Equity", "Latest risk snapshot has positive account equity")


def _position_reconciliation_check(
    connection: sqlite3.Connection,
    evidence_payload: dict[str, Any] | None,
    generated_at: datetime,
    max_age_days: int,
) -> ReleaseGateCheck:
    config_version = _evidence_config_version(evidence_payload)
    row = connection.execute(
        """
        SELECT event_type, payload_json
        FROM audit_log
        WHERE event_type IN ('POSITION_RECONCILIATION_VERIFIED', 'POSITION_RECONCILIATION_UNVERIFIED')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "No position reconciliation audit event found",
        )

    event_type = str(row[0])
    metadata = _audit_metadata(str(row[1]))
    if event_type != "POSITION_RECONCILIATION_VERIFIED":
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Latest position reconciliation is not verified",
        )
    if metadata.get("open_risk_verified") is not True:
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Latest position reconciliation did not verify open risk",
        )
    if config_version is not None and metadata.get("config_version") != config_version:
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Position reconciliation config version does not match evidence packet",
        )
    checked_at = metadata.get("checked_at")
    if not isinstance(checked_at, str):
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Position reconciliation is missing checked_at",
        )
    try:
        reconciliation_checked_at = _parse_datetime(checked_at)
    except ValueError:
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Position reconciliation checked_at is malformed",
        )

    release_age = generated_at.astimezone(UTC) - reconciliation_checked_at.astimezone(UTC)
    if release_age < timedelta(0):
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Position reconciliation is after the release gate timestamp",
        )
    if release_age > timedelta(days=max_age_days):
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            f"Position reconciliation is older than {max_age_days} days",
        )

    evidence_generated_at = _evidence_generated_at(evidence_payload)
    if evidence_generated_at is not None and reconciliation_checked_at.astimezone(UTC) > evidence_generated_at.astimezone(UTC):
        return _failed(
            "Position Reconciliation",
            ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED,
            "Position reconciliation is after evidence packet generated_at",
        )
    return _passed("Position Reconciliation", "Latest position reconciliation verified open risk")


def _readiness_evidence_checks(
    readiness_payload: dict[str, Any] | None,
    evidence_payload: dict[str, Any] | None,
) -> tuple[ReleaseGateCheck, ...]:
    if readiness_payload is None or evidence_payload is None:
        return ()

    checks: list[ReleaseGateCheck] = []
    readiness_config_version = _readiness_config_version(readiness_payload)
    evidence_config_version = _evidence_config_version(evidence_payload)
    if readiness_config_version is None:
        checks.append(
            _failed(
                "Readiness Evidence Coupling",
                ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                "Readiness report is missing config_version",
            )
        )
    elif evidence_config_version is not None and readiness_config_version != evidence_config_version:
        checks.append(
            _failed(
                "Readiness Evidence Coupling",
                ReleaseGateReasonCode.READINESS_CONFIG_MISMATCH,
                "Readiness report config version does not match evidence packet",
            )
        )
    elif evidence_config_version is not None:
        checks.append(
            _passed(
                "Readiness Evidence Coupling",
                "Readiness report config version matches evidence packet",
            )
        )

    try:
        readiness_run_at = _parse_datetime(str(readiness_payload.get("run_at")))
        evidence_generated_at = _parse_datetime(str(evidence_payload.get("generated_at")))
    except ValueError:
        return tuple(checks)

    if readiness_run_at.astimezone(UTC) > evidence_generated_at.astimezone(UTC):
        checks.append(
            _failed(
                "Readiness Evidence Timing",
                ReleaseGateReasonCode.READINESS_REPORT_INVALID,
                "Readiness report run_at is after evidence packet generated_at",
            )
        )
    else:
        checks.append(_passed("Readiness Evidence Timing", "Readiness report is not after evidence packet"))

    return tuple(checks)


def _broker_submission_checks(name: str, payload: dict[str, Any]) -> tuple[ReleaseGateCheck, ...]:
    if _contains_forbidden_execution_flag(payload):
        return (
            _failed(
                name,
                ReleaseGateReasonCode.BROKER_ORDER_SUBMISSION_DETECTED,
                "Artifact contains broker execution or market-order flag set true",
            ),
        )
    return (_passed(name, "Artifact contains no broker execution flags set true"),)


def _contains_forbidden_execution_flag(value: object) -> bool:
    forbidden_false_keys = {
        "auto_execution",
        "broker_order_submitted",
        "broker_order_submitted_by_system",
        "broker_orders_submitted",
        "broker_orders_submitted_by_system",
        "market_order_allowed",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in forbidden_false_keys and nested is not False:
                return True
            if _contains_forbidden_execution_flag(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_execution_flag(item) for item in value)
    return False


def _boolean_check(
    *,
    name: str,
    passed: bool,
    reason_code: ReleaseGateReasonCode,
    pass_message: str,
    fail_message: str,
) -> ReleaseGateCheck:
    if passed:
        return _passed(name, pass_message)
    return _failed(name, reason_code, fail_message)


def _review_check(
    *,
    name: str,
    passed: bool,
    reason_code: ReleaseGateReasonCode,
    pass_message: str,
    review_message: str,
) -> ReleaseGateCheck:
    if passed:
        return _passed(name, pass_message)
    return ReleaseGateCheck(
        name=name,
        status=ReleaseGateCheckStatus.REVIEW_REQUIRED,
        reason_code=reason_code.value,
        message=review_message,
    )


def _passed(name: str, message: str) -> ReleaseGateCheck:
    return ReleaseGateCheck(
        name=name,
        status=ReleaseGateCheckStatus.PASSED,
        reason_code="OK",
        message=message,
    )


def _failed(name: str, reason_code: ReleaseGateReasonCode, message: str) -> ReleaseGateCheck:
    return ReleaseGateCheck(
        name=name,
        status=ReleaseGateCheckStatus.FAILED,
        reason_code=reason_code.value,
        message=message,
    )


def _overall_status(checks: tuple[ReleaseGateCheck, ...]) -> ReleaseGateStatus:
    if any(check.status == ReleaseGateCheckStatus.FAILED for check in checks):
        return ReleaseGateStatus.NO_GO
    if any(check.status == ReleaseGateCheckStatus.REVIEW_REQUIRED for check in checks):
        return ReleaseGateStatus.REVIEW_REQUIRED
    return ReleaseGateStatus.GO


def _evidence_config_version(evidence_payload: dict[str, Any] | None) -> str | None:
    if evidence_payload is None:
        return None
    session = evidence_payload.get("pilot_session")
    if not isinstance(session, dict):
        return None
    config_version = session.get("config_version")
    return config_version if isinstance(config_version, str) and config_version else None


def _evidence_generated_at(evidence_payload: dict[str, Any] | None) -> datetime | None:
    if evidence_payload is None:
        return None
    generated_at = evidence_payload.get("generated_at")
    if not isinstance(generated_at, str):
        return None
    try:
        return _parse_datetime(generated_at)
    except ValueError:
        return None


def _readiness_config_version(readiness_payload: dict[str, Any] | None) -> str | None:
    if readiness_payload is None:
        return None
    config_version = readiness_payload.get("config_version")
    return config_version if isinstance(config_version, str) and config_version else None


def _audit_metadata(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("metadata", payload)
    return metadata if isinstance(metadata, dict) else {}


def _parse_datetime(raw_value: str) -> datetime:
    normalized_value = f"{raw_value[:-1]}+00:00" if raw_value.endswith("Z") else raw_value
    parsed = datetime.fromisoformat(normalized_value)
    _require_aware_datetime(parsed, "timestamp")
    return parsed


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
