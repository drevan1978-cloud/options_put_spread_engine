from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.data.option_chain import (
    CSVOptionChainProvider,
    OptionChainError,
    OptionType,
    load_option_chain_csv,
)
from options_engine.storage.database import initialize_database, insert_option_chains
from options_engine.storage.models import OptionChain

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_loads_sample_option_chain_csv() -> None:
    snapshots = load_option_chain_csv(FIXTURE_DIR / "sample_option_chain.csv")

    assert len(snapshots) == 2
    spy_snapshot = next(snapshot for snapshot in snapshots if snapshot.symbol == "SPY")
    assert spy_snapshot.expiration_date.isoformat() == "2026-07-24"
    assert len(spy_snapshot.quotes) == 3
    assert spy_snapshot.quotes[0].option_type == OptionType.PUT
    assert spy_snapshot.quotes[0].strike == Decimal("540")
    assert spy_snapshot.quotes[0].bid == Decimal("2.10")
    assert spy_snapshot.quotes[0].ask == Decimal("2.25")
    assert spy_snapshot.quotes[0].mid == Decimal("2.175")
    assert spy_snapshot.quotes[0].dte == 35
    assert spy_snapshot.quotes[0].iv == Decimal("0.1800")
    assert spy_snapshot.quotes[0].delta == Decimal("-0.18")
    assert spy_snapshot.quotes[0].gamma == Decimal("0.0150")
    assert spy_snapshot.quotes[0].theta == Decimal("-0.0800")
    assert spy_snapshot.quotes[0].vega == Decimal("0.1200")
    assert spy_snapshot.quotes[0].open_interest == 4500
    assert spy_snapshot.quotes[0].volume == 120
    assert spy_snapshot.quotes[1].mid == Decimal("1.625")


def test_csv_option_chain_provider_filters_by_symbol() -> None:
    provider = CSVOptionChainProvider(FIXTURE_DIR / "sample_option_chain.csv")

    snapshots = provider.load_option_chain("spy")

    assert len(snapshots) == 1
    assert snapshots[0].symbol == "SPY"
    assert len(snapshots[0].quotes) == 3


def test_persists_loaded_option_chains_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    snapshots = load_option_chain_csv(FIXTURE_DIR / "sample_option_chain.csv")
    option_chains = [
        OptionChain(
            symbol=snapshot.symbol,
            expiration_date=snapshot.expiration_date,
            quote_timestamp=snapshot.quote_timestamp,
            chain_json=snapshot.to_chain_json(),
            source="csv_fixture",
            config_version="test-config",
        )
        for snapshot in snapshots
    ]

    with sqlite3.connect(database_path) as connection:
        inserted_ids = insert_option_chains(connection, option_chains)
        rows = connection.execute(
            """
            SELECT symbol, expiration_date, quote_timestamp, chain_json
            FROM option_chains
            ORDER BY symbol
            """
        ).fetchall()

    assert len(inserted_ids) == 2
    assert rows[0][0] == "QQQ"
    assert rows[0][1] == "2026-07-24"
    assert rows[0][2] == "2026-06-19T14:00:00+00:00"
    assert json.loads(rows[0][3])["quotes"][0]["option_type"] == "PUT"


def test_missing_required_option_chain_column_fails_loudly() -> None:
    with pytest.raises(OptionChainError, match="CSV missing required columns: delta"):
        load_option_chain_csv(FIXTURE_DIR / "missing_columns_option_chain.csv")


def test_malformed_option_chain_row_fails_loudly() -> None:
    with pytest.raises(OptionChainError, match="ask must be greater than or equal to bid"):
        load_option_chain_csv(FIXTURE_DIR / "malformed_option_chain.csv")


def test_missing_bid_fails_loudly() -> None:
    with pytest.raises(OptionChainError, match="row 2: bid is required"):
        load_option_chain_csv(FIXTURE_DIR / "missing_bid_option_chain.csv")


def test_missing_ask_fails_loudly() -> None:
    with pytest.raises(OptionChainError, match="row 2: ask is required"):
        load_option_chain_csv(FIXTURE_DIR / "missing_ask_option_chain.csv")


def test_bad_dte_fails_loudly() -> None:
    with pytest.raises(OptionChainError, match="dte must be non-negative"):
        load_option_chain_csv(FIXTURE_DIR / "bad_dte_option_chain.csv")


def test_bad_option_type_fails_loudly() -> None:
    with pytest.raises(OptionChainError, match="row 2: option_type must be CALL or PUT"):
        load_option_chain_csv(FIXTURE_DIR / "bad_option_type_option_chain.csv")
