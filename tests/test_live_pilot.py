from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from options_engine.live import (
    LiveExecutionChecklist,
    LiveFillEntry,
    LiveOrderType,
    LivePilotRuleViolation,
    LivePilotRuleViolationCode,
    LivePilotStatus,
    activate_emergency_shutdown,
    build_live_risk_dashboard_from_database,
    create_config_lock,
    evaluate_pilot_review_status,
    load_latest_emergency_shutdown,
    record_live_fill,
    record_rule_violation,
    validate_config_lock,
    validate_live_execution_checklist,
)
from options_engine.storage.database import (
    connect_database,
    initialize_database,
    insert_trade_ticket,
    record_audit_event,
)
from options_engine.storage.models import TradeTicket


def test_live_execution_checklist_passes_when_all_controls_confirmed() -> None:
    checked_at = datetime(2026, 6, 20, 14, 0, tzinfo=UTC)
    checklist = LiveExecutionChecklist(
        checked_at=checked_at,
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

    decision = validate_live_execution_checklist(checklist)

    assert decision.status == LivePilotStatus.READY
    assert decision.allow_live_pilot is True
    assert decision.reason_codes == (LivePilotRuleViolationCode.LIVE_PILOT_READY.value,)


def test_live_execution_checklist_blocks_red_or_black_override() -> None:
    checklist = LiveExecutionChecklist(
        checked_at=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
        kill_switch_state="RED",
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

    decision = validate_live_execution_checklist(checklist)

    assert decision.status == LivePilotStatus.BLOCKED
    assert decision.allow_live_pilot is False
    assert LivePilotRuleViolationCode.RED_BLACK_OVERRIDE_FORBIDDEN.value in decision.reason_codes


def test_config_lock_requires_matching_runtime_version() -> None:
    checked_at = datetime(2026, 6, 20, 14, 0, tzinfo=UTC)
    lock = create_config_lock(
        config_version="config-v1",
        locked_by="operator",
        reason="pilot start",
        locked_at=checked_at,
    )

    valid = validate_config_lock(lock, "config-v1", checked_at)
    invalid = validate_config_lock(lock, "config-v2", checked_at)

    assert valid.status == LivePilotStatus.READY
    assert invalid.status == LivePilotStatus.BLOCKED
    assert invalid.reason_codes == (LivePilotRuleViolationCode.CONFIG_VERSION_MISMATCH.value,)


def test_live_fill_records_manual_fill_and_slippage(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        ticket_id = _insert_ticket(connection)
        result = record_live_fill(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
                quantity=1,
                price=Decimal("1.45"),
                expected_credit=Decimal("1.50"),
                order_type=LiveOrderType.LIMIT,
                manual_execution_confirmed=True,
                execution_kill_switch_state="GREEN",
            ),
            config_version="config-v1",
        )
        fill_row = connection.execute(
            "SELECT ticket_id, quantity, price, source FROM fills WHERE id = ?",
            (result.fill_id,),
        ).fetchone()
        audit_rows = connection.execute(
            "SELECT event_type, payload_json FROM audit_log ORDER BY id"
        ).fetchall()

    assert fill_row == (ticket_id, 1, "1.45", "manual_live_entry")
    assert result.slippage.slippage == Decimal("0.05")
    assert result.slippage.adverse is True
    assert result.violations == ()
    assert "LIVE_FILL_RECORDED" in [row[0] for row in audit_rows]
    assert "LIVE_FILL_SLIPPAGE_RECORDED" in [row[0] for row in audit_rows]
    classification_payload = next(json.loads(row[1]) for row in audit_rows if row[0] == "LIVE_FILL_CLASSIFIED")
    assert result.classification_audit_id > 0
    assert classification_payload["metadata"]["classification"] == "CLEAN_PILOT_FILL"
    assert classification_payload["metadata"]["valid_for_pilot"] is True
    assert classification_payload["metadata"]["violation_reason_codes"] == []


def test_live_fill_tracks_rule_violation_and_stops_pilot(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        ticket_id = _insert_ticket(connection)
        result = record_live_fill(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
                quantity=2,
                price=Decimal("1.45"),
                expected_credit=Decimal("1.50"),
                order_type=LiveOrderType.MARKET,
                manual_execution_confirmed=True,
                execution_kill_switch_state="BLACK",
                critical_system_error=True,
            ),
            config_version="config-v1",
        )
        latest_shutdown = load_latest_emergency_shutdown(connection)
        persisted_quantity = connection.execute("SELECT quantity FROM fills WHERE id = ?", (result.fill_id,)).fetchone()[0]
        classification_payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM audit_log WHERE event_type = 'LIVE_FILL_CLASSIFIED'"
            ).fetchone()[0]
        )

    violation_codes = {violation.code for violation in result.violations}
    assert persisted_quantity == 2
    assert LivePilotRuleViolationCode.ONE_LOT_ONLY in violation_codes
    assert LivePilotRuleViolationCode.SIZE_INCREASE_FORBIDDEN in violation_codes
    assert LivePilotRuleViolationCode.MARKET_ORDERS_FORBIDDEN in violation_codes
    assert LivePilotRuleViolationCode.RED_BLACK_OVERRIDE_FORBIDDEN in violation_codes
    assert LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR in violation_codes
    assert result.emergency_shutdown_active is True
    assert latest_shutdown.active is True
    assert classification_payload["metadata"]["classification"] == "VIOLATION_OBSERVATION_FILL"
    assert classification_payload["metadata"]["valid_for_pilot"] is False
    assert classification_payload["metadata"]["violation_count"] == 5
    assert LivePilotRuleViolationCode.MARKET_ORDERS_FORBIDDEN.value in classification_payload["metadata"][
        "violation_reason_codes"
    ]


