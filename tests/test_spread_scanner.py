from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType, load_option_chain_csv
from options_engine.regime import RegimeLabel
from options_engine.storage.database import initialize_database, insert_trade_candidates, record_audit_event
from options_engine.strategy import CandidateScanStatus, audit_events_for_scan, scan_spreads, storage_models_for_scan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_scan_spreads_enumerates_put_verticals_and_evaluates_eligibility() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )

    assert len(result.spreads) == 3
    assert len(result.eligible_spreads) == 1
    assert len(result.rejected_spreads) == 2
    eligible = result.eligible_spreads[0]
    assert eligible.candidate.short_put.strike == Decimal("540")
    assert eligible.candidate.long_put.strike == Decimal("535")
    assert eligible.status == CandidateScanStatus.WATCHLIST
    assert eligible.conservative_credit == Decimal("1.60")
    assert eligible.width == Decimal("5")
    assert eligible.max_loss == Decimal("3.40")
    assert eligible.breakeven == Decimal("538.40")
    assert eligible.credit_to_width == Decimal("0.32")
    assert eligible.max_bid_ask_width_pct == Decimal("0.1052631578947368421052631579")


def test_scan_spreads_generates_candidates_from_sample_chain_fixture() -> None:
    snapshot = next(snapshot for snapshot in load_option_chain_csv(FIXTURE_DIR / "sample_option_chain.csv") if snapshot.symbol == "SPY")

    result = scan_spreads(
        option_chain=snapshot,
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=Decimal("100"),
    )

    assert len(result.spreads) == 1
    assert {spread.candidate.short_put.option_type for spread in result.spreads} == {OptionType.PUT}


def test_scan_spreads_ignores_call_quotes() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )

    scanned_option_types = {
        spread.candidate.short_put.option_type
        for spread in result.spreads
    } | {
        spread.candidate.long_put.option_type
        for spread in result.spreads
    }

    assert scanned_option_types == {OptionType.PUT}


def test_bearish_regime_rejects_all_scanned_spreads() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.BEARISH,
        strategy=_strategy(),
    )

    assert len(result.spreads) == 3
    assert len(result.eligible_spreads) == 0
    assert len(result.rejected_spreads) == 3
    assert {spread.status for spread in result.spreads} == {CandidateScanStatus.BLOCKED_BY_REGIME}


def test_bad_dte_is_blocked_by_data() -> None:
    expiration = date(2026, 7, 10)
    result = scan_spreads(
        option_chain=_option_chain_snapshot(expiration=expiration),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=Decimal("100"),
    )

    assert {spread.status for spread in result.spreads} == {CandidateScanStatus.BLOCKED_BY_DATA}


def test_bad_short_delta_is_blocked_by_data() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(short_delta="-0.30"),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=Decimal("100"),
    )

    assert result.spreads[0].status == CandidateScanStatus.BLOCKED_BY_DATA


def test_wide_bid_ask_is_blocked_by_liquidity() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(long_bid="0.20", long_ask="0.50"),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=Decimal("100"),
    )

    assert CandidateScanStatus.BLOCKED_BY_LIQUIDITY in {spread.status for spread in result.spreads}


def test_low_credit_to_width_is_blocked_by_liquidity() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(short_bid="1.80", short_ask="1.90"),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=Decimal("100"),
    )

    assert result.spreads[0].status == CandidateScanStatus.BLOCKED_BY_LIQUIDITY


def test_risk_budget_blocks_candidate_before_watchlist() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=Decimal("1"),
    )

    assert result.spreads[0].status == CandidateScanStatus.BLOCKED_BY_RISK


def test_storage_models_for_scan_include_auditable_reasons() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )

    storage_models = storage_models_for_scan(result, config_version="test-config")
    payloads = [json.loads(model.reason_json) for model in storage_models]

    assert len(storage_models) == 3
    assert storage_models[0].status == CandidateScanStatus.WATCHLIST.value
    assert storage_models[0].max_loss == Decimal("3.40")
    assert payloads[0]["eligibility_decision"] == "PASS"
    assert payloads[1]["eligibility_decision"] == "NO_TRADE"
    assert payloads[1]["rejection_reasons"] != []


