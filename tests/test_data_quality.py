from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from options_engine.data.data_quality import (
    DataQualityDecision,
    DataQualityPolicy,
    DataQualityRejectionCode,
    DataQualityResult,
    DataQualitySeverity,
    evaluate_required_data_quality,
)
from options_engine.data.market_data import PriceBar
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.storage.database import initialize_database, record_audit_event

_OPEN_POSITIONS_SENTINEL = object()


def test_required_data_quality_passes_when_inputs_are_present_and_fresh() -> None:
    now = datetime(2026, 6, 19, 14, 10, tzinfo=UTC)

    result = _evaluate(now=now)

    assert result.passed is True
    assert result.decision == DataQualityDecision.PASS
    assert result.severity == DataQualitySeverity.INFO
    assert result.reason_code == "PASS"
    assert result.message == "Data quality checks passed"
    assert result.checked_at == now
    assert result.rejection_reasons == ()


def test_missing_required_data_returns_no_trade_reasons() -> None:
    result = _evaluate(price_bars=[], option_chains=[])

    assert result.decision == DataQualityDecision.NO_TRADE
    assert _codes(result) == {
        DataQualityRejectionCode.MISSING_PRICE_DATA,
        DataQualityRejectionCode.MISSING_OPTION_CHAIN_DATA,
    }


def test_stale_price_data_returns_no_trade_reason() -> None:
    now = datetime(2026, 6, 19, 14, 10, tzinfo=UTC)

    result = _evaluate(
        now=now,
        price_bars=[_price_bar(timestamp=now - timedelta(minutes=10))],
        option_chains=[_option_chain(quote_timestamp=now - timedelta(minutes=1))],
    )

    assert _codes(result) == {DataQualityRejectionCode.STALE_PRICE_DATA}


def test_stale_option_chain_data_returns_no_trade_reason() -> None:
    now = datetime(2026, 6, 19, 14, 10, tzinfo=UTC)

    result = _evaluate(
        now=now,
        price_bars=[_price_bar(timestamp=now - timedelta(minutes=1))],
        option_chains=[_option_chain(quote_timestamp=now - timedelta(minutes=10))],
    )

    assert _codes(result) == {DataQualityRejectionCode.STALE_OPTION_CHAIN_DATA}


def test_future_required_data_returns_no_trade_reasons() -> None:
    now = datetime(2026, 6, 19, 14, 10, tzinfo=UTC)

    result = _evaluate(
        now=now,
        price_bars=[_price_bar(timestamp=now + timedelta(minutes=1))],
        option_chains=[_option_chain(quote_timestamp=now + timedelta(minutes=1))],
    )

    assert _codes(result) == {
        DataQualityRejectionCode.FUTURE_PRICE_DATA,
        DataQualityRejectionCode.FUTURE_OPTION_CHAIN_DATA,
    }


def test_missing_field_returns_no_trade_reason() -> None:
    price_bar = _price_bar()
    object.__setattr__(price_bar, "close", None)

    result = _evaluate(price_bars=[price_bar])

    assert DataQualityRejectionCode.MISSING_REQUIRED_FIELD in _codes(result)
    assert result.passed is False


def test_invalid_bid_ask_returns_no_trade_reason() -> None:
    chain = _option_chain()
    object.__setattr__(chain.quotes[0], "bid", Decimal("2.50"))

    result = _evaluate(option_chains=[chain])

    assert DataQualityRejectionCode.INVALID_BID_ASK in _codes(result)


def test_negative_prices_return_no_trade_reason() -> None:
    price_bar = _price_bar()
    object.__setattr__(price_bar, "close", Decimal("-1"))

    result = _evaluate(price_bars=[price_bar])

    assert DataQualityRejectionCode.NEGATIVE_PRICE in _codes(result)


def test_missing_greeks_return_no_trade_reason() -> None:
    chain = _option_chain()
    object.__setattr__(chain.quotes[0], "delta", None)

    result = _evaluate(option_chains=[chain])

    assert DataQualityRejectionCode.MISSING_GREEKS in _codes(result)


def test_missing_account_equity_is_critical_no_trade() -> None:
    result = _evaluate(account_equity=None)

    assert result.decision == DataQualityDecision.NO_TRADE
    assert result.severity == DataQualitySeverity.CRITICAL
    assert result.reason_code == DataQualityRejectionCode.MISSING_ACCOUNT_EQUITY.value
    assert _codes(result) == {DataQualityRejectionCode.MISSING_ACCOUNT_EQUITY}


def test_missing_open_positions_snapshot_returns_no_trade_reason() -> None:
    result = _evaluate(open_positions=None)

    assert DataQualityRejectionCode.MISSING_OPEN_POSITIONS in _codes(result)
    assert result.decision == DataQualityDecision.NO_TRADE


def test_duplicate_quotes_return_no_trade_reason() -> None:
    timestamp = datetime(2026, 6, 19, 14, 9, tzinfo=UTC)
    expiration = date(2026, 7, 24)
    quote = _option_quote(timestamp=timestamp, expiration=expiration)
    chain = OptionChainSnapshot(
        symbol="SPY",
        quote_timestamp=timestamp,
        expiration_date=expiration,
        quotes=(quote, quote),
    )

    result = _evaluate(option_chains=[chain])

    assert DataQualityRejectionCode.DUPLICATE_QUOTES in _codes(result)


