"""Reproducible first-pilot simulation packet builder.

The demo packet seeds local SQLite storage and writes operator rehearsal
artifacts. It is strictly local: no broker APIs, no automated execution, and no
market-order workflow.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_engine.data.data_quality import DataQualityResult
from options_engine.execution import reconcile_open_positions
from options_engine.live.operations import (
    DailyPilotSignoff,
    PilotSessionStartRequest,
    build_gated_live_risk_dashboard_from_database,
    build_pilot_evidence_packet,
    record_daily_operator_signoff,
    record_live_fill_for_active_session,
    start_pilot_session,
)
from options_engine.live.pilot import LiveFillEntry, LivePilotError, create_config_lock
from options_engine.live.readiness import LivePilotDryRunRequest, run_live_pilot_readiness_dry_run
from options_engine.regime import RegimeLabel
from options_engine.risk.kill_switch import KillSwitchInputs, evaluate_kill_switch_state
from options_engine.storage.database import (
    connect_database,
    create_schema,
    initialize_database,
    insert_regime_state,
    insert_trade_candidate,
    insert_trade_ticket,
    record_audit_event,
)
from options_engine.storage.models import RegimeState, TradeCandidate, TradeTicket

DEMO_CONFIG_VERSION = "demo-config-v1"
DEMO_PILOT_ID = "pilot-demo-001"
DEMO_OPERATOR = "demo-operator"
DEMO_SYMBOL = "SPY"
DEMO_RUN_AT = datetime(2026, 6, 20, 15, 5, tzinfo=UTC)
DEMO_REPORT_DATE = date(2026, 6, 20)


@dataclass(frozen=True, slots=True)
class PilotDemoPacketRequest:
    """Inputs for building one local pilot simulation packet."""

    database_path: Path
    output_dir: Path
    run_at: datetime = DEMO_RUN_AT
    report_date: date = DEMO_REPORT_DATE
    pilot_id: str = DEMO_PILOT_ID
    operator: str = DEMO_OPERATOR
    config_version: str = DEMO_CONFIG_VERSION
    account_equity: Decimal = Decimal("100000")
    expected_credit: Decimal = Decimal("1.50")
    actual_credit: Decimal = Decimal("1.45")

    def __post_init__(self) -> None:
        if self.run_at.tzinfo is None or self.run_at.utcoffset() is None:
            raise LivePilotError("run_at must be timezone-aware")
        if not str(self.pilot_id).strip():
            raise LivePilotError("pilot_id is required")
        if not str(self.operator).strip():
            raise LivePilotError("operator is required")
        if not str(self.config_version).strip():
            raise LivePilotError("config_version is required")
        if self.account_equity <= Decimal("0"):
            raise LivePilotError("account_equity must be positive")
        if self.expected_credit <= Decimal("0"):
            raise LivePilotError("expected_credit must be positive")
        if self.actual_credit <= Decimal("0"):
            raise LivePilotError("actual_credit must be positive")


@dataclass(frozen=True, slots=True)
class PilotDemoPacketResult:
    """Paths and ids created for one demo packet."""

    database_path: Path
    output_dir: Path
    readiness_report_path: Path
    daily_report_path: Path
    dashboard_markdown_path: Path
    evidence_packet_path: Path
    operator_checklist_path: Path
    manifest_path: Path
    pilot_id: str
    ticket_id: int
    fill_id: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe manifest payload."""
        return {
            "database_path": str(self.database_path),
            "output_dir": str(self.output_dir),
            "readiness_report_path": str(self.readiness_report_path),
            "daily_report_path": str(self.daily_report_path),
            "dashboard_markdown_path": str(self.dashboard_markdown_path),
            "evidence_packet_path": str(self.evidence_packet_path),
            "operator_checklist_path": str(self.operator_checklist_path),
            "manifest_path": str(self.manifest_path),
            "pilot_id": self.pilot_id,
            "ticket_id": self.ticket_id,
            "fill_id": self.fill_id,
            "broker_orders_submitted_by_system": False,
        }


