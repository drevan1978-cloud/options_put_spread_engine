"""Manual live-pilot guardrails and observation utilities.

The live pilot layer records manual activity and blocks unsafe pilot states.
It never submits broker orders and never creates market orders.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from options_engine.reporting import DailyReport, build_daily_report_from_database
from options_engine.reporting.daily_report import MISSING
from options_engine.risk.kill_switch import KillSwitchState
from options_engine.storage.database import insert_fill, record_audit_event
from options_engine.storage.models import AuditEvent, Fill

PILOT_REVIEW_MIN_TRADES = 20
PILOT_REVIEW_MAX_TRADES = 30
PILOT_REVIEW_DAYS = 90
LIVE_FILL_SOURCE = "manual_live_entry"
LIVE_FILL_CLASSIFICATION_EVENT = "LIVE_FILL_CLASSIFIED"


class LivePilotError(ValueError):
    """Raised when live-pilot inputs are missing, unsafe, or malformed."""


class LivePilotStatus(StrEnum):
    """Pilot gate and review statuses."""

    READY = "READY"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class LiveOrderType(StrEnum):
    """Observed live order types accepted by the manual fill form."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class LivePilotRuleViolationCode(StrEnum):
    """Stable reason codes for live-pilot gate failures and violations."""

    ACCOUNT_EQUITY_UNVERIFIED = "ACCOUNT_EQUITY_UNVERIFIED"
    CONFIG_LOCK_VALID = "CONFIG_LOCK_VALID"
    CONFIG_NOT_LOCKED = "CONFIG_NOT_LOCKED"
    CONFIG_VERSION_MISMATCH = "CONFIG_VERSION_MISMATCH"
    CRITICAL_SYSTEM_ERROR = "CRITICAL_SYSTEM_ERROR"
    EMERGENCY_SHUTDOWN_ACTIVE = "EMERGENCY_SHUTDOWN_ACTIVE"
    EMERGENCY_SHUTDOWN_CLEAR = "EMERGENCY_SHUTDOWN_CLEAR"
    LIMIT_ORDER_REQUIRED = "LIMIT_ORDER_REQUIRED"
    LIVE_PILOT_READY = "LIVE_PILOT_READY"
    MANUAL_EXECUTION_ONLY = "MANUAL_EXECUTION_ONLY"
    MARKET_ORDERS_FORBIDDEN = "MARKET_ORDERS_FORBIDDEN"
    ONE_LOT_ONLY = "ONE_LOT_ONLY"
    OPEN_POSITIONS_UNVERIFIED = "OPEN_POSITIONS_UNVERIFIED"
    PILOT_REVIEW_REQUIRED = "PILOT_REVIEW_REQUIRED"
    PILOT_TRADE_LIMIT_REACHED = "PILOT_TRADE_LIMIT_REACHED"
    RED_BLACK_OVERRIDE_FORBIDDEN = "RED_BLACK_OVERRIDE_FORBIDDEN"
    RISK_REPORT_NOT_REVIEWED = "RISK_REPORT_NOT_REVIEWED"
    RISK_RULE_VIOLATION = "RISK_RULE_VIOLATION"
    SIZE_INCREASE_FORBIDDEN = "SIZE_INCREASE_FORBIDDEN"


