from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.live import (
    DailyPilotSignoff,
    DailySignoffStatus,
    LiveFillEntry,
    LivePilotError,
    LivePilotStatus,
    PilotSessionReasonCode,
    PilotSessionStartRequest,
    PilotSessionStatus,
    build_gated_live_risk_dashboard_from_database,
    build_pilot_evidence_packet,
    load_pilot_sessions,
    record_daily_operator_signoff,
    record_live_fill_for_active_session,
    record_pilot_reset_review,
    require_active_pilot_session,
    resume_pilot_session,
    start_pilot_session,
    stop_pilot_session,
)
from options_engine.storage.database import connect_database, initialize_database, insert_trade_ticket
from options_engine.storage.models import TradeTicket


def test_start_pilot_session_creates_single_active_session_and_gate(tmp_path: Path) -> None:
    database_path = tmp_path / "pilot.sqlite"
    initialize_database(database_path)
    started_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)

    with connect_database(database_path) as connection:
        session = start_pilot_session(connection, _start_request(started_at=started_at))
        gate = require_active_pilot_session(
            connection,
            checked_at=started_at,
            config_version="config-v1",
            pilot_id="pilot-001",
        )

        with pytest.raises(LivePilotError, match="another session is active"):
            start_pilot_session(
                connection,
                PilotSessionStartRequest(
                    pilot_id="pilot-002",
                    operator="operator",
                    config_version="config-v1",
                    started_at=started_at,
                ),
            )

    assert session.status == PilotSessionStatus.ACTIVE
    assert gate.status == LivePilotStatus.READY
    assert gate.allow_operation is True
    assert gate.pilot_session is not None
    assert gate.pilot_session.trade_count == 0


def test_session_gated_live_fill_records_fill_and_increments_trade_count(tmp_path: Path) -> None:
    database_path = tmp_path / "pilot.sqlite"
    initialize_database(database_path)
    started_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)
    filled_at = datetime(2026, 6, 20, 15, 5, tzinfo=UTC)

    with connect_database(database_path) as connection:
        start_pilot_session(connection, _start_request(started_at=started_at))
        ticket_id = _insert_ticket(connection)
        result = record_live_fill_for_active_session(
            connection,
            _fill_entry(ticket_id=ticket_id, filled_at=filled_at),
            config_version="config-v1",
            pilot_id="pilot-001",
        )
        sessions = load_pilot_sessions(connection)
        dashboard = build_gated_live_risk_dashboard_from_database(
            connection,
            date(2026, 6, 20),
            generated_at=filled_at,
            config_version="config-v1",
            pilot_id="pilot-001",
        )

    assert result.gate_decision.status == LivePilotStatus.READY
    assert result.fill_result.fill_id > 0
    assert result.stop_audit_id is None
    assert len(sessions) == 1
    assert sessions[0].trade_count == 1
    assert dashboard.live_fills_today == 1


def test_session_fill_violation_stops_pilot_and_requires_reset_before_resume(tmp_path: Path) -> None:
    database_path = tmp_path / "pilot.sqlite"
    initialize_database(database_path)
    started_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)
    filled_at = datetime(2026, 6, 20, 15, 5, tzinfo=UTC)

    with connect_database(database_path) as connection:
        start_pilot_session(connection, _start_request(started_at=started_at))
        ticket_id = _insert_ticket(connection)
        result = record_live_fill_for_active_session(
            connection,
            _fill_entry(ticket_id=ticket_id, filled_at=filled_at, risk_rule_violation=True),
            config_version="config-v1",
            pilot_id="pilot-001",
        )
        stopped_session = load_pilot_sessions(connection)[0]

        with pytest.raises(LivePilotError, match="reset review is required"):
            resume_pilot_session(
                connection,
                pilot_id="pilot-001",
                resumed_at=datetime(2026, 6, 20, 15, 30, tzinfo=UTC),
                review_note="resume without reset",
                config_version="config-v1",
            )

        record_pilot_reset_review(
            connection,
            pilot_id="pilot-001",
            reviewed_at=datetime(2026, 6, 20, 15, 35, tzinfo=UTC),
            review_note="risk reviewed and reset approved",
            config_version="config-v1",
        )
        resume_pilot_session(
            connection,
            pilot_id="pilot-001",
            resumed_at=datetime(2026, 6, 20, 15, 40, tzinfo=UTC),
            review_note="resume approved after reset",
            config_version="config-v1",
        )
        resumed_session = load_pilot_sessions(connection)[0]

    assert result.stop_audit_id is not None
    assert stopped_session.status == PilotSessionStatus.STOPPED
    assert stopped_session.stop_reason_code == PilotSessionReasonCode.RISK_RULE_VIOLATION.value
    assert resumed_session.status == PilotSessionStatus.ACTIVE
    assert resumed_session.last_review_note == "resume approved after reset"


