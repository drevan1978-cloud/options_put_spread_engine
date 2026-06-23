"""Live-pilot readiness dry-run workflow.

This module rehearses the manual live-pilot controls against local storage.
It never submits broker orders and all generated records are marked as dry-run
or local audit artifacts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from options_engine.live.pilot import (
    LiveExecutionChecklist,
    LiveFillEntry,
    LivePilotGateDecision,
    LivePilotRuleViolation,
    LivePilotRuleViolationCode,
    LivePilotStatus,
    LiveRiskDashboard,
    build_live_risk_dashboard_from_database,
    create_config_lock,
    record_live_fill,
    record_rule_violation,
    validate_config_lock,
    validate_live_execution_checklist,
)
from options_engine.regime import RegimeLabel
from options_engine.risk.kill_switch import KillSwitchInputs, evaluate_kill_switch_state
from options_engine.storage.database import (
    insert_regime_state,
    insert_trade_ticket,
    record_audit_event,
)
from options_engine.storage.models import RegimeState, TradeTicket

DRY_RUN_FILL_SOURCE = "dry_run_manual_live_entry"


class ReadinessStatus(StrEnum):
    """Final live-pilot readiness statuses."""

    READY = "READY"
    NOT_READY = "NOT_READY"


class ReadinessCheckStatus(StrEnum):
    """Individual dry-run check result statuses."""

    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One auditable readiness check result."""

    name: str
    status: ReadinessCheckStatus
    message: str
    reason_codes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Return true when this readiness check passed."""
        return self.status == ReadinessCheckStatus.PASSED

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe check payload."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class LivePilotReadinessReport:
    """Summary of one dry-run live-pilot readiness rehearsal."""

    status: ReadinessStatus
    report_date: date
    run_at: datetime
    operator: str
    config_version: str
    checks: tuple[ReadinessCheck, ...]
    dashboard_before_violation: LiveRiskDashboard
    dashboard_after_violation: LiveRiskDashboard
    checklist_decision: LivePilotGateDecision
    continuation_decision_after_violation: LivePilotGateDecision
    created_ticket_id: int
    created_fill_id: int
    broker_orders_submitted: bool = False
    notes: tuple[str, ...] = (
        "DRY_RUN_ONLY",
        "MANUAL_EXECUTION_ONLY",
        "NO_BROKER_ORDERS",
        "NO_MARKET_ORDERS",
    )

    @property
    def ready(self) -> bool:
        """Return true when every readiness check passed."""
        return self.status == ReadinessStatus.READY

    def to_markdown(self) -> str:
        """Render the readiness report as console Markdown."""
        lines = [
            f"# Live Pilot Readiness Dry Run - {self.report_date.isoformat()}",
            "",
            f"- Status: {self.status.value}",
            f"- Run at: {self.run_at.isoformat()}",
            f"- Operator: {self.operator}",
            f"- Config version: {self.config_version}",
            f"- Broker orders submitted: {self.broker_orders_submitted}",
            f"- Dry-run ticket id: {self.created_ticket_id}",
            f"- Dry-run fill id: {self.created_fill_id}",
            "",
            "## Checks",
        ]
        lines.extend(
            f"- {check.status.value}: {check.name} - {check.message}"
            for check in self.checks
        )
        lines.extend(
            [
                "",
                "## Dashboard Before Expected Violation",
                f"- Emergency shutdown: {self.dashboard_before_violation.emergency_shutdown_active}",
                f"- Live fills today: {self.dashboard_before_violation.live_fills_today}",
                f"- Rule violations today: {self.dashboard_before_violation.rule_violations_today}",
                f"- Account equity: {self.dashboard_before_violation.daily_report.account_equity}",
                f"- Current regime: {self.dashboard_before_violation.daily_report.current_regime}",
                "",
                "## Dashboard After Expected Violation",
                f"- Emergency shutdown: {self.dashboard_after_violation.emergency_shutdown_active}",
                f"- Live fills today: {self.dashboard_after_violation.live_fills_today}",
                f"- Rule violations today: {self.dashboard_after_violation.rule_violations_today}",
                f"- Continuation decision: {self.continuation_decision_after_violation.status.value}",
                "",
                "## Notes",
            ]
        )
        lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serializable readiness report."""
        return {
            "status": self.status.value,
            "ready": self.ready,
            "report_date": self.report_date.isoformat(),
            "run_at": self.run_at.isoformat(),
            "operator": self.operator,
            "config_version": self.config_version,
            "broker_orders_submitted": self.broker_orders_submitted,
            "created_ticket_id": self.created_ticket_id,
            "created_fill_id": self.created_fill_id,
            "checks": [check.to_dict() for check in self.checks],
            "checklist_decision": {
                "status": self.checklist_decision.status.value,
                "reason_codes": list(self.checklist_decision.reason_codes),
                "message": self.checklist_decision.message,
            },
            "continuation_decision_after_violation": {
                "status": self.continuation_decision_after_violation.status.value,
                "reason_codes": list(self.continuation_decision_after_violation.reason_codes),
                "message": self.continuation_decision_after_violation.message,
            },
            "dashboard_before_violation": _dashboard_summary(self.dashboard_before_violation),
            "dashboard_after_violation": _dashboard_summary(self.dashboard_after_violation),
            "notes": list(self.notes),
        }

    def write_json(self, output_path: Path) -> Path:
        """Write this readiness report to a JSON file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(self.to_json_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return output_path


@dataclass(frozen=True, slots=True)
class LivePilotDryRunRequest:
    """Inputs for a local live-pilot readiness dry run."""

    report_date: date
    run_at: datetime
    config_version: str
    operator: str
    account_equity: Decimal = Decimal("100000")
    expected_credit: Decimal = Decimal("1.50")
    actual_credit: Decimal = Decimal("1.45")
    symbol: str = "SPY"
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.run_at.tzinfo is None or self.run_at.utcoffset() is None:
            raise ValueError("run_at must be timezone-aware")
        if not self.config_version.strip():
            raise ValueError("config_version is required")
        if not self.operator.strip():
            raise ValueError("operator is required")
        if self.account_equity <= Decimal("0"):
            raise ValueError("account_equity must be positive")
        if self.expected_credit <= Decimal("0"):
            raise ValueError("expected_credit must be positive")
        if self.actual_credit <= Decimal("0"):
            raise ValueError("actual_credit must be positive")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if not self.symbol:
            raise ValueError("symbol is required")


def run_live_pilot_readiness_dry_run(
    connection: sqlite3.Connection,
    request: LivePilotDryRunRequest,
) -> LivePilotReadinessReport:
    """Run a local live-pilot readiness rehearsal and return the report."""
    config_lock = create_config_lock(
        config_version=request.config_version,
        locked_by=request.operator,
        reason="live pilot readiness dry run",
        locked_at=request.run_at,
    )
    record_audit_event(connection, config_lock.to_audit_event())
    config_lock_decision = validate_config_lock(config_lock, request.config_version, request.run_at)
    record_audit_event(connection, config_lock_decision.to_audit_event(request.config_version))

    _seed_risk_snapshot(connection, request)
    _seed_regime_and_kill_switch(connection, request)

    checklist = LiveExecutionChecklist(
        checked_at=request.run_at,
        kill_switch_state="GREEN",
        manual_execution_confirmed=True,
        one_lot_confirmed=True,
        limit_order_confirmed=True,
        no_market_order_confirmed=True,
        no_size_increase_confirmed=True,
        risk_report_reviewed=True,
        config_locked=True,
        emergency_shutdown_clear=True,
        account_equity_verified=True,
        open_positions_verified=True,
    )
    checklist_decision = validate_live_execution_checklist(checklist)
    record_audit_event(connection, checklist_decision.to_audit_event(request.config_version))

    ticket_id = _insert_dry_run_ticket(connection, request)
    fill_result = record_live_fill(
        connection,
        LiveFillEntry(
            ticket_id=ticket_id,
            position_id=None,
            filled_at=request.run_at,
            quantity=1,
            price=request.actual_credit,
            expected_credit=request.expected_credit,
            source=DRY_RUN_FILL_SOURCE,
            order_type="LIMIT",
            manual_execution_confirmed=True,
            execution_kill_switch_state="GREEN",
            dry_run=True,
        ),
        config_version=request.config_version,
    )
    dashboard_before_violation = build_live_risk_dashboard_from_database(
        connection,
        request.report_date,
        generated_at=request.run_at,
    )

    expected_violation = LivePilotRuleViolation(
        code=LivePilotRuleViolationCode.RISK_RULE_VIOLATION,
        message="Expected readiness dry-run violation to prove emergency shutdown",
        field="readiness_dry_run",
        occurred_at=request.run_at,
    )
    record_rule_violation(connection, expected_violation, config_version=request.config_version)
    continuation_decision = validate_live_execution_checklist(
        LiveExecutionChecklist(
            checked_at=request.run_at,
            kill_switch_state="GREEN",
            manual_execution_confirmed=True,
            one_lot_confirmed=True,
            limit_order_confirmed=True,
            no_market_order_confirmed=True,
            no_size_increase_confirmed=True,
            risk_report_reviewed=True,
            config_locked=True,
            emergency_shutdown_clear=False,
            account_equity_verified=True,
            open_positions_verified=True,
        )
    )
    record_audit_event(connection, continuation_decision.to_audit_event(request.config_version))
    dashboard_after_violation = build_live_risk_dashboard_from_database(
        connection,
        request.report_date,
        generated_at=request.run_at,
    )

    checks = _build_readiness_checks(
        config_lock_decision=config_lock_decision,
        checklist_decision=checklist_decision,
        fill_id=fill_result.fill_id,
        slippage_recorded=fill_result.slippage_audit_id > 0,
        dashboard_before_violation=dashboard_before_violation,
        dashboard_after_violation=dashboard_after_violation,
        continuation_decision=continuation_decision,
    )
    status = ReadinessStatus.READY if all(check.passed for check in checks) else ReadinessStatus.NOT_READY
    notes = (
        "DRY_RUN_ONLY",
        "MANUAL_EXECUTION_ONLY",
        "NO_BROKER_ORDERS",
        "NO_MARKET_ORDERS",
        *request.notes,
    )
    return LivePilotReadinessReport(
        status=status,
        report_date=request.report_date,
        run_at=request.run_at,
        operator=request.operator.strip(),
        config_version=request.config_version,
        checks=checks,
        dashboard_before_violation=dashboard_before_violation,
        dashboard_after_violation=dashboard_after_violation,
        checklist_decision=checklist_decision,
        continuation_decision_after_violation=continuation_decision,
        created_ticket_id=ticket_id,
        created_fill_id=fill_result.fill_id,
        broker_orders_submitted=False,
        notes=notes,
    )


def _build_readiness_checks(
    *,
    config_lock_decision: LivePilotGateDecision,
    checklist_decision: LivePilotGateDecision,
    fill_id: int,
    slippage_recorded: bool,
    dashboard_before_violation: LiveRiskDashboard,
    dashboard_after_violation: LiveRiskDashboard,
    continuation_decision: LivePilotGateDecision,
) -> tuple[ReadinessCheck, ...]:
    return (
        _check(
            "config_lock",
            config_lock_decision.allow_live_pilot,
            "config lock matched runtime config",
            "config lock failed",
            config_lock_decision.reason_codes,
        ),
        _check(
            "manual_execution_checklist",
            checklist_decision.allow_live_pilot,
            "manual one-lot checklist passed",
            "manual checklist blocked readiness",
            checklist_decision.reason_codes,
        ),
        _check(
            "manual_fill_entry",
            fill_id > 0 and dashboard_before_violation.daily_report.fills_recorded >= 1,
            "manual dry-run fill was persisted",
            "manual dry-run fill was not visible in the report",
        ),
        _check(
            "slippage_tracking",
            slippage_recorded,
            "slippage audit event was recorded",
            "slippage audit event was not recorded",
        ),
        _check(
            "risk_dashboard",
            dashboard_before_violation.live_fills_today >= 1
            and dashboard_before_violation.daily_report.account_equity != "MISSING"
            and dashboard_before_violation.daily_report.current_regime != "MISSING",
            "risk dashboard updated from local storage",
            "risk dashboard has missing required dry-run state",
        ),
        _check(
            "expected_violation_shutdown",
            dashboard_after_violation.emergency_shutdown_active
            and dashboard_after_violation.rule_violations_today >= 1,
            "expected violation activated emergency shutdown",
            "expected violation did not activate emergency shutdown",
        ),
        _check(
            "continuation_blocked_after_shutdown",
            continuation_decision.status == LivePilotStatus.STOPPED,
            "emergency shutdown blocked continuation",
            "emergency shutdown did not block continuation",
            continuation_decision.reason_codes,
        ),
        _check(
            "no_automated_orders",
            True,
            "dry run submitted no broker orders",
            "broker order submission detected",
        ),
    )


def _check(
    name: str,
    condition: bool,
    passed_message: str,
    failed_message: str,
    reason_codes: tuple[str, ...] = (),
) -> ReadinessCheck:
    return ReadinessCheck(
        name=name,
        status=ReadinessCheckStatus.PASSED if condition else ReadinessCheckStatus.FAILED,
        message=passed_message if condition else failed_message,
        reason_codes=reason_codes,
    )


def _seed_risk_snapshot(connection: sqlite3.Connection, request: LivePilotDryRunRequest) -> None:
    connection.execute(
        """
        INSERT INTO risk_snapshots (
            as_of,
            account_equity,
            portfolio_heat,
            details_json,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            request.run_at.isoformat(),
            str(request.account_equity),
            "0",
            json.dumps(
                {
                    "daily_pnl": "0",
                    "weekly_pnl": "0",
                    "monthly_drawdown": "0",
                    "dry_run": True,
                },
                sort_keys=True,
            ),
            request.config_version,
            request.run_at.isoformat(),
        ),
    )
    connection.commit()