def test_timestamp_consistency_failure_returns_no_trade_reason() -> None:
    chain = _option_chain()
    object.__setattr__(chain.quotes[0], "quote_timestamp", chain.quote_timestamp + timedelta(minutes=1))

    result = _evaluate(option_chains=[chain])

    assert DataQualityRejectionCode.TIMESTAMP_TIMEZONE_INCONSISTENT in _codes(result)


def test_naive_now_returns_critical_no_trade_reason() -> None:
    result = _evaluate(
        now=datetime(2026, 6, 19, 14, 10),
        price_bars=[_price_bar(timestamp=datetime(2026, 6, 19, 14, 9, tzinfo=UTC))],
        option_chains=[_option_chain(quote_timestamp=datetime(2026, 6, 19, 14, 9, tzinfo=UTC))],
    )

    assert _codes(result) == {DataQualityRejectionCode.TIMEZONE_REQUIRED}
    assert result.severity == DataQualitySeverity.CRITICAL
    assert result.decision == DataQualityDecision.NO_TRADE


def test_data_for_other_symbol_counts_as_missing_required_data() -> None:
    result = _evaluate(
        price_bars=[_price_bar(symbol="QQQ")],
        option_chains=[_option_chain(symbol="QQQ")],
    )

    assert _codes(result) == {
        DataQualityRejectionCode.MISSING_PRICE_DATA,
        DataQualityRejectionCode.MISSING_OPTION_CHAIN_DATA,
    }


def test_data_quality_failure_produces_persistable_audit_event(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    result = _evaluate(account_equity=None)

    audit_event = result.to_audit_event(config_version="test-config")

    assert audit_event.event_type == "DATA_QUALITY_FAILED"
    assert audit_event.entity_type == "data_quality"
    assert audit_event.metadata["reason_code"] == DataQualityRejectionCode.MISSING_ACCOUNT_EQUITY.value

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute("SELECT event_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "DATA_QUALITY_FAILED"
    assert json.loads(row[1])["metadata"]["passed"] is False


def _evaluate(
    *,
    symbol: str = "SPY",
    now: datetime = datetime(2026, 6, 19, 14, 10, tzinfo=UTC),
    price_bars: list[PriceBar] | None = None,
    option_chains: list[OptionChainSnapshot] | None = None,
    account_equity: Decimal | None = Decimal("100000"),
    open_positions: Any = _OPEN_POSITIONS_SENTINEL,
) -> DataQualityResult:
    resolved_open_positions = [] if open_positions is _OPEN_POSITIONS_SENTINEL else open_positions
    return evaluate_required_data_quality(
        symbol=symbol,
        now=now,
        price_bars=price_bars if price_bars is not None else [_price_bar()],
        option_chains=option_chains if option_chains is not None else [_option_chain()],
        policy=_policy(),
        account_equity=account_equity,
        open_positions=resolved_open_positions,
    )


def _policy() -> DataQualityPolicy:
    return DataQualityPolicy(max_price_age=timedelta(minutes=5), max_option_chain_age=timedelta(minutes=5))


def _price_bar(symbol: str = "SPY", timestamp: datetime | None = None) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        timestamp=timestamp or datetime(2026, 6, 19, 14, 9, tzinfo=UTC),
        open=Decimal("550.10"),
        high=Decimal("552.50"),
        low=Decimal("549.90"),
        close=Decimal("551.25"),
        volume=1_200_000,
    )


def _option_chain(symbol: str = "SPY", quote_timestamp: datetime | None = None) -> OptionChainSnapshot:
    timestamp = quote_timestamp or datetime(2026, 6, 19, 14, 9, tzinfo=UTC)
    expiration = date(2026, 7, 24)
    quote = _option_quote(symbol=symbol, timestamp=timestamp, expiration=expiration)
    return OptionChainSnapshot(
        symbol=symbol,
        quote_timestamp=timestamp,
        expiration_date=expiration,
        quotes=(quote,),
    )


def _option_quote(
    *,
    symbol: str = "SPY",
    timestamp: datetime,
    expiration: date,
) -> OptionQuote:
    return OptionQuote(
        symbol=symbol,
        quote_timestamp=timestamp,
        expiration_date=expiration,
        dte=(expiration - timestamp.date()).days,
        option_type=OptionType.PUT,
        strike=Decimal("540"),
        bid=Decimal("2.10"),
        ask=Decimal("2.25"),
        mid=Decimal("2.175"),
        iv=Decimal("0.1800"),
        delta=Decimal("-0.18"),
        gamma=Decimal("0.0150"),
        theta=Decimal("-0.0800"),
        vega=Decimal("0.1200"),
        volume=120,
        open_interest=4500,
    )


def _codes(result: DataQualityResult) -> set[DataQualityRejectionCode]:
    return {reason.code for reason in result.rejection_reasons}