def build_pilot_demo_packet(request: PilotDemoPacketRequest) -> PilotDemoPacketResult:
    """Build a reproducible local first-pilot simulation packet."""
    request.output_dir.mkdir(parents=True, exist_ok=True)
    _initialize_empty_demo_database(request.database_path)

    readiness_report_path = request.output_dir / "readiness_report.json"
    daily_report_path = request.output_dir / "daily_risk_report.json"
    dashboard_markdown_path = request.output_dir / "pilot_dashboard.md"
    evidence_packet_path = request.output_dir / "evidence_packet.json"
    operator_checklist_path = request.output_dir / "operator_checklist.md"
    manifest_path = request.output_dir / "manifest.json"

    readiness_report = _build_isolated_readiness_report(request)
    readiness_report.write_json(readiness_report_path)

    with connect_database(request.database_path) as connection:
        _seed_demo_control_state(connection, request)
        _seed_demo_risk_state(connection, request)
        _seed_demo_regime_and_kill_switch(connection, request)
        start_pilot_session(
            connection,
            PilotSessionStartRequest(
                pilot_id=request.pilot_id,
                operator=request.operator,
                config_version=request.config_version,
                started_at=request.run_at,
            ),
        )
        candidate_id = _insert_demo_candidate(connection, request)
        ticket_id = _insert_demo_ticket(connection, request, candidate_id)
        fill_result = record_live_fill_for_active_session(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=request.run_at,
                quantity=1,
                price=request.actual_credit,
                expected_credit=request.expected_credit,
                source="demo_manual_live_entry",
                order_type="LIMIT",
                manual_execution_confirmed=True,
                execution_kill_switch_state="GREEN",
            ),
            config_version=request.config_version,
            pilot_id=request.pilot_id,
        )
        _record_demo_position_reconciliation(connection, request)
        record_daily_operator_signoff(
            connection,
            DailyPilotSignoff(
                pilot_id=request.pilot_id,
                signoff_date=request.report_date,
                operator=request.operator,
                signed_at=request.run_at,
                report_reviewed=True,
                positions_reconciled=True,
                slippage_reviewed=True,
                violations_reviewed=True,
                notes="demo packet daily closeout complete",
            ),
            config_version=request.config_version,
        )
        dashboard = build_gated_live_risk_dashboard_from_database(
            connection,
            request.report_date,
            generated_at=request.run_at,
            config_version=request.config_version,
            pilot_id=request.pilot_id,
        )
        daily_report = dashboard.daily_report
        evidence_packet = build_pilot_evidence_packet(
            connection,
            pilot_id=request.pilot_id,
            generated_at=request.run_at,
        )

    daily_report.write_json(daily_report_path)
    dashboard_markdown_path.write_text(dashboard.to_markdown(), encoding="utf-8")
    evidence_packet.write_json(evidence_packet_path)
    operator_checklist_path.write_text(_operator_checklist_markdown(request), encoding="utf-8")

    result = PilotDemoPacketResult(
        database_path=request.database_path,
        output_dir=request.output_dir,
        readiness_report_path=readiness_report_path,
        daily_report_path=daily_report_path,
        dashboard_markdown_path=dashboard_markdown_path,
        evidence_packet_path=evidence_packet_path,
        operator_checklist_path=operator_checklist_path,
        manifest_path=manifest_path,
        pilot_id=request.pilot_id,
        ticket_id=ticket_id,
        fill_id=fill_result.fill_result.fill_id,
    )
    manifest_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return result


def _initialize_empty_demo_database(database_path: Path) -> None:
    if database_path.exists() and database_path.stat().st_size > 0:
        with connect_database(database_path) as connection:
            create_schema(connection)
            if _has_existing_demo_records(connection):
                raise LivePilotError("demo database must be empty or newly created")
    else:
        initialize_database(database_path)


def _has_existing_demo_records(connection: sqlite3.Connection) -> bool:
    watched_tables = (
        "audit_log",
        "trade_candidates",
        "trade_tickets",
        "fills",
        "positions",
        "regime_states",
        "risk_snapshots",
    )
    for table_name in watched_tables:
        row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
        if int(row[0]) > 0:
            return True
    return False


def _build_isolated_readiness_report(request: PilotDemoPacketRequest) -> object:
    connection = sqlite3.connect(":memory:")
    try:
        create_schema(connection)
        return run_live_pilot_readiness_dry_run(
            connection,
            LivePilotDryRunRequest(
                report_date=request.report_date,
                run_at=request.run_at,
                config_version=request.config_version,
                operator=request.operator,
                account_equity=request.account_equity,
                expected_credit=request.expected_credit,
                actual_credit=request.actual_credit,
                notes=("DEMO_PACKET_REHEARSAL",),
            ),
        )
    finally:
        connection.close()


