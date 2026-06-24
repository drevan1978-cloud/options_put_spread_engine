from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_engine.reporting import (
    DailyReportInput,
    build_daily_report,
    build_daily_report_from_database,
    write_daily_report_json,
)
from options_engine.storage.database import (
    connect_database,
    initialize_database,
    insert_exit,
    insert_fill,
    insert_position,
    insert_regime_state,
    insert_trade_candidate,
    insert_trade_ticket,
    record_audit_event,
)
from options_engine.storage.models import (
    AuditEvent,
    AuditLog,
    Exit,
    Fill,
    Position,
    RegimeState,
    TradeCandidate,
    TradeTicket,
)


def test_daily_report_summarizes_local_records() -> None:
    report = build_daily_report(
        DailyReportInput(
            report_date=date(2026, 6, 20),
            trade_candidates=[
                _candidate(status="ELIGIBLE_FOR_REVIEW", id=1),
                _candidate(
                    status="REJECTED",
                    id=2,
                    rejection_codes=("BID_ASK_WIDTH_TOO_WIDE", "CREDIT_TO_WIDTH_TOO_LOW"),
                ),
            ],
            trade_tickets=[_ticket(status="DRAFT"), _ticket(status="ARCHIVED")],
            fills=[_fill()],
            positions=[_position(status="OPEN"), _position(status="CLOSED")],
            exits=[_exit(action="REVIEW_EXIT", reason_codes=("NEAR_EXPIRATION",))],
        )
    )

    assert report.candidates_scanned == 2
    assert report.candidate_status_counts == {"ELIGIBLE_FOR_REVIEW": 1, "REJECTED": 1}
    assert report.tickets_drafted == 1
    assert report.fills_recorded == 1
    assert report.clean_pilot_fills == 0
    assert report.violation_observation_fills == 0
    assert report.unclassified_fills == 1
    assert report.open_positions == 1
    assert report.exit_review_counts == {"REVIEW_EXIT": 1}
    assert report.rejection_reason_counts == {
        "BID_ASK_WIDTH_TOO_WIDE": 1,
        "CREDIT_TO_WIDTH_TOO_LOW": 1,
        "NEAR_EXPIRATION": 1,
    }
    assert report.report_issues == ()


def test_daily_report_renders_markdown() -> None:
    report = build_daily_report(
        DailyReportInput(
            report_date=date(2026, 6, 20),
            trade_candidates=[_candidate(status="REJECTED", rejection_codes=("BID_ASK_WIDTH_TOO_WIDE",))],
            trade_tickets=[],
            fills=[],
            positions=[],
            exits=[],
        )
    )

    rendered = report.to_markdown()

    assert "# Daily Report - 2026-06-20" in rendered
    assert "- Candidates scanned: 1" in rendered
    assert "- Clean pilot fills: 0" in rendered
    assert "- Violation-observation fills: 0" in rendered
    assert "- REJECTED: 1" in rendered
    assert "- BID_ASK_WIDTH_TOO_WIDE: 1" in rendered


def test_daily_report_records_malformed_candidate_json_as_issue() -> None:
    report = build_daily_report(
        DailyReportInput(
            report_date=date(2026, 6, 20),
            trade_candidates=[_candidate(reason_json="{not json", id=99)],
        )
    )

    assert report.rejection_reason_counts == {}
    assert report.report_issues == ("trade_candidate:99 contains malformed JSON",)


def test_daily_report_records_malformed_exit_reason_as_issue() -> None:
    report = build_daily_report(
        DailyReportInput(
            report_date=date(2026, 6, 20),
            exits=[_exit(reason_json=json.dumps({"reasons": [{"message": "missing code"}]}), id=7)],
        )
    )

    assert report.rejection_reason_counts == {}
    assert report.report_issues == ("exit:7 has malformed reason",)