def test_operator_stop_and_resume_requires_explicit_reason_but_not_reset(tmp_path: Path) -> None:
    database_path = tmp_path / "pilot.sqlite"
    initialize_database(database_path)
    started_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)

    with connect_database(database_path) as connection:
        start_pilot_session(connection, _start_request(started_at=started_at))
        with pytest.raises(LivePilotError, match="reason is required"):
            stop_pilot_session(
                connection,
                pilot_id="pilot-001",
                stopped_at=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
                reason_code=PilotSessionReasonCode.OPERATOR_STOP,
                reason=" ",
                config_version="config-v1",
            )
        stop_pilot_session(
            connection,
            pilot_id="pilot-001",
            stopped_at=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
            reason_code=PilotSessionReasonCode.OPERATOR_STOP,
            reason="operator pause",
            config_version="config-v1",
        )
        resume_pilot_session(
            connection,
            pilot_id="pilot-001",
            resumed_at=datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
            review_note="operator reviewed pause",
            config_version="config-v1",
        )
        session = load_pilot_sessions(connection)[0]

    assert session.status == PilotSessionStatus.ACTIVE


def test_daily_operator_signoff_records_pass_and_failures(tmp_path: Path) -> None:
    database_path = tmp_path / "pilot.sqlite"
    initialize_database(database_path)
    started_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)

    with connect_database(database_path) as connection:
        start_pilot_session(connection, _start_request(started_at=started_at))
        failed = record_daily_operator_signoff(
            connection,
            DailyPilotSignoff(
                pilot_id="pilot-001",
                signoff_date=date(2026, 6, 20),
                operator="operator",
                signed_at=datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
                report_reviewed=True,
                positions_reconciled=False,
                slippage_reviewed=True,
                violations_reviewed=True,
                notes="positions not reconciled",
            ),
            config_version="config-v1",
        )
        passed = record_daily_operator_signoff(
            connection,
            DailyPilotSignoff(
                pilot_id="pilot-001",
                signoff_date=date(2026, 6, 20),
                operator="operator",
                signed_at=datetime(2026, 6, 20, 20, 5, tzinfo=UTC),
                report_reviewed=True,
                positions_reconciled=True,
                slippage_reviewed=True,
                violations_reviewed=True,
                notes="daily closeout complete",
            ),
            config_version="config-v1",
        )

    assert failed.status == DailySignoffStatus.FAILED
    assert failed.reason_codes == ("POSITIONS_NOT_RECONCILED",)
    assert passed.status == DailySignoffStatus.PASSED
    assert passed.reason_codes == ()


def test_evidence_packet_exports_session_fills_slippage_and_signoff(tmp_path: Path) -> None:
    database_path = tmp_path / "pilot.sqlite"
    output_path = tmp_path / "evidence.json"
    initialize_database(database_path)
    started_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)
    filled_at = datetime(2026, 6, 20, 15, 5, tzinfo=UTC)

    with connect_database(database_path) as connection:
        start_pilot_session(connection, _start_request(started_at=started_at))
        ticket_id = _insert_ticket(connection)
        record_live_fill_for_active_session(
            connection,
            _fill_entry(ticket_id=ticket_id, filled_at=filled_at),
            config_version="config-v1",
            pilot_id="pilot-001",
        )
        record_daily_operator_signoff(
            connection,
            DailyPilotSignoff(
                pilot_id="pilot-001",
                signoff_date=date(2026, 6, 20),
                operator="operator",
                signed_at=datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
                report_reviewed=True,
                positions_reconciled=True,
                slippage_reviewed=True,
                violations_reviewed=True,
                notes="daily closeout complete",
            ),
            config_version="config-v1",
        )
        packet = build_pilot_evidence_packet(
            connection,
            pilot_id="pilot-001",
            generated_at=datetime(2026, 6, 20, 21, 0, tzinfo=UTC),
        )
        packet.write_json(output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert packet.pilot_session.pilot_id == "pilot-001"
    assert len(packet.fills) == 1
    assert len(packet.slippage_events) == 1
    assert len(packet.daily_signoffs) == 1
    assert payload["broker_orders_submitted_by_system"] is False
    assert payload["pilot_session"]["trade_count"] == 1


def _start_request(started_at: datetime) -> PilotSessionStartRequest:
    return PilotSessionStartRequest(
        pilot_id="pilot-001",
        operator="operator",
        config_version="config-v1",
        started_at=started_at,
    )


def _fill_entry(
    *,
    ticket_id: int,
    filled_at: datetime,
    risk_rule_violation: bool = False,
) -> LiveFillEntry:
    return LiveFillEntry(
        ticket_id=ticket_id,
        position_id=None,
        filled_at=filled_at,
        quantity=1,
        price=Decimal("1.45"),
        expected_credit=Decimal("1.50"),
        manual_execution_confirmed=True,
        execution_kill_switch_state="GREEN",
        risk_rule_violation=risk_rule_violation,
    )


def _insert_ticket(connection: object) -> int:
    return insert_trade_ticket(
        connection,
        TradeTicket(
            candidate_id=None,
            symbol="SPY",
            order_type="LIMIT",
            limit_price=Decimal("1.50"),
            status="DRAFT",
            notes="MANUAL_EXECUTION_REQUIRED; NO_MARKET_ORDERS",
            config_version="config-v1",
            created_at=datetime(2026, 6, 20, 13, 30, tzinfo=UTC),
        ),
    )