def _seed_demo_control_state(connection: sqlite3.Connection, request: PilotDemoPacketRequest) -> None:
    config_lock = create_config_lock(
        config_version=request.config_version,
        locked_by=request.operator,
        reason="demo packet config lock",
        locked_at=request.run_at,
    )
    record_audit_event(connection, config_lock.to_audit_event())
    record_audit_event(connection, DataQualityResult.pass_result(request.run_at).to_audit_event(request.config_version))


def _seed_demo_risk_state(connection: sqlite3.Connection, request: PilotDemoPacketRequest) -> None:
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
            "0.00355",
            json.dumps(
                {
                    "daily_pnl": "0",
                    "weekly_pnl": "0",
                    "monthly_drawdown": "0",
                    "demo_packet": True,
                },
                sort_keys=True,
            ),
            request.config_version,
            request.run_at.isoformat(),
        ),
    )
    connection.commit()


def _seed_demo_regime_and_kill_switch(connection: sqlite3.Connection, request: PilotDemoPacketRequest) -> None:
    insert_regime_state(
        connection,
        RegimeState(
            symbol=DEMO_SYMBOL,
            as_of=request.run_at,
            regime=RegimeLabel.GREEN.value,
            details_json=json.dumps(
                {"demo_packet": True, "reason_codes": ["DEMO_GREEN_REGIME"]},
                sort_keys=True,
            ),
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


def _record_demo_position_reconciliation(connection: sqlite3.Connection, request: PilotDemoPacketRequest) -> None:
    reconciliation = reconcile_open_positions(connection, checked_at=request.run_at)
    record_audit_event(connection, reconciliation.to_audit_event(request.config_version))


def _insert_demo_candidate(connection: sqlite3.Connection, request: PilotDemoPacketRequest) -> int:
    return insert_trade_candidate(
        connection,
        TradeCandidate(
            symbol=DEMO_SYMBOL,
            expiration_date=date(2026, 7, 24),
            short_put_strike=Decimal("540"),
            long_put_strike=Decimal("535"),
            max_loss=Decimal("3.55"),
            status="WATCHLIST",
            reason_json=json.dumps(
                {
                    "demo_packet": True,
                    "status": "APPROVED_FOR_MANUAL_REHEARSAL",
                    "rejection_reasons": [],
                    "warnings": ["MANUAL_EXECUTION_REQUIRED", "NO_MARKET_ORDERS"],
                },
                sort_keys=True,
            ),
            config_version=request.config_version,
            created_at=request.run_at,
        ),
    )


def _insert_demo_ticket(
    connection: sqlite3.Connection,
    request: PilotDemoPacketRequest,
    candidate_id: int,
) -> int:
    return insert_trade_ticket(
        connection,
        TradeTicket(
            candidate_id=candidate_id,
            symbol=DEMO_SYMBOL,
            order_type="LIMIT",
            limit_price=request.expected_credit,
            status="DRAFT",
            notes=json.dumps(
                {
                    "ticket_type": "MANUAL_EXECUTION_REQUIRED",
                    "warnings": ["MANUAL_EXECUTION_REQUIRED", "NO_MARKET_ORDERS"],
                    "symbol": DEMO_SYMBOL,
                    "expiration": "2026-07-24",
                    "short_strike": "540",
                    "long_strike": "535",
                    "contracts": 1,
                    "target_credit": str(request.expected_credit),
                    "worst_acceptable_credit": "1.40",
                    "broker_order_submitted": False,
                    "market_order_allowed": False,
                    "demo_packet": True,
                },
                sort_keys=True,
            ),
            config_version=request.config_version,
            created_at=request.run_at,
        ),
    )


def _operator_checklist_markdown(request: PilotDemoPacketRequest) -> str:
    return "\n".join(
        [
            f"# Operator Rehearsal Checklist - {request.report_date.isoformat()}",
            "",
            "- Confirm this is a local demo packet only.",
            "- Confirm no broker application is connected to this workflow.",
            f"- Confirm pilot id: {request.pilot_id}",
            f"- Confirm config version: {request.config_version}",
            "- Confirm exactly one active pilot session exists.",
            "- Confirm ticket says MANUAL_EXECUTION_REQUIRED.",
            "- Confirm ticket says NO_MARKET_ORDERS.",
            "- Confirm order type is LIMIT only.",
            "- Confirm quantity is one lot only.",
            "- Record fill only after manual broker observation in a real pilot.",
            "- Review slippage and dashboard after fill entry.",
            "- Complete daily operator signoff.",
            "- Export evidence packet.",
            "",
            "Do not continue if data quality, account equity, open positions, regime, kill switch, or emergency shutdown checks fail.",
        ]
    )