def test_audit_events_for_scan_include_each_candidate_decision() -> None:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )

    audit_events = audit_events_for_scan(result, config_version="test-config")

    assert len(audit_events) == len(result.spreads)
    assert audit_events[0].event_type == "CANDIDATE_WATCHLIST"
    assert audit_events[0].entity_type == "trade_candidate"
    assert audit_events[0].metadata["symbol"] == "SPY"
    assert audit_events[0].metadata["expiration_date"] == "2026-07-24"
    assert audit_events[0].metadata["short_put_strike"] == "540"
    assert audit_events[0].metadata["long_put_strike"] == "535"
    assert audit_events[0].metadata["max_loss"] == "3.40"
    assert audit_events[0].metadata["config_version"] == "test-config"

    rejected_events = [event for event in audit_events if event.event_type == "CANDIDATE_REJECTED"]
    assert len(rejected_events) == len(result.rejected_spreads)
    assert rejected_events[0].metadata["rejection_reason_codes"] != []


def test_persists_scanned_trade_candidates_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )
    storage_models = storage_models_for_scan(result, config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_ids = insert_trade_candidates(connection, storage_models)
        rows = connection.execute(
            """
            SELECT short_put_strike, long_put_strike, max_loss, status, reason_json
            FROM trade_candidates
            ORDER BY id
            """
        ).fetchall()

    assert len(inserted_ids) == 3
    assert rows[0][0:4] == ("540", "535", "3.40", CandidateScanStatus.WATCHLIST.value)
    assert json.loads(rows[0][4])["status"] == CandidateScanStatus.WATCHLIST.value


def test_persists_scan_decision_audit_events_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )
    audit_events = audit_events_for_scan(result, config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_ids = [record_audit_event(connection, event) for event in audit_events]
        rows = connection.execute(
            """
            SELECT event_type, entity_type, payload_json, config_version
            FROM audit_log
            ORDER BY id
            """
        ).fetchall()

    assert len(inserted_ids) == len(result.spreads)
    assert len(rows) == len(result.spreads)
    assert rows[0][0] == "CANDIDATE_WATCHLIST"
    assert rows[0][1] == "trade_candidate"
    assert rows[0][3] == "test-config"

    payloads = [json.loads(row[2])["metadata"] for row in rows]
    assert payloads[0]["status"] == CandidateScanStatus.WATCHLIST.value
    assert payloads[1]["status"] != CandidateScanStatus.WATCHLIST.value
    assert payloads[1]["rejection_reason_codes"] != []


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _evaluated_at() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _quote_timestamp() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _option_chain_snapshot(
    *,
    expiration: date = date(2026, 7, 24),
    short_bid: str = "2.10",
    short_ask: str = "2.20",
    short_delta: str = "-0.18",
    long_bid: str = "0.45",
    long_ask: str = "0.50",
) -> OptionChainSnapshot:
    quotes = (
        _quote(expiration_date=expiration, option_type=OptionType.PUT, strike="540", bid=short_bid, ask=short_ask, delta=short_delta),
        _quote(expiration_date=expiration, option_type=OptionType.PUT, strike="535", bid=long_bid, ask=long_ask, delta="-0.12"),
        _quote(expiration_date=expiration, option_type=OptionType.PUT, strike="530", bid="0.25", ask="0.30", delta="-0.08"),
        _quote(expiration_date=expiration, option_type=OptionType.CALL, strike="560", bid="3.00", ask="3.10", delta="0.32"),
    )
    return OptionChainSnapshot(
        symbol="SPY",
        quote_timestamp=_quote_timestamp(),
        expiration_date=expiration,
        quotes=quotes,
    )


def _quote(
    *,
    expiration_date: date,
    option_type: OptionType,
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
        option_type=option_type,
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
