from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.execution import (
    PositionMonitorError,
    PositionMonitorReasonCode,
    PositionReconciliationStatus,
    add_position_from_filled_ticket,
    reconcile_open_positions,
    update_position_mark,
)
from options_engine.regime import RegimeLabel
from options_engine.storage.database import initialize_database, insert_fill, insert_position, record_audit_event
from options_engine.storage.models import Fill, TradeCandidate
from options_engine.strategy import CandidateScanStatus


def test_add_position_from_filled_ticket() -> None:
    record = add_position_from_filled_ticket(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    assert record.position.symbol == "SPY"
    assert record.position.opened_at == datetime(2026, 6, 19, 15, 1, tzinfo=UTC)
    assert record.position.quantity == 1
    assert record.position.short_put_strike == Decimal("540")
    assert record.position.long_put_strike == Decimal("535")
    assert record.position.expiration_date == date(2026, 7, 24)
    assert record.position.status == "OPEN"


def test_add_position_from_filled_ticket_requires_fill_price() -> None:
    with pytest.raises(PositionMonitorError, match="positive fill price"):
        add_position_from_filled_ticket(
            fill=_fill(price="0"),
            trade_candidate=_trade_candidate(),
            config_version="test-config",
        )


def test_update_position_mark_calculates_pnl_dte_delta_regime_and_slippage() -> None:
    record = add_position_from_filled_ticket(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    snapshot = update_position_mark(
        position=record.position,
        fill=_fill(),
        mark_price=Decimal("1.10"),
        theoretical_mid=Decimal("1.60"),
        marked_at=datetime(2026, 6, 24, 14, 0, tzinfo=UTC),
        short_delta=Decimal("-0.16"),
        current_regime=RegimeLabel.GREEN,
    )

    assert snapshot.current_dte == 30
    assert snapshot.fill_slippage == Decimal("-0.05")
    assert snapshot.unrealized_pnl == Decimal("45.00")
    assert snapshot.short_delta == Decimal("-0.16")
    assert snapshot.regime_state == RegimeLabel.GREEN.value
    assert snapshot.multiplier == Decimal("100")


def test_position_mark_snapshot_produces_audit_event(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    record = add_position_from_filled_ticket(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )
    snapshot = update_position_mark(
        position=record.position,
        fill=_fill(),
        mark_price=Decimal("1.10"),
        theoretical_mid=Decimal("1.60"),
        marked_at=datetime(2026, 6, 24, 14, 0, tzinfo=UTC),
        short_delta=Decimal("-0.16"),
        current_regime=RegimeLabel.YELLOW,
    )

    audit_event = snapshot.to_audit_event()

    assert audit_event.event_type == "POSITION_MARK_UPDATED"
    assert audit_event.metadata["fill_slippage"] == "-0.05"
    assert audit_event.metadata["unrealized_pnl"] == "45.00"
    assert audit_event.metadata["regime_state"] == RegimeLabel.YELLOW.value

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute("SELECT event_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "POSITION_MARK_UPDATED"
    assert json.loads(row[1])["metadata"]["short_delta"] == "-0.16"


def test_reconcile_open_positions_verifies_position_with_linked_fill(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    record = add_position_from_filled_ticket(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    with sqlite3.connect(database_path) as connection:
        position_id = insert_position(connection, record.position)
        insert_fill(connection, _fill(ticket_id=None, position_id=position_id))
        result = reconcile_open_positions(
            connection,
            checked_at=datetime(2026, 6, 24, 14, 0, tzinfo=UTC),
            expected_open_position_ids={position_id},
        )

    assert result.status == PositionReconciliationStatus.VERIFIED
    assert result.open_risk_verified is True
    assert result.black_state_required is False
    assert result.reason_codes == (PositionMonitorReasonCode.POSITION_VERIFIED.value,)
    assert result.open_positions[0].id == position_id


def test_reconcile_open_positions_detects_missing_position_data(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        result = reconcile_open_positions(
            connection,
            checked_at=datetime(2026, 6, 24, 14, 0, tzinfo=UTC),
            expected_open_position_ids={99},
        )

    assert result.status == PositionReconciliationStatus.UNVERIFIED
    assert result.open_risk_verified is False
    assert result.black_state_required is True
    assert PositionMonitorReasonCode.EXPECTED_POSITION_NOT_FOUND.value in result.reason_codes
    assert result.missing_position_ids == (99,)


def test_reconcile_open_positions_detects_open_position_without_fill(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    record = add_position_from_filled_ticket(
        fill=_fill(),
        trade_candidate=_trade_candidate(),
        config_version="test-config",
    )

    with sqlite3.connect(database_path) as connection:
        position_id = insert_position(connection, record.position)
        result = reconcile_open_positions(
            connection,
            checked_at=datetime(2026, 6, 24, 14, 0, tzinfo=UTC),
            expected_open_position_ids={position_id},
        )

    assert result.status == PositionReconciliationStatus.UNVERIFIED
    assert result.black_state_required is True
    assert PositionMonitorReasonCode.OPEN_POSITION_FILL_MISSING.value in result.reason_codes


def _fill(
    *,
    price: str = "1.55",
    ticket_id: int | None = 10,
    position_id: int | None = None,
) -> Fill:
    return Fill(
        ticket_id=ticket_id,
        position_id=position_id,
        filled_at=datetime(2026, 6, 19, 15, 1, tzinfo=UTC),
        quantity=1,
        price=Decimal(price),
        source="manual_test",
        config_version="test-config",
    )


def _trade_candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="SPY",
        expiration_date=date(2026, 7, 24),
        short_put_strike=Decimal("540"),
        long_put_strike=Decimal("535"),
        max_loss=Decimal("3.45"),
        status=CandidateScanStatus.ELIGIBLE_FOR_REVIEW.value,
        reason_json="{}",
        config_version="test-config",
    )