def test_daily_report_loads_records_from_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        insert_trade_candidate(
            connection,
            _candidate(status="REJECTED", rejection_codes=("BID_ASK_WIDTH_TOO_WIDE",), id=None),
        )
        insert_trade_ticket(connection, _ticket(status="DRAFT"))
        insert_fill(connection, _fill())
        insert_position(connection, _position(status="OPEN"))
        insert_exit(connection, _exit(reason_codes=("NEAR_EXPIRATION",)))

        report = build_daily_report_from_database(connection, report_date=date(2026, 6, 20))

    assert report.candidates_scanned == 1
    assert report.candidate_status_counts == {"REJECTED": 1}
    assert report.tickets_drafted == 1
    assert report.fills_recorded == 1
    assert report.clean_pilot_fills == 0
    assert report.violation_observation_fills == 0
    assert report.unclassified_fills == 1
    assert report.open_positions == 1
    assert report.exit_review_counts == {"REVIEW_EXIT": 1}
    assert report.rejection_reason_counts == {
        "BID_ASK_WIDTH_TOO_WIDE": 1,
        "NEAR_EXPIRATION": 1,
    }


def test_daily_report_as_of_excludes_future_created_backfilled_records(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        _insert_risk_snapshot(
            connection,
            as_of="2026-06-20T14:00:00+00:00",
            account_equity="100000",
            created_at="2026-06-20T14:01:00+00:00",
        )
        _insert_risk_snapshot(
            connection,
            as_of="2026-06-20T15:00:00+00:00",
            account_equity="200000",
            created_at="2026-06-20T17:00:00+00:00",
        )
        position_id = insert_position(
            connection,
            _position(created_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC)),
        )
        insert_fill(
            connection,
            _fill(
                position_id=position_id,
                ticket_id=None,
                filled_at=datetime(2026, 6, 20, 15, 0, tzinfo=UTC),
                created_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
            ),
        )

        early_report = build_daily_report_from_database(
            connection,
            report_date=date(2026, 6, 20),
            as_of=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )
        later_report = build_daily_report_from_database(
            connection,
            report_date=date(2026, 6, 20),
            as_of=datetime(2026, 6, 20, 18, 0, tzinfo=UTC),
        )

    assert early_report.account_equity == "100000"
    assert early_report.fills_recorded == 0
    assert early_report.open_positions == 0
    assert later_report.account_equity == "200000"
    assert later_report.fills_recorded == 1
    assert later_report.open_positions == 1


