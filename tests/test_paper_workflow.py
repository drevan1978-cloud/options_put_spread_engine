from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.data_quality import DataQualityResult
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.paper import (
    PaperEntryMarket,
    PaperExitMarket,
    PaperTradingRequest,
    PaperTradingStatus,
    run_daily_paper_workflow,
)
from options_engine.regime import RegimeLabel
from options_engine.risk import RiskCheckResult
from options_engine.storage.database import connect_database, initialize_database
from options_engine.strategy import ExitAction

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_daily_paper_workflow_persists_full_lifecycle(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    report_path = tmp_path / "paper_report.json"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        result = run_daily_paper_workflow(
            connection,
            _request(report_output_path=report_path),
        )
        counts = _table_counts(connection)
        ticket_notes = connection.execute("SELECT notes FROM trade_tickets WHERE id = ?", (result.ticket_id,)).fetchone()[0]
        fill_row = connection.execute("SELECT ticket_id, position_id, price, source FROM fills WHERE id = ?", (result.fill_id,)).fetchone()
        exit_row = connection.execute("SELECT action, reason_json FROM exits WHERE id = ?", (result.exit_id,)).fetchone()

    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    ticket_payload = json.loads(ticket_notes)
    exit_payload = json.loads(exit_row[1])

    assert result.status == PaperTradingStatus.COMPLETED
    assert result.selected_candidate_id is not None
    assert result.ticket_id is not None
    assert result.fill_id is not None
    assert result.position_id is not None
    assert result.exit_id is not None
    assert result.fill_simulation is not None
    assert result.fill_simulation.fill_price == Decimal("1.55")
    assert result.fill_simulation.comparison.expected_target_credit == Decimal("1.60")
    assert result.fill_simulation.comparison.slippage_vs_target == Decimal("0.05")
    assert result.exit_recommendation is not None
    assert result.exit_recommendation.action == ExitAction.TAKE_PROFIT

    assert counts["trade_candidates"] == 1
    assert counts["trade_tickets"] == 1
    assert counts["fills"] == 1
    assert counts["positions"] == 1
    assert counts["exits"] == 1
    assert counts["audit_log"] >= 4
    assert ticket_payload["entry_reason"] == "PAPER_TRADING_ONLY - no broker order submitted"
    assert ticket_payload["broker_order_submitted"] is False
    assert fill_row == (result.ticket_id, result.position_id, "1.55", "paper_simulator_conservative")
    assert exit_row[0] == ExitAction.TAKE_PROFIT.value
    assert exit_payload["reasons"][0]["code"] == "PROFIT_TARGET_HIT"
    assert report_path.exists()
    assert report_payload["open_positions_count"] == 1
    assert report_payload["open_max_loss"] == "345.00"
    assert report_payload["pending_tickets"][0]["symbol"] == "SPY"
    assert report_payload["exit_recommendations"][0]["action"] == ExitAction.TAKE_PROFIT.value


def test_daily_paper_workflow_marks_midpoint_only_fill_not_filled(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    report_path = tmp_path / "paper_not_filled_report.json"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        result = run_daily_paper_workflow(
            connection,
            _request(
                report_output_path=report_path,
                entry_market=PaperEntryMarket(
                    observed_at=datetime(2026, 6, 20, 14, 5, tzinfo=UTC),
                    natural_credit=Decimal("1.40"),
                    mid_credit=Decimal("1.55"),
                ),
            ),
        )
        counts = _table_counts(connection)
        audit_events = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM audit_log ORDER BY id",
            ).fetchall()
        ]

    report_payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert result.status == PaperTradingStatus.NOT_FILLED
    assert result.ticket_id is not None
    assert result.fill_id is None
    assert result.position_id is None
    assert result.exit_id is None
    assert result.fill_simulation is not None
    assert result.fill_simulation.comparison.reason_code == "UNREALISTIC_MID_REQUIRED"
    assert result.fill_simulation.comparison.simulated_credit is None
    assert counts["trade_candidates"] == 1
    assert counts["trade_tickets"] == 1
    assert counts["fills"] == 0
    assert counts["positions"] == 0
    assert counts["exits"] == 0
    assert "PAPER_FILL_NOT_FILLED" in audit_events
    assert report_payload["pending_tickets"][0]["symbol"] == "SPY"
    assert report_payload["open_positions_count"] == 0


def _request(
    *,
    report_output_path: Path,
    entry_market: PaperEntryMarket | None = None,
) -> PaperTradingRequest:
    return PaperTradingRequest(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        strategy=_strategy(),
        data_quality=DataQualityResult.pass_result(checked_at=_evaluated_at()),
        regime=RegimeLabel.GREEN,
        risk_result=RiskCheckResult.pass_result(),
        contracts=1,
        account_equity=Decimal("100000"),
        projected_portfolio_heat=Decimal("0.0500"),
        config_version="test-config",
        entry_market=entry_market
        or PaperEntryMarket(
            observed_at=datetime(2026, 6, 20, 14, 5, tzinfo=UTC),
            natural_credit=Decimal("1.55"),
            mid_credit=Decimal("1.675"),
        ),
        exit_market=PaperExitMarket(
            marked_at=datetime(2026, 6, 20, 15, 0, tzinfo=UTC),
            current_mark=Decimal("0.70"),
            theoretical_mid=Decimal("0.72"),
            current_short_delta_abs=Decimal("0.10"),
            underlying_close=Decimal("551"),
            trend_filter_price=Decimal("540"),
        ),
        worst_acceptable_credit=Decimal("1.50"),
        report_output_path=report_output_path,
    )


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _evaluated_at() -> datetime:
    return datetime(2026, 6, 20, 14, 0, tzinfo=UTC)


def _quote_timestamp() -> datetime:
    return datetime(2026, 6, 20, 14, 0, tzinfo=UTC)


def _option_chain_snapshot() -> OptionChainSnapshot:
    expiration = date(2026, 7, 24)
    return OptionChainSnapshot(
        symbol="SPY",
        quote_timestamp=_quote_timestamp(),
        expiration_date=expiration,
        quotes=(
            _quote(expiration_date=expiration, strike="540", bid="2.10", ask="2.20", delta="-0.18"),
            _quote(expiration_date=expiration, strike="535", bid="0.45", ask="0.50", delta="-0.12"),
        ),
    )


def _quote(
    *,
    expiration_date: date,
    strike: str,
    bid: str,
    ask: str,
    delta: str,
) -> OptionQuote:
    return OptionQuote(
        symbol="SPY",
        quote_timestamp=_quote_timestamp(),
        expiration_date=expiration_date,
        dte=(expiration_date - _evaluated_at().date()).days,
        option_type=OptionType.PUT,
        strike=Decimal(strike),
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        iv=Decimal("0.1800"),
        delta=Decimal(delta),
        gamma=Decimal("0.0150"),
        theta=Decimal("-0.0800"),
        vega=Decimal("0.1200"),
        volume=100,
        open_interest=1000,
    )


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    table_names = ("trade_candidates", "trade_tickets", "fills", "positions", "exits", "audit_log")
    return {table_name: connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] for table_name in table_names}