@dataclass(frozen=True, slots=True)
class LivePilotGateDecision:
    """Auditable decision from a live-pilot gate check."""

    status: LivePilotStatus
    reason_codes: tuple[str, ...]
    message: str
    checked_at: datetime

    @property
    def allow_live_pilot(self) -> bool:
        """Return true only when the pilot gate is clear."""
        return self.status == LivePilotStatus.READY

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this gate decision to a structured audit event."""
        _require_config_version(config_version)
        return AuditEvent(
            event_type=f"LIVE_PILOT_GATE_{self.status.value}",
            entity_type="live_pilot",
            message=self.message,
            metadata={
                "status": self.status.value,
                "reason_codes": list(self.reason_codes),
                "allow_live_pilot": self.allow_live_pilot,
                "checked_at": self.checked_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.checked_at,
        )


@dataclass(frozen=True, slots=True)
class LiveExecutionChecklist:
    """Manual one-lot live execution checklist."""

    checked_at: datetime
    kill_switch_state: KillSwitchState | str
    manual_execution_confirmed: bool
    one_lot_confirmed: bool
    limit_order_confirmed: bool
    no_market_order_confirmed: bool
    no_size_increase_confirmed: bool
    risk_report_reviewed: bool
    config_locked: bool
    emergency_shutdown_clear: bool
    account_equity_verified: bool
    open_positions_verified: bool

    def __post_init__(self) -> None:
        _require_aware_datetime(self.checked_at, "checked_at")

    def failed_reason_codes(self) -> tuple[str, ...]:
        """Return all failed checklist reason codes."""
        reason_codes: list[LivePilotRuleViolationCode] = []
        if not self.manual_execution_confirmed:
            reason_codes.append(LivePilotRuleViolationCode.MANUAL_EXECUTION_ONLY)
        if not self.one_lot_confirmed:
            reason_codes.append(LivePilotRuleViolationCode.ONE_LOT_ONLY)
        if not self.limit_order_confirmed:
            reason_codes.append(LivePilotRuleViolationCode.LIMIT_ORDER_REQUIRED)
        if not self.no_market_order_confirmed:
            reason_codes.append(LivePilotRuleViolationCode.MARKET_ORDERS_FORBIDDEN)
        if not self.no_size_increase_confirmed:
            reason_codes.append(LivePilotRuleViolationCode.SIZE_INCREASE_FORBIDDEN)
        if not self.risk_report_reviewed:
            reason_codes.append(LivePilotRuleViolationCode.RISK_REPORT_NOT_REVIEWED)
        if not self.config_locked:
            reason_codes.append(LivePilotRuleViolationCode.CONFIG_NOT_LOCKED)
        if not self.emergency_shutdown_clear:
            reason_codes.append(LivePilotRuleViolationCode.EMERGENCY_SHUTDOWN_ACTIVE)
        if not self.account_equity_verified:
            reason_codes.append(LivePilotRuleViolationCode.ACCOUNT_EQUITY_UNVERIFIED)
        if not self.open_positions_verified:
            reason_codes.append(LivePilotRuleViolationCode.OPEN_POSITIONS_UNVERIFIED)

        kill_state = _kill_switch_state_value(self.kill_switch_state)
        if kill_state in {KillSwitchState.RED.value, KillSwitchState.BLACK.value}:
            reason_codes.append(LivePilotRuleViolationCode.RED_BLACK_OVERRIDE_FORBIDDEN)

        return tuple(code.value for code in reason_codes)


@dataclass(frozen=True, slots=True)
class ConfigLock:
    """Auditable config lock for live pilot operation."""

    config_version: str
    locked_by: str
    reason: str
    locked_at: datetime
    locked: bool = True

    def __post_init__(self) -> None:
        _require_config_version(self.config_version)
        _require_aware_datetime(self.locked_at, "locked_at")
        if not self.locked_by.strip():
            raise LivePilotError("locked_by is required")
        if not self.reason.strip():
            raise LivePilotError("config lock reason is required")

    def to_audit_event(self) -> AuditEvent:
        """Convert this config lock to a structured audit event."""
        return AuditEvent(
            event_type="LIVE_PILOT_CONFIG_LOCKED" if self.locked else "LIVE_PILOT_CONFIG_UNLOCKED",
            entity_type="live_pilot_config",
            message="Live pilot config locked" if self.locked else "Live pilot config unlocked",
            metadata={
                "locked": self.locked,
                "config_version": self.config_version,
                "locked_by": self.locked_by.strip(),
                "reason": self.reason.strip(),
                "locked_at": self.locked_at.isoformat(),
            },
            config_version=self.config_version,
            created_at=self.locked_at,
        )


@dataclass(frozen=True, slots=True)
class EmergencyShutdownFlag:
    """Emergency shutdown flag backed by the audit log."""

    active: bool
    reason_code: str
    message: str
    flagged_at: datetime

    def __post_init__(self) -> None:
        _require_aware_datetime(self.flagged_at, "flagged_at")
        if not self.reason_code.strip():
            raise LivePilotError("emergency shutdown reason_code is required")
        if not self.message.strip():
            raise LivePilotError("emergency shutdown message is required")

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this flag state to a structured audit event."""
        _require_config_version(config_version)
        event_type = "LIVE_PILOT_EMERGENCY_SHUTDOWN_ACTIVATED" if self.active else "LIVE_PILOT_EMERGENCY_SHUTDOWN_CLEARED"
        return AuditEvent(
            event_type=event_type,
            entity_type="live_pilot_shutdown",
            message=self.message,
            metadata={
                "active": self.active,
                "reason_code": self.reason_code,
                "message": self.message,
                "flagged_at": self.flagged_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.flagged_at,
        )


@dataclass(frozen=True, slots=True)
class LivePilotRuleViolation:
    """Auditable rule violation captured during live pilot observation."""

    code: LivePilotRuleViolationCode
    message: str
    field: str
    occurred_at: datetime
    severity: str = "CRITICAL"
    stop_pilot: bool = True

    def __post_init__(self) -> None:
        _require_aware_datetime(self.occurred_at, "occurred_at")
        if not self.message.strip():
            raise LivePilotError("rule violation message is required")
        if not self.field.strip():
            raise LivePilotError("rule violation field is required")
        if self.severity not in {"WARNING", "ERROR", "CRITICAL"}:
            raise LivePilotError("rule violation severity must be WARNING, ERROR, or CRITICAL")

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this violation to a structured audit event."""
        _require_config_version(config_version)
        return AuditEvent(
            event_type="LIVE_PILOT_RULE_VIOLATION",
            entity_type="live_pilot",
            message=self.message,
            metadata={
                "code": self.code.value,
                "message": self.message,
                "field": self.field,
                "severity": self.severity,
                "stop_pilot": self.stop_pilot,
                "occurred_at": self.occurred_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.occurred_at,
        )


@dataclass(frozen=True, slots=True)
class RuleViolationRecordResult:
    """Database ids created while recording a rule violation."""

    violation_audit_id: int
    shutdown_audit_id: int | None
    emergency_shutdown_active: bool


@dataclass(frozen=True, slots=True)
class SlippageRecord:
    """Observed live fill slippage for a credit spread."""

    ticket_id: int | None
    expected_credit: Decimal
    actual_credit: Decimal
    slippage: Decimal
    recorded_at: datetime
    fill_id: int | None = None

    @property
    def adverse(self) -> bool:
        """Return true when actual credit is worse than expected credit."""
        return self.slippage > Decimal("0")

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this slippage record to a structured audit event."""
        _require_config_version(config_version)
        return AuditEvent(
            event_type="LIVE_FILL_SLIPPAGE_RECORDED",
            entity_type="fill",
            message="Manual live fill slippage recorded",
            metadata={
                "fill_id": self.fill_id,
                "ticket_id": self.ticket_id,
                "expected_credit": str(self.expected_credit),
                "actual_credit": str(self.actual_credit),
                "slippage": str(self.slippage),
                "adverse": self.adverse,
                "recorded_at": self.recorded_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.recorded_at,
        )


@dataclass(frozen=True, slots=True)
class LiveFillEntry:
    """Manual live fill entry from operator observation."""

    ticket_id: int | None
    position_id: int | None
    filled_at: datetime
    quantity: int
    price: Decimal
    expected_credit: Decimal
    source: str = LIVE_FILL_SOURCE
    order_type: LiveOrderType | str = LiveOrderType.LIMIT
    manual_execution_confirmed: bool = True
    execution_kill_switch_state: KillSwitchState | str = KillSwitchState.GREEN
    critical_system_error: bool = False
    risk_rule_violation: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.ticket_id is None and self.position_id is None:
            raise LivePilotError("live fill must reference a ticket_id or position_id")
        if self.ticket_id is not None and self.ticket_id <= 0:
            raise LivePilotError("ticket_id must be positive when provided")
        if self.position_id is not None and self.position_id <= 0:
            raise LivePilotError("position_id must be positive when provided")
        _require_aware_datetime(self.filled_at, "filled_at")
        if self.quantity <= 0:
            raise LivePilotError("quantity must be positive")
        if self.price <= Decimal("0"):
            raise LivePilotError("price must be positive")
        if self.expected_credit <= Decimal("0"):
            raise LivePilotError("expected_credit must be positive")
        if not self.source.strip():
            raise LivePilotError("source is required")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "order_type", _order_type_value(self.order_type))
        object.__setattr__(self, "execution_kill_switch_state", _kill_switch_state_value(self.execution_kill_switch_state))

    def to_fill(self, config_version: str) -> Fill:
        """Convert this manual live fill to the persistent fill model."""
        _require_config_version(config_version)
        return Fill(
            ticket_id=self.ticket_id,
            position_id=self.position_id,
            filled_at=self.filled_at,
            quantity=self.quantity,
            price=self.price,
            source=self.source,
            config_version=config_version,
            created_at=self.filled_at,
        )

    def slippage_record(self, fill_id: int | None = None) -> SlippageRecord:
        """Return the slippage record for this fill entry."""
        return SlippageRecord(
            fill_id=fill_id,
            ticket_id=self.ticket_id,
            expected_credit=self.expected_credit,
            actual_credit=self.price,
            slippage=calculate_credit_slippage(self.expected_credit, self.price),
            recorded_at=self.filled_at,
        )


@dataclass(frozen=True, slots=True)
class LiveFillResult:
    """Result of recording one manual live fill."""

    fill_id: int
    fill_audit_id: int
    slippage_audit_id: int
    classification_audit_id: int
    slippage: SlippageRecord
    violations: tuple[LivePilotRuleViolation, ...]
    violation_audit_ids: tuple[int, ...]
    shutdown_audit_ids: tuple[int, ...]

    @property
    def emergency_shutdown_active(self) -> bool:
        """Return true when recording this fill activated a shutdown."""
        return bool(self.shutdown_audit_ids)


@dataclass(frozen=True, slots=True)
class PilotReviewStatus:
    """Review gate for the one-lot live pilot."""

    status: LivePilotStatus
    trades_completed: int
    reason_codes: tuple[str, ...]
    message: str
    as_of: datetime
    days_elapsed: int | None = None

    @property
    def review_required(self) -> bool:
        """Return true once the pilot review window has been reached."""
        return self.status in {LivePilotStatus.REVIEW_REQUIRED, LivePilotStatus.STOPPED}


@dataclass(frozen=True, slots=True)
class LiveRiskDashboard:
    """Console-friendly live pilot risk dashboard."""

    report_date: date
    generated_at: datetime
    daily_report: DailyReport
    emergency_shutdown_active: bool
    emergency_shutdown_reason: str
    config_lock_status: str
    live_fills_today: int
    clean_live_fills_today: int
    violation_live_fills_today: int
    unclassified_live_fills_today: int
    rule_violations_today: int
    pilot_review_status: PilotReviewStatus

    def to_markdown(self) -> str:
        """Render the live pilot dashboard as Markdown."""
        lines = [
            f"# Live Pilot Risk Dashboard - {self.report_date.isoformat()}",
            "",
            "## Pilot Controls",
            f"- Generated at: {self.generated_at.isoformat()}",
            f"- Emergency shutdown: {'ACTIVE' if self.emergency_shutdown_active else 'CLEAR'}",
            f"- Emergency reason: {self.emergency_shutdown_reason}",
            f"- Config lock: {self.config_lock_status}",
            f"- Live fills today: {self.live_fills_today}",
            f"- Clean live fills today: {self.clean_live_fills_today}",
            f"- Violation-observation fills today: {self.violation_live_fills_today}",
            f"- Unclassified live fills today: {self.unclassified_live_fills_today}",
            f"- Rule violations today: {self.rule_violations_today}",
            f"- Pilot review status: {self.pilot_review_status.status.value}",
            f"- Pilot review reasons: {', '.join(self.pilot_review_status.reason_codes) or 'None'}",
            "",
            "## Risk State",
            f"- Account equity: {self.daily_report.account_equity}",
            f"- Current regime: {self.daily_report.current_regime}",
            f"- Kill switch state: {self.daily_report.kill_switch_state}",
            f"- Open positions: {self.daily_report.open_positions}",
            f"- Open max loss: {self.daily_report.open_max_loss}",
            f"- Portfolio heat: {self.daily_report.portfolio_heat}",
            f"- Daily PnL: {self.daily_report.daily_pnl}",
            f"- Weekly PnL: {self.daily_report.weekly_pnl}",
            f"- Monthly drawdown: {self.daily_report.monthly_drawdown}",
            "",
            "## Daily Report",
            f"- Candidates scanned: {self.daily_report.candidates_scanned}",
            f"- Tickets drafted: {self.daily_report.tickets_drafted}",
            f"- Fills recorded: {self.daily_report.fills_recorded}",
            f"- Pending tickets: {len(self.daily_report.pending_tickets)}",
            f"- Rejected trades: {len(self.daily_report.rejected_trades)}",
            f"- Exit recommendations: {len(self.daily_report.exit_recommendations)}",
        ]
        if self.daily_report.report_issues:
            lines.extend(["", "## Report Issues", *[f"- {issue}" for issue in self.daily_report.report_issues]])
        return "\n".join(lines)


def validate_live_execution_checklist(checklist: LiveExecutionChecklist) -> LivePilotGateDecision:
    """Validate the manual execution checklist before live pilot action."""
    reason_codes = checklist.failed_reason_codes()
    if not reason_codes:
        return LivePilotGateDecision(
            status=LivePilotStatus.READY,
            reason_codes=(LivePilotRuleViolationCode.LIVE_PILOT_READY.value,),
            message="Live pilot checklist passed for manual one-lot limit execution",
            checked_at=checklist.checked_at,
        )

    status = (
        LivePilotStatus.STOPPED
        if LivePilotRuleViolationCode.EMERGENCY_SHUTDOWN_ACTIVE.value in reason_codes
        else LivePilotStatus.BLOCKED
    )
    return LivePilotGateDecision(
        status=status,
        reason_codes=reason_codes,
        message="Live pilot checklist failed; do not execute a live trade",
        checked_at=checklist.checked_at,
    )


def create_config_lock(
    *,
    config_version: str,
    locked_by: str,
    reason: str,
    locked_at: datetime,
) -> ConfigLock:
    """Create a live-pilot config lock model."""
    return ConfigLock(config_version=config_version, locked_by=locked_by, reason=reason, locked_at=locked_at)


def validate_config_lock(lock: ConfigLock | None, runtime_config_version: str, checked_at: datetime) -> LivePilotGateDecision:
    """Validate that the live pilot is using the locked config version."""
    _require_config_version(runtime_config_version)
    _require_aware_datetime(checked_at, "checked_at")
    if lock is None or not lock.locked:
        return LivePilotGateDecision(
            status=LivePilotStatus.BLOCKED,
            reason_codes=(LivePilotRuleViolationCode.CONFIG_NOT_LOCKED.value,),
            message="Live pilot config is not locked",
            checked_at=checked_at,
        )
    if lock.config_version != runtime_config_version:
        return LivePilotGateDecision(
            status=LivePilotStatus.BLOCKED,
            reason_codes=(LivePilotRuleViolationCode.CONFIG_VERSION_MISMATCH.value,),
            message="Runtime config version does not match locked config",
            checked_at=checked_at,
        )
    return LivePilotGateDecision(
        status=LivePilotStatus.READY,
        reason_codes=(LivePilotRuleViolationCode.CONFIG_LOCK_VALID.value,),
        message="Live pilot config lock validated",
        checked_at=checked_at,
    )


def activate_emergency_shutdown(
    connection: sqlite3.Connection,
    *,
    reason_code: LivePilotRuleViolationCode | str,
    message: str,
    config_version: str,
    activated_at: datetime,
) -> EmergencyShutdownFlag:
    """Activate the emergency shutdown flag through the audit log."""
    code = reason_code.value if isinstance(reason_code, LivePilotRuleViolationCode) else reason_code
    flag = EmergencyShutdownFlag(active=True, reason_code=code, message=message, flagged_at=activated_at)
    record_audit_event(connection, flag.to_audit_event(config_version))
    return flag


def load_latest_emergency_shutdown(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
) -> EmergencyShutdownFlag:
    """Load the latest emergency shutdown state from the audit log."""
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    as_of_filter = "" if as_of is None else "AND created_at <= ?"
    parameters: tuple[object, ...] = () if as_of is None else (as_of.isoformat(),)
    row = connection.execute(
        f"""
        SELECT event_type, payload_json, created_at
        FROM audit_log
        WHERE event_type IN (
            'LIVE_PILOT_EMERGENCY_SHUTDOWN_ACTIVATED',
            'LIVE_PILOT_EMERGENCY_SHUTDOWN_CLEARED'
        )
        {as_of_filter}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    if row is None:
        return EmergencyShutdownFlag(
            active=False,
            reason_code=LivePilotRuleViolationCode.EMERGENCY_SHUTDOWN_CLEAR.value,
            message="No emergency shutdown flag is active",
            flagged_at=datetime.now(UTC),
        )

    metadata = _audit_metadata(row[1])
    active = row[0] == "LIVE_PILOT_EMERGENCY_SHUTDOWN_ACTIVATED"
    return EmergencyShutdownFlag(
        active=active,
        reason_code=str(metadata.get("reason_code", LivePilotRuleViolationCode.EMERGENCY_SHUTDOWN_ACTIVE.value)),
        message=str(metadata.get("message", "Emergency shutdown state loaded")),
        flagged_at=_parse_storage_datetime(row[2]),
    )


def record_rule_violation(
    connection: sqlite3.Connection,
    violation: LivePilotRuleViolation,
    *,
    config_version: str,
) -> RuleViolationRecordResult:
    """Persist a rule violation and activate shutdown when required."""
    violation_audit_id = record_audit_event(connection, violation.to_audit_event(config_version))
    shutdown_audit_id: int | None = None
    if violation.stop_pilot:
        flag = EmergencyShutdownFlag(
            active=True,
            reason_code=violation.code.value,
            message=f"Live pilot stopped: {violation.message}",
            flagged_at=violation.occurred_at,
        )
        shutdown_audit_id = record_audit_event(connection, flag.to_audit_event(config_version))
    return RuleViolationRecordResult(
        violation_audit_id=violation_audit_id,
        shutdown_audit_id=shutdown_audit_id,
        emergency_shutdown_active=shutdown_audit_id is not None,
    )


def calculate_credit_slippage(expected_credit: Decimal, actual_credit: Decimal) -> Decimal:
    """Calculate credit-spread slippage as expected credit minus actual credit."""
    if expected_credit <= Decimal("0"):
        raise LivePilotError("expected_credit must be positive")
    if actual_credit <= Decimal("0"):
        raise LivePilotError("actual_credit must be positive")
    return expected_credit - actual_credit


def record_live_fill(
    connection: sqlite3.Connection,
    entry: LiveFillEntry,
    *,
    config_version: str,
) -> LiveFillResult:
    """Record one manual live fill, slippage, and any pilot-rule violations."""
    fill = entry.to_fill(config_version)
    fill_id = insert_fill(connection, fill)
    stored_fill = Fill(
        id=fill_id,
        ticket_id=fill.ticket_id,
        position_id=fill.position_id,
        filled_at=fill.filled_at,
        quantity=fill.quantity,
        price=fill.price,
        source=fill.source,
        config_version=fill.config_version,
        created_at=fill.created_at,
    )
    fill_audit_id = record_audit_event(connection, _live_fill_audit_event(stored_fill, entry))

    slippage = entry.slippage_record(fill_id=fill_id)
    slippage_audit_id = record_audit_event(connection, slippage.to_audit_event(config_version))

    violations = _violations_for_live_fill(entry)
    violation_audit_ids: list[int] = []
    shutdown_audit_ids: list[int] = []
    for violation in violations:
        result = record_rule_violation(connection, violation, config_version=config_version)
        violation_audit_ids.append(result.violation_audit_id)
        if result.shutdown_audit_id is not None:
            shutdown_audit_ids.append(result.shutdown_audit_id)
    classification_audit_id = record_audit_event(
        connection,
        _live_fill_classification_audit_event(stored_fill, entry, violations),
    )

    return LiveFillResult(
        fill_id=fill_id,
        fill_audit_id=fill_audit_id,
        slippage_audit_id=slippage_audit_id,
        classification_audit_id=classification_audit_id,
        slippage=slippage,
        violations=violations,
        violation_audit_ids=tuple(violation_audit_ids),
        shutdown_audit_ids=tuple(shutdown_audit_ids),
    )


def evaluate_pilot_review_status(
    *,
    trades_completed: int,
    as_of: datetime,
    pilot_started_at: datetime | None = None,
) -> PilotReviewStatus:
    """Evaluate whether the one-lot pilot review window has been reached."""
    if trades_completed < 0:
        raise LivePilotError("trades_completed must be non-negative")
    _require_aware_datetime(as_of, "as_of")
    days_elapsed: int | None = None
    reason_codes: list[str] = []
    if pilot_started_at is not None:
        _require_aware_datetime(pilot_started_at, "pilot_started_at")
        if pilot_started_at > as_of:
            raise LivePilotError("pilot_started_at cannot be after as_of")
        days_elapsed = (as_of - pilot_started_at).days
        if days_elapsed >= PILOT_REVIEW_DAYS:
            reason_codes.append(LivePilotRuleViolationCode.PILOT_REVIEW_REQUIRED.value)

    if trades_completed >= PILOT_REVIEW_MAX_TRADES:
        reason_codes.append(LivePilotRuleViolationCode.PILOT_TRADE_LIMIT_REACHED.value)
        return PilotReviewStatus(
            status=LivePilotStatus.STOPPED,
            trades_completed=trades_completed,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            message="Pilot trade cap reached; stop and review before continuing",
            as_of=as_of,
            days_elapsed=days_elapsed,
        )

    if trades_completed >= PILOT_REVIEW_MIN_TRADES:
        reason_codes.append(LivePilotRuleViolationCode.PILOT_REVIEW_REQUIRED.value)

    if reason_codes:
        return PilotReviewStatus(
            status=LivePilotStatus.REVIEW_REQUIRED,
            trades_completed=trades_completed,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            message="Pilot review is required before expanding scope",
            as_of=as_of,
            days_elapsed=days_elapsed,
        )

    return PilotReviewStatus(
        status=LivePilotStatus.READY,
        trades_completed=trades_completed,
        reason_codes=(),
        message="Pilot review window has not been reached",
        as_of=as_of,
        days_elapsed=days_elapsed,
    )


def build_live_risk_dashboard_from_database(
    connection: sqlite3.Connection,
    report_date: date,
    *,
    generated_at: datetime | None = None,
    pilot_started_at: datetime | None = None,
) -> LiveRiskDashboard:
    """Build a live-pilot dashboard from local SQLite storage."""
    dashboard_generated_at = generated_at or datetime.now(UTC)
    _require_aware_datetime(dashboard_generated_at, "generated_at")
    daily_report = build_daily_report_from_database(connection, report_date, as_of=dashboard_generated_at)
    shutdown = load_latest_emergency_shutdown(connection, as_of=dashboard_generated_at)
    trades_completed = _count_live_fills(connection, as_of=dashboard_generated_at)
    live_fills_today = _count_audit_events_for_date(
        connection,
        "LIVE_FILL_RECORDED",
        report_date,
        as_of=dashboard_generated_at,
    )
    clean_live_fills_today, violation_live_fills_today = _live_fill_classification_counts(
        connection,
        report_date,
        as_of=dashboard_generated_at,
    )
    unclassified_live_fills_today = max(
        live_fills_today - clean_live_fills_today - violation_live_fills_today,
        0,
    )
    review_status = evaluate_pilot_review_status(
        trades_completed=trades_completed,
        pilot_started_at=pilot_started_at,
        as_of=dashboard_generated_at,
    )
    return LiveRiskDashboard(
        report_date=report_date,
        generated_at=dashboard_generated_at,
        daily_report=daily_report,
        emergency_shutdown_active=shutdown.active,
        emergency_shutdown_reason=shutdown.message,
        config_lock_status=_latest_config_lock_status(connection, as_of=dashboard_generated_at),
        live_fills_today=live_fills_today,
        clean_live_fills_today=clean_live_fills_today,
        violation_live_fills_today=violation_live_fills_today,
        unclassified_live_fills_today=unclassified_live_fills_today,
        rule_violations_today=_count_audit_events_for_date(
            connection,
            "LIVE_PILOT_RULE_VIOLATION",
            report_date,
            as_of=dashboard_generated_at,
        ),
        pilot_review_status=review_status,
    )


def _live_fill_audit_event(fill: Fill, entry: LiveFillEntry) -> AuditEvent:
    return AuditEvent(
        event_type="LIVE_FILL_RECORDED",
        entity_type="fill",
        message="Manual live fill recorded locally; no broker order submitted by system",
        metadata={
            "fill_id": fill.id,
            "ticket_id": fill.ticket_id,
            "position_id": fill.position_id,
            "filled_at": fill.filled_at.isoformat(),
            "quantity": fill.quantity,
            "price": str(fill.price),
            "expected_credit": str(entry.expected_credit),
            "source": fill.source,
            "order_type": str(entry.order_type),
            "manual_execution_confirmed": entry.manual_execution_confirmed,
            "execution_kill_switch_state": str(entry.execution_kill_switch_state),
            "dry_run": entry.dry_run,
            "broker_order_submitted_by_system": False,
            "auto_execution": False,
            "market_order_allowed": False,
            "config_version": fill.config_version,
        },
        config_version=fill.config_version,
        created_at=entry.filled_at,
    )


def _live_fill_classification_audit_event(
    fill: Fill,
    entry: LiveFillEntry,
    violations: tuple[LivePilotRuleViolation, ...],
) -> AuditEvent:
    reason_codes = [violation.code.value for violation in violations]
    valid_for_pilot = not reason_codes
    return AuditEvent(
        event_type=LIVE_FILL_CLASSIFICATION_EVENT,
        entity_type="fill",
        message=(
            "Manual live fill classified as clean pilot fill"
            if valid_for_pilot
            else "Manual live fill classified as violation observation"
        ),
        metadata={
            "fill_id": fill.id,
            "ticket_id": fill.ticket_id,
            "position_id": fill.position_id,
            "filled_at": fill.filled_at.isoformat(),
            "valid_for_pilot": valid_for_pilot,
            "classification": "CLEAN_PILOT_FILL" if valid_for_pilot else "VIOLATION_OBSERVATION_FILL",
            "violation_reason_codes": reason_codes,
            "violation_count": len(reason_codes),
            "dry_run": entry.dry_run,
            "broker_order_submitted_by_system": False,
            "auto_execution": False,
            "config_version": fill.config_version,
        },
        config_version=fill.config_version,
        created_at=entry.filled_at,
    )


def _violations_for_live_fill(entry: LiveFillEntry) -> tuple[LivePilotRuleViolation, ...]:
    violations: list[LivePilotRuleViolation] = []
    if not entry.manual_execution_confirmed:
        violations.append(
            _violation(
                LivePilotRuleViolationCode.MANUAL_EXECUTION_ONLY,
                "live fill was not confirmed as manual execution",
                "manual_execution_confirmed",
                entry.filled_at,
            )
        )
    if entry.quantity != 1:
        violations.append(
            _violation(
                LivePilotRuleViolationCode.ONE_LOT_ONLY,
                "live pilot allows one-lot fills only",
                "quantity",
                entry.filled_at,
            )
        )
        violations.append(
            _violation(
                LivePilotRuleViolationCode.SIZE_INCREASE_FORBIDDEN,
                "live pilot forbids size increases",
                "quantity",
                entry.filled_at,
            )
        )
    if entry.order_type != LiveOrderType.LIMIT.value:
        violations.append(
            _violation(
                LivePilotRuleViolationCode.MARKET_ORDERS_FORBIDDEN,
                "live pilot forbids market orders",
                "order_type",
                entry.filled_at,
            )
        )
    if entry.execution_kill_switch_state in {KillSwitchState.RED.value, KillSwitchState.BLACK.value}:
        violations.append(
            _violation(
                LivePilotRuleViolationCode.RED_BLACK_OVERRIDE_FORBIDDEN,
                "live pilot cannot override RED or BLACK state",
                "execution_kill_switch_state",
                entry.filled_at,
            )
        )
    if entry.critical_system_error:
        violations.append(
            _violation(
                LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR,
                "critical system error observed during live pilot",
                "critical_system_error",
                entry.filled_at,
            )
        )
    if entry.risk_rule_violation:
        violations.append(
            _violation(
                LivePilotRuleViolationCode.RISK_RULE_VIOLATION,
                "risk rule violation observed during live pilot",
                "risk_rule_violation",
                entry.filled_at,
            )
        )
    return tuple(violations)


def _violation(
    code: LivePilotRuleViolationCode,
    message: str,
    field_name: str,
    occurred_at: datetime,
) -> LivePilotRuleViolation:
    return LivePilotRuleViolation(code=code, message=message, field=field_name, occurred_at=occurred_at)


def _count_live_fills(connection: sqlite3.Connection, *, as_of: datetime | None = None) -> int:
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    as_of_filter = "" if as_of is None else "AND created_at <= ?"
    parameters: tuple[object, ...] = () if as_of is None else (as_of.isoformat(),)
    row = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM audit_log
        WHERE event_type = 'LIVE_FILL_RECORDED'
        {as_of_filter}
        """,
        parameters,
    ).fetchone()
    return int(row[0])


def _count_audit_events_for_date(
    connection: sqlite3.Connection,
    event_type: str,
    event_date: date,
    *,
    as_of: datetime | None = None,
) -> int:
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM audit_log
        WHERE event_type = ?
          AND substr(created_at, 1, 10) = ?
          AND (? IS NULL OR created_at <= ?)
        """,
        (event_type, event_date.isoformat(), None if as_of is None else as_of.isoformat(), None if as_of is None else as_of.isoformat()),
    ).fetchone()
    return int(row[0])


def _live_fill_classification_counts(
    connection: sqlite3.Connection,
    event_date: date,
    *,
    as_of: datetime | None = None,
) -> tuple[int, int]:
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    rows = connection.execute(
        """
        SELECT payload_json
        FROM audit_log
        WHERE event_type = ?
          AND substr(created_at, 1, 10) = ?
          AND (? IS NULL OR created_at <= ?)
        """,
        (
            LIVE_FILL_CLASSIFICATION_EVENT,
            event_date.isoformat(),
            None if as_of is None else as_of.isoformat(),
            None if as_of is None else as_of.isoformat(),
        ),
    ).fetchall()
    clean_count = 0
    violation_count = 0
    for row in rows:
        metadata = _audit_metadata(row[0])
        classification = metadata.get("classification")
        if classification == "CLEAN_PILOT_FILL" or metadata.get("valid_for_pilot") is True:
            clean_count += 1
        elif classification == "VIOLATION_OBSERVATION_FILL" or metadata.get("valid_for_pilot") is False:
            violation_count += 1
    return clean_count, violation_count


def _latest_config_lock_status(connection: sqlite3.Connection, *, as_of: datetime | None = None) -> str:
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    as_of_filter = "" if as_of is None else "AND created_at <= ?"
    parameters: tuple[object, ...] = () if as_of is None else (as_of.isoformat(),)
    row = connection.execute(
        f"""
        SELECT payload_json
        FROM audit_log
        WHERE event_type IN ('LIVE_PILOT_CONFIG_LOCKED', 'LIVE_PILOT_CONFIG_UNLOCKED')
          {as_of_filter}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    if row is None:
        return MISSING

    metadata = _audit_metadata(row[0])
    locked = metadata.get("locked")
    config_version = metadata.get("config_version", MISSING)
    if locked is True:
        return f"LOCKED:{config_version}"
    if locked is False:
        return f"UNLOCKED:{config_version}"
    return MISSING


def _audit_metadata(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("metadata", payload)
    return metadata if isinstance(metadata, dict) else {}


def _order_type_value(order_type: LiveOrderType | str) -> str:
    value = order_type.value if isinstance(order_type, LiveOrderType) else order_type.strip().upper()
    allowed = {item.value for item in LiveOrderType}
    if value not in allowed:
        raise LivePilotError("order_type must be LIMIT or MARKET")
    return value


def _kill_switch_state_value(kill_switch_state: KillSwitchState | str) -> str:
    value = kill_switch_state.value if isinstance(kill_switch_state, KillSwitchState) else kill_switch_state.strip().upper()
    allowed = {item.value for item in KillSwitchState}
    if value not in allowed:
        raise LivePilotError("kill_switch_state must be GREEN, YELLOW, RED, or BLACK")
    return value


def _require_config_version(config_version: str) -> None:
    if not config_version.strip():
        raise LivePilotError("config_version is required")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LivePilotError(f"{field_name} must be timezone-aware")


def _parse_storage_datetime(raw_value: str) -> datetime:
    normalized_value = f"{raw_value[:-1]}+00:00" if raw_value.endswith("Z") else raw_value
    return datetime.fromisoformat(normalized_value)
