from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.execution import (
    FillTrackingError,
    ManualFillRecord,
    audit_events_for_fills,
    load_manual_fills_csv,
    track_fills,
)
from options_engine.storage.database import initialize_database, insert_fills, record_audit_event

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_manual_fill_record_converts_to_storage_model() -> None:
    record = ManualFillRecord(
        ticket_id=1,
        position_id=None,
        filled_at=datetime(2026, 6, 19, 15, 1, tzinfo=UTC),
        quantity=1,
        price=Decimal("1.55"),
        source=" manual_entry ",
    )

    fill = record.to_storage_model(config_version="test-config")

    assert fill.ticket_id == 1
    assert fill.position_id is None
    assert fill.price == Decimal("1.55")
    assert fill.source == "manual_entry"
    assert fill.config_version == "test-config"


def test_load_manual_fills_csv_loads_valid_records() -> None:
    records = load_manual_fills_csv(FIXTURE_DIR / "sample_fills.csv")

    assert len(records) == 2
    assert records[0].ticket_id == 1
    assert records[0].position_id is None
    assert records[1].ticket_id is None
    assert records[1].position_id == 2
    assert records[0].filled_at.tzinfo is not None


def test_track_fills_converts_records_to_storage_models() -> None:
    records = load_manual_fills_csv(FIXTURE_DIR / "sample_fills.csv")

    fills = track_fills(records, config_version="test-config")

    assert len(fills) == 2
    assert fills[0].ticket_id == 1
    assert fills[0].config_version == "test-config"
    assert fills[1].position_id == 2


def test_manual_fills_produce_audit_events(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    fills = track_fills(load_manual_fills_csv(FIXTURE_DIR / "sample_fills.csv"), config_version="test-config")

    audit_events = audit_events_for_fills(fills)

    assert len(audit_events) == 2
    assert audit_events[0].event_type == "MANUAL_FILL_RECORDED"
    assert audit_events[0].entity_type == "fill"
    assert audit_events[0].metadata["ticket_id"] == 1
    assert audit_events[0].metadata["price"] == "1.55"

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_events[0])
        row = connection.execute("SELECT event_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "MANUAL_FILL_RECORDED"
    assert json.loads(row[1])["metadata"]["source"] == "manual_csv"


def test_manual_fill_requires_ticket_or_position_reference() -> None:
    with pytest.raises(FillTrackingError, match="ticket_id or position_id"):
        ManualFillRecord(
            ticket_id=None,
            position_id=None,
            filled_at=datetime(2026, 6, 19, 15, 1, tzinfo=UTC),
            quantity=1,
            price=Decimal("1.55"),
            source="manual_entry",
        )


def test_manual_fill_rejects_naive_timestamp() -> None:
    with pytest.raises(FillTrackingError, match="filled_at must be timezone-aware"):
        ManualFillRecord(
            ticket_id=1,
            position_id=None,
            filled_at=datetime(2026, 6, 19, 15, 1),
            quantity=1,
            price=Decimal("1.55"),
            source="manual_entry",
        )


def test_missing_required_fill_csv_column_fails_loudly() -> None:
    with pytest.raises(FillTrackingError, match="CSV missing required columns: price"):
        load_manual_fills_csv(FIXTURE_DIR / "missing_columns_fills.csv")


def test_malformed_fill_csv_row_fails_loudly() -> None:
    with pytest.raises(FillTrackingError, match="ticket_id or position_id"):
        load_manual_fills_csv(FIXTURE_DIR / "malformed_fills.csv")


def test_persists_manual_fills_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    fills = track_fills(load_manual_fills_csv(FIXTURE_DIR / "sample_fills.csv"), config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_ids = insert_fills(connection, fills)
        rows = connection.execute(
            """
            SELECT ticket_id, position_id, filled_at, quantity, price, source, config_version
            FROM fills
            ORDER BY id
            """
        ).fetchall()

    assert len(inserted_ids) == 2
    assert rows[0] == (1, None, "2026-06-19T15:01:00+00:00", 1, "1.55", "manual_csv", "test-config")
    assert rows[1] == (None, 2, "2026-06-19T15:05:00+00:00", 1, "1.60", "manual_csv", "test-config")