def test_daily_report_as_of_treats_later_closed_position_as_open_before_close(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        position_id = insert_position(
            connection,
            _position(
                status="CLOSED",
                closed_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
            ),
        )
        insert_fill(connection, _fill(position_id=position_id, ticket_id=None))

        before_close = build_daily_report_from_database(
            connection,
            report_date=date(2026, 6, 20),
            as_of=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )
        after_close = build_daily_report_from_database(
            connection,
            report_date=date(2026, 6, 20),
            as_of=datetime(2026, 6, 20, 18, 0, tzinfo=UTC),
        )

    assert before_close.open_positions == 1
    assert before_close.open_position_details[0]["status"] == "OPEN"
    assert before_close.open_max_loss == "345.00"
    assert after_close.open_positions == 0
    assert after_close.open_max_loss == "0"


def test_daily_risk_report_from_database_includes_required_fields_and_json_output(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    output_path = tmp_path / "daily_report.json"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        candidate_id = insert_trade_candidate(
            connection,
            _candidate(status="REJECTED", rejection_codes=("BID_ASK_WIDTH_TOO_WIDE",), id=None),
        )
        insert_trade_ticket(connection, _ticket(status="DRAFT", candidate_id=candidate_id))
        position_id = insert_position(connection, _position(status="OPEN"))
        insert_fill(connection, _fill(position_id=position_id, ticket_id=None))
        insert_exit(connection, _exit(action="TAKE_PROFIT", reason_codes=("PROFIT_TARGET_HIT",)))
        insert_regime_state(connection, _regime_state())
        _insert_risk_snapshot(connection)
        record_audit_event(connection, _kill_switch_audit_event())
        record_audit_event(connection, _data_quality_warning_audit_event())

        report = build_daily_report_from_database(connection, report_date=date(2026, 6, 20))

    written_path = write_daily_report_json(report, output_path)
    payload = json.loads(written_path.read_text(encoding="utf-8"))
    markdown = report.to_markdown()

    assert report.account_equity == "100000"
    assert report.current_regime == "GREEN"
    assert report.kill_switch_state == "GREEN"
    assert report.open_positions == 1
    assert report.open_max_loss == "345.00"
    assert report.portfolio_heat == "0.00345"
    assert report.risk_by_expiration == {"2026-07-24": "345.00"}
    assert report.risk_by_underlying == {"SPY": "345.00"}
    assert report.daily_pnl == "125.50"
    assert report.weekly_pnl == "-250.00"
    assert report.monthly_drawdown == "0.0150"
    assert report.pending_tickets[0]["symbol"] == "SPY"
    assert report.rejected_trades[0]["reason_codes"] == ["BID_ASK_WIDTH_TOO_WIDE"]
    assert report.exit_recommendations[0]["action"] == "TAKE_PROFIT"
    assert report.data_quality_warnings == ("WARNING: STALE_PRICE_DATA - price data stale",)
    assert report.clean_pilot_fills == 0
    assert report.violation_observation_fills == 0
    assert report.unclassified_fills == 1
    assert "## Risk State" in markdown
    assert "- Account equity: 100000" in markdown
    assert "- Kill switch state: GREEN" in markdown
    assert payload["date"] == "2026-06-20"
    assert payload["open_max_loss"] == "345.00"
    assert payload["unclassified_fills"] == 1
    assert payload["data_quality_warnings"] == ["WARNING: STALE_PRICE_DATA - price data stale"]


def test_daily_report_distinguishes_clean_and_violation_observation_fills() -> None:
    report = build_daily_report(
        DailyReportInput(
            report_date=date(2026, 6, 20),
            fills=[_fill(), _fill()],
            audit_logs=[
                _fill_classification_audit_log(
                    fill_id=1,
                    classification="CLEAN_PILOT_FILL",
                    valid_for_pilot=True,
                    reason_codes=(),
                ),
                _fill_classification_audit_log(
                    fill_id=2,
                    classification="VIOLATION_OBSERVATION_FILL",
                    valid_for_pilot=False,
                    reason_codes=("MARKET_ORDERS_FORBIDDEN",),
                ),
            ],
        )
    )

    payload = report.to_json_dict()
    markdown = report.to_markdown()

    assert report.fills_recorded == 2
    assert report.clean_pilot_fills == 1
    assert report.violation_observation_fills == 1
    assert report.unclassified_fills == 0
    assert payload["clean_pilot_fills"] == 1
    assert payload["violation_observation_fills"] == 1
    assert "- Clean pilot fills: 1" in markdown
    assert "- Violation-observation fills: 1" in markdown


def test_daily_risk_report_shows_missing_data_clearly(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    output_path = tmp_path / "missing_report.json"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        report = build_daily_report_from_database(connection, report_date=date(2026, 6, 20))

    written_path = report.write_json(output_path)
    payload = json.loads(written_path.read_text(encoding="utf-8"))
    markdown = report.to_markdown()

    assert report.account_equity == "MISSING"
    assert report.current_regime == "MISSING"
    assert report.kill_switch_state == "MISSING"
    assert report.open_max_loss == "0"
    assert report.portfolio_heat == "MISSING"
    assert report.daily_pnl == "MISSING"
    assert report.data_quality_warnings == ("MISSING: no data quality audit entries found",)
    assert "- Account equity: MISSING" in markdown
    assert "- Kill switch state: MISSING" in markdown
    assert "MISSING: no data quality audit entries found" in markdown
    _assert_no_silent_blanks(payload)


def _candidate(
    *,
    status: str = "ELIGIBLE_FOR_REVIEW",
    rejection_codes: tuple[str, ...] = (),
    reason_json: str | None = None,
    id: int | None = None,
) -> TradeCandidate:
    return TradeCandidate(
        id=id,
        symbol="SPY",
        expiration_date=date(2026, 7, 24),
        short_put_strike=Decimal("540"),
        long_put_strike=Decimal("535"),
        max_loss=Decimal("3.40"),
        status=status,
        reason_json=reason_json if reason_json is not None else _candidate_reason_json(rejection_codes),
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )


def _candidate_reason_json(rejection_codes: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "eligibility_decision": "PASS" if not rejection_codes else "NO_TRADE",
            "rejection_reasons": [{"code": code, "message": code, "field": "test"} for code in rejection_codes],
        },
        sort_keys=True,
    )


def _ticket(status: str = "DRAFT", candidate_id: int | None = 1) -> TradeTicket:
    return TradeTicket(
        candidate_id=candidate_id,
        symbol="SPY",
        order_type="LIMIT",
        limit_price=Decimal("1.60"),
        status=status,
        notes="manual review only",
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )


def _fill(
    *,
    position_id: int | None = None,
    ticket_id: int | None = 1,
    filled_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Fill:
    return Fill(
        ticket_id=ticket_id,
        position_id=position_id,
        filled_at=filled_at or datetime(2026, 6, 20, 15, 1, tzinfo=UTC),
        quantity=1,
        price=Decimal("1.55"),
        source="manual_test",
        config_version="test-config",
        created_at=created_at or datetime(2026, 6, 20, 15, 2, tzinfo=UTC),
    )


def _position(
    status: str = "OPEN",
    *,
    opened_at: datetime | None = None,
    closed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Position:
    return Position(
        symbol="SPY",
        opened_at=opened_at or datetime(2026, 6, 20, 15, 1, tzinfo=UTC),
        quantity=1,
        short_put_strike=Decimal("540"),
        long_put_strike=Decimal("535"),
        expiration_date=date(2026, 7, 24),
        status=status,
        config_version="test-config",
        closed_at=closed_at,
        created_at=created_at or datetime(2026, 6, 20, 15, 2, tzinfo=UTC),
    )


def _exit(
    *,
    action: str = "REVIEW_EXIT",
    reason_codes: tuple[str, ...] = (),
    reason_json: str | None = None,
    id: int | None = None,
) -> Exit:
    return Exit(
        id=id,
        position_id=1,
        evaluated_at=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
        action=action,
        reason_json=reason_json if reason_json is not None else _exit_reason_json(reason_codes),
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 14, 1, tzinfo=UTC),
    )


def _exit_reason_json(reason_codes: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "action": "REVIEW_EXIT",
            "reasons": [{"code": code, "message": code, "field": "test"} for code in reason_codes],
        },
        sort_keys=True,
    )


def _regime_state() -> RegimeState:
    return RegimeState(
        symbol="SPY",
        as_of=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
        regime="GREEN",
        details_json=json.dumps({"reason_codes": ["GREEN_CONDITIONS_MET"]}),
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 14, 1, tzinfo=UTC),
    )


def _insert_risk_snapshot(
    connection: sqlite3.Connection,
    *,
    as_of: str = "2026-06-20T14:00:00+00:00",
    account_equity: str = "100000",
    created_at: str = "2026-06-20T14:01:00+00:00",
) -> None:
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
            "0.00345",
            json.dumps(
                {
                    "daily_pnl": "125.50",
                    "weekly_pnl": "-250.00",
                    "monthly_drawdown": "0.0150",
                },
                sort_keys=True,
            ),
            "test-config",
            created_at,
        ),
    )
    connection.commit()