def test_rule_violation_tracker_records_shutdown(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    occurred_at = datetime(2026, 6, 20, 15, 5, tzinfo=UTC)

    with connect_database(database_path) as connection:
        result = record_rule_violation(
            connection,
            LivePilotRuleViolation(
                code=LivePilotRuleViolationCode.RISK_RULE_VIOLATION,
                message="risk cap breached during pilot",
                field="portfolio_heat",
                occurred_at=occurred_at,
            ),
            config_version="config-v1",
        )
        rows = connection.execute("SELECT event_type FROM audit_log ORDER BY id").fetchall()

    assert result.emergency_shutdown_active is True
    assert result.shutdown_audit_id is not None
    assert rows == [("LIVE_PILOT_RULE_VIOLATION",), ("LIVE_PILOT_EMERGENCY_SHUTDOWN_ACTIVATED",)]


def test_emergency_shutdown_flag_is_loaded_from_audit_log(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        activate_emergency_shutdown(
            connection,
            reason_code=LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR,
            message="critical system error",
            config_version="config-v1",
            activated_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )
        shutdown = load_latest_emergency_shutdown(connection)

    assert shutdown.active is True
    assert shutdown.reason_code == LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR.value


def test_pilot_review_required_after_twenty_trades_or_three_months() -> None:
    as_of = datetime(2026, 6, 20, 14, 0, tzinfo=UTC)

    trade_review = evaluate_pilot_review_status(trades_completed=20, as_of=as_of)
    time_review = evaluate_pilot_review_status(
        trades_completed=3,
        pilot_started_at=as_of - timedelta(days=90),
        as_of=as_of,
    )
    hard_stop = evaluate_pilot_review_status(trades_completed=30, as_of=as_of)

    assert trade_review.status == LivePilotStatus.REVIEW_REQUIRED
    assert time_review.status == LivePilotStatus.REVIEW_REQUIRED
    assert hard_stop.status == LivePilotStatus.STOPPED


def test_live_risk_dashboard_updates_from_daily_report_and_audit(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        ticket_id = _insert_ticket(connection)
        lock = create_config_lock(
            config_version="config-v1",
            locked_by="operator",
            reason="pilot start",
            locked_at=datetime(2026, 6, 20, 13, 0, tzinfo=UTC),
        )
        record_audit_event(connection, lock.to_audit_event())
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
                "2026-06-20T13:00:00+00:00",
                "100000",
                "0.01",
                json.dumps({"daily_pnl": "0", "weekly_pnl": "0", "monthly_drawdown": "0"}),
                "config-v1",
                "2026-06-20T13:00:00+00:00",
            ),
        )
        connection.commit()
        record_live_fill(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
                quantity=1,
                price=Decimal("1.45"),
                expected_credit=Decimal("1.50"),
            ),
            config_version="config-v1",
        )
        dashboard = build_live_risk_dashboard_from_database(
            connection,
            date(2026, 6, 20),
            generated_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )

    assert dashboard.daily_report.fills_recorded == 1
    assert dashboard.daily_report.account_equity == "100000"
    assert dashboard.live_fills_today == 1
    assert dashboard.clean_live_fills_today == 1
    assert dashboard.violation_live_fills_today == 0
    assert dashboard.unclassified_live_fills_today == 0
    assert dashboard.daily_report.clean_pilot_fills == 1
    assert dashboard.daily_report.violation_observation_fills == 0
    assert dashboard.daily_report.unclassified_fills == 0
    assert dashboard.rule_violations_today == 0
    assert dashboard.emergency_shutdown_active is False
    assert dashboard.config_lock_status == "LOCKED:config-v1"
    assert "# Live Pilot Risk Dashboard - 2026-06-20" in dashboard.to_markdown()
    assert "- Clean live fills today: 1" in dashboard.to_markdown()
    assert "- Violation-observation fills today: 0" in dashboard.to_markdown()


