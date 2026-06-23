from __future__ import annotations

import sqlite3
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.execution import PositionRecordError, PositionStatus, record_open_position
from options_engine.storage.database import initialize_database, insert_position, record_audit_event
from options_engine.storage.models import Fill, TradeCandidate
from options_engine.strategy import CandidateScanStatus


def test_record_open_position_from_ticket_backed_fill_and_candidate() -> None:
    record = record_open_position(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    assert record.source_ticket_id == 10
    assert record.source_candidate_status == CandidateScanStatus.ELIGIBLE_FOR_REVIEW.value
    assert record.position.symbol == "SPY"
    assert record.position.opened_at == datetime(2026, 6, 19, 15, 1, tzinfo=UTC)
    assert record.position.quantity == 1
    assert record.position.short_put_strike == Decimal("540")
    assert record.position.long_put_strike == Decimal("535")
    assert record.position.expiration_date == date(2026, 7, 24)
    assert record.position.status == PositionStatus.OPEN.value
    assert record.position.config_version == "test-config"


def test_record_open_position_requires_ticket_backed_fill() -> None:
    with pytest.raises(PositionRecordError, match="ticket_id"):
        record_open_position(
            fill=_fill(ticket_id=None, position_id=2),
            trade_candidate=_trade_candidate(),
            config_version="test-config",
        )


def test_record_open_position_rejects_fill_already_tied_to_position() -> None:
    with pytest.raises(PositionRecordError, match="already references"):
        record_open_position(
            fill=_fill(position_id=2),
            trade_candidate=_trade_candidate(),
            config_version="test-config",
        )


def test_record_open_position_rejects_rejected_candidate() -> None:
    with pytest.raises(PositionRecordError, match="eligible trade candidates"):
        record_open_position(
            fill=_fill(),
            trade_candidate=_trade_candidate(status=CandidateScanStatus.REJECTED.value),
            config_version="test-config",
        )


def test_record_open_position_rejects_expired_candidate() -> None:
    with pytest.raises(PositionRecordError, match="expiration must be after fill date"):
        record_open_position(
            fill=_fill(),
            trade_candidate=_trade_candidate(expiration_date=date(2026, 6, 19)),
            config_version="test-config",
        )


def test_persists_recorded_position_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    record = record_open_position(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    with sqlite3.connect(database_path) as connection:
        inserted_id = insert_position(connection, record.position)
        row = connection.execute(
            """
            SELECT symbol, opened_at, closed_at, quantity, short_put_strike,
                   long_put_strike, expiration_date, status, config_version
            FROM positions
            WHERE id = ?
            """,
            (inserted_id,),
        ).fetchone()

    assert row == (
        "SPY",
        "2026-06-19T15:01:00+00:00",
        None,
        1,
        "540",
        "535",
        "2026-07-24",
        PositionStatus.OPEN.value,
        "test-config",
    )


def test_recorded_position_produces_audit_event(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    record = record_open_position(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    audit_event = record.to_audit_event()

    assert audit_event.event_type == "POSITION_RECORDED"
    assert audit_event.entity_type == "position"
    assert audit_event.metadata["source_ticket_id"] == 10
    assert audit_event.metadata["status"] == PositionStatus.OPEN.value

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute("SELECT event_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "POSITION_RECORDED"
    assert json.loads(row[1])["metadata"]["symbol"] == "SPY"


def _fill(ticket_id: int | None = 10, position_id: int | None = None) -> Fill:
    return Fill(
        ticket_id=ticket_id,
        position_id=position_id,
        filled_at=datetime(2026, 6, 19, 15, 1, tzinfo=UTC),
        quantity=1,
        price=Decimal("1.55"),
        source="manual_test",
        config_version="test-config",
    )


def _trade_candidate(
    *,
    status: str = CandidateScanStatus.ELIGIBLE_FOR_REVIEW.value,
    expiration_date: date = date(2026, 7, 24),
) -> TradeCandidate:
    return TradeCandidate(
        symbol="SPY",
        expiration_date=expiration_date,
        short_put_strike=Decimal("540"),
        long_put_strike=Decimal("535"),
        max_loss=Decimal("3.45"),
        status=status,
        reason_json="{}",
        config_version="test-config",
    )