def _kill_switch_audit_event() -> AuditEvent:
    return AuditEvent(
        event_type="KILL_SWITCH_GREEN",
        entity_type="kill_switch",
        message="Kill switch state: GREEN",
        metadata={"state": "GREEN", "reason_codes": ["STATE_GREEN"]},
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 14, 2, tzinfo=UTC),
    )


def _data_quality_warning_audit_event() -> AuditEvent:
    return AuditEvent(
        event_type="DATA_QUALITY_FAILED",
        entity_type="data_quality",
        message="price data stale",
        metadata={
            "passed": False,
            "severity": "WARNING",
            "reason_code": "STALE_PRICE_DATA",
            "message": "price data stale",
        },
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 14, 3, tzinfo=UTC),
    )


def _fill_classification_audit_log(
    *,
    fill_id: int,
    classification: str,
    valid_for_pilot: bool,
    reason_codes: tuple[str, ...],
) -> AuditLog:
    return AuditLog(
        id=fill_id,
        event_type="LIVE_FILL_CLASSIFIED",
        entity_type="fill",
        message="Manual live fill classified",
        payload_json=json.dumps(
            {
                "metadata": {
                    "fill_id": fill_id,
                    "classification": classification,
                    "valid_for_pilot": valid_for_pilot,
                    "violation_reason_codes": list(reason_codes),
                }
            },
            sort_keys=True,
        ),
        config_version="test-config",
        created_at=datetime(2026, 6, 20, 15, fill_id, tzinfo=UTC),
    )


def _assert_no_silent_blanks(value: object) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_silent_blanks(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_silent_blanks(item)
    else:
        assert value is not None
        assert value != ""