def test_live_risk_dashboard_uses_generated_at_as_intraday_boundary(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        ticket_id = _insert_ticket(connection)
        lock = create_config_lock(
            config_version="config-v1",
            locked_by="operator",
            reason="pilot start",
            locked_at=datetime(2026, 6, 20, 13, 0, tzinfo=UTC),
        )
        record_audit_event(connection, lock.to_audit_event())
        _insert_risk_snapshot(connection, "2026-06-20T13:00:00+00:00", "100000")
        record_live_fill(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
                quantity=1,
                price=Decimal("1.45"),
                expected_credit=Decimal("1.50"),
            ),
            config_version="config-v1",
        )
        _insert_risk_snapshot(connection, "2026-06-20T17:00:00+00:00", "200000")
        record_live_fill(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
                quantity=1,
                price=Decimal("1.40"),
                expected_credit=Decimal("1.50"),
            ),
            config_version="config-v1",
        )
        activate_emergency_shutdown(
            connection,
            reason_code=LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR,
            message="future shutdown",
            config_version="config-v1",
            activated_at=datetime(2026, 6, 20, 17, 5, tzinfo=UTC),
        )

        dashboard_at_1600 = build_live_risk_dashboard_from_database(
            connection,
            date(2026, 6, 20),
            generated_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )
        dashboard_at_1800 = build_live_risk_dashboard_from_database(
            connection,
            date(2026, 6, 20),
            generated_at=datetime(2026, 6, 20, 18, 0, tzinfo=UTC),
        )

    assert dashboard_at_1600.daily_report.account_equity == "100000"
    assert dashboard_at_1600.daily_report.fills_recorded == 1
    assert dashboard_at_1600.live_fills_today == 1
    assert dashboard_at_1600.clean_live_fills_today == 1
    assert dashboard_at_1600.emergency_shutdown_active is False

    assert dashboard_at_1800.daily_report.account_equity == "200000"
    assert dashboard_at_1800.daily_report.fills_recorded == 2
    assert dashboard_at_1800.live_fills_today == 2
    assert dashboard_at_1800.clean_live_fills_today == 2
    assert dashboard_at_1800.emergency_shutdown_active is True


def test_live_risk_dashboard_distinguishes_violation_observation_fills(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        ticket_id = _insert_ticket(connection)
        record_live_fill(
            connection,
            LiveFillEntry(
                ticket_id=ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
                quantity=2,
                price=Decimal("1.45"),
                expected_credit=Decimal("1.50"),
                order_type=LiveOrderType.MARKET,
                manual_execution_confirmed=True,
                execution_kill_switch_state="BLACK",
                critical_system_error=True,
            ),
            config_version="config-v1",
        )
        dashboard = build_live_risk_dashboard_from_database(
            connection,
            date(2026, 6, 20),
            generated_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )

    markdown = dashboard.to_markdown()

    assert dashboard.live_fills_today == 1
    assert dashboard.clean_live_fills_today == 0
    assert dashboard.violation_live_fills_today == 1
    assert dashboard.unclassified_live_fills_today == 0
    assert dashboard.daily_report.clean_pilot_fills == 0
    assert dashboard.daily_report.violation_observation_fills == 1
    assert dashboard.daily_report.unclassified_fills == 0
    assert dashboard.rule_violations_today == 5
    assert "- Violation-observation fills today: 1" in markdown


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


def _insert_risk_snapshot(connection: object, as_of: str, account_equity: str) -> None:
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
            as_of,
            account_equity,
            "0.01",
            json.dumps({"daily_pnl": "0", "weekly_pnl": "0", "monthly_drawdown": "0"}),
            "config-v1",
            as_of,
        ),
    )
    connection.commit()
