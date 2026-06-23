from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.data.market_data import CSVMarketDataProvider, MarketDataError, load_ohlcv_csv
from options_engine.storage.database import initialize_database, insert_prices
from options_engine.storage.models import Price

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_loads_sample_ohlcv_csv() -> None:
    bars = load_ohlcv_csv(FIXTURE_DIR / "sample_ohlcv.csv")

    assert len(bars) == 3
    assert bars[0].symbol == "SPY"
    assert bars[0].open == Decimal("550.10")
    assert bars[0].high == Decimal("552.50")
    assert bars[0].low == Decimal("549.90")
    assert bars[0].close == Decimal("551.25")
    assert bars[0].volume == 1_200_000
    assert bars[0].timestamp.tzinfo is not None


def test_csv_market_data_provider_filters_by_symbol() -> None:
    provider = CSVMarketDataProvider(FIXTURE_DIR / "sample_ohlcv.csv")

    bars = provider.load_price_bars("spy")

    assert len(bars) == 2
    assert {bar.symbol for bar in bars} == {"SPY"}


def test_persists_loaded_prices_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    bars = load_ohlcv_csv(FIXTURE_DIR / "sample_ohlcv.csv")
    prices = [
        Price(
            symbol=bar.symbol,
            observed_at=bar.timestamp,
            open_price=bar.open,
            high_price=bar.high,
            low_price=bar.low,
            close_price=bar.close,
            volume=bar.volume,
            source="csv_fixture",
            config_version="test-config",
        )
        for bar in bars
    ]

    with sqlite3.connect(database_path) as connection:
        inserted_ids = insert_prices(connection, prices)
        rows = connection.execute(
            """
            SELECT symbol, observed_at, open_price, high_price, low_price, close_price, volume
            FROM prices
            ORDER BY id
            """
        ).fetchall()

    assert len(inserted_ids) == 3
    assert rows[0] == (
        "SPY",
        "2026-06-19T13:30:00+00:00",
        "550.10",
        "552.50",
        "549.90",
        "551.25",
        1_200_000,
    )


def test_missing_required_csv_column_fails_loudly() -> None:
    with pytest.raises(MarketDataError, match="CSV missing required columns: close"):
        load_ohlcv_csv(FIXTURE_DIR / "missing_columns_ohlcv.csv")


def test_malformed_csv_row_fails_loudly() -> None:
    with pytest.raises(MarketDataError, match="row 2: high is malformed"):
        load_ohlcv_csv(FIXTURE_DIR / "malformed_ohlcv.csv")