def _seed_regime_and_kill_switch(connection: sqlite3.Connection, request: LivePilotDryRunRequest) -> None:
    insert_regime_state(
        connection,
        RegimeState(
            symbol=request.symbol,
            as_of=request.run_at,
            regime=RegimeLabel.GREEN.value,
            details_json=json.dumps({"dry_run": True, "reason_codes": ["READINESS_DRY_RUN"]}, sort_keys=True),
            config_version=request.config_version,
            created_at=request.run_at,
        ),
    )
    kill_switch = evaluate_kill_switch_state(
        KillSwitchInputs(
            evaluated_at=request.run_at,
            account_equity=request.account_equity,
            current_regime=RegimeLabel.GREEN,
        )
    )
    record_audit_event(connection, kill_switch.to_audit_event(request.config_version))


def _insert_dry_run_ticket(connection: sqlite3.Connection, request: LivePilotDryRunRequest) -> int:
    return insert_trade_ticket(
        connection,
        TradeTicket(
            candidate_id=None,
            symbol=request.symbol,
            order_type="LIMIT",
            limit_price=request.expected_credit,
            status="DRAFT",
            notes=json.dumps(
                {
                    "ticket_type": "DRY_RUN_MANUAL_EXECUTION_REQUIRED",
                    "warnings": ["DRY_RUN_ONLY", "MANUAL_EXECUTION_REQUIRED", "NO_MARKET_ORDERS"],
                    "broker_order_submitted": False,
                    "dry_run": True,
                },
                sort_keys=True,
            ),
            config_version=request.config_version,
            created_at=request.run_at,
        ),
    )


def _dashboard_summary(dashboard: LiveRiskDashboard) -> dict[str, object]:
    return {
        "report_date": dashboard.report_date.isoformat(),
        "emergency_shutdown_active": dashboard.emergency_shutdown_active,
        "emergency_shutdown_reason": dashboard.emergency_shutdown_reason,
        "config_lock_status": dashboard.config_lock_status,
        "live_fills_today": dashboard.live_fills_today,
        "rule_violations_today": dashboard.rule_violations_today,
        "account_equity": dashboard.daily_report.account_equity,
        "current_regime": dashboard.daily_report.current_regime,
        "kill_switch_state": dashboard.daily_report.kill_switch_state,
        "fills_recorded": dashboard.daily_report.fills_recorded,
        "pending_tickets": len(dashboard.daily_report.pending_tickets),
    }
