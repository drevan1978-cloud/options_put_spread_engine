from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.data.market_data import PriceBar
from options_engine.storage.database import initialize_database, insert_exit, record_audit_event
from options_engine.storage.models import Position
from options_engine.strategy import (
    ExitAction,
    ExitRecommendationInputs,
    ExitRecommendationPolicy,
    ExitReasonCode,
    ExitReviewPolicy,
    ExitReviewResult,
    audit_events_for_exit_reviews,
    evaluate_exit,
    evaluate_exit_recommendation,
    evaluate_exits,
)
from options_engine.regime import RegimeLabel


def test_exit_review_holds_when_dte_is_above_threshold() -> None:
    evaluated_at = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)

    result = evaluate_exit(
        position=_position(expiration_date=date(2026, 7, 24)),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )

    assert result.action == ExitAction.HOLD
    assert _codes(result) == {ExitReasonCode.HOLD_DTE_ABOVE_THRESHOLD}
    assert result.details["dte"] == 14


def test_exit_review_flags_near_expiration_for_manual_review() -> None:
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)

    result = evaluate_exit(
        position=_position(expiration_date=date(2026, 7, 24)),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )

    assert result.action == ExitAction.REVIEW_EXIT
    assert _codes(result) == {ExitReasonCode.NEAR_EXPIRATION}
    assert result.details["dte"] == 4


def test_exit_review_flags_at_or_past_expiration_for_manual_review() -> None:
    evaluated_at = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)

    result = evaluate_exit(
        position=_position(expiration_date=date(2026, 7, 24)),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )

    assert result.action == ExitAction.REVIEW_EXIT
    assert _codes(result) == {ExitReasonCode.AT_OR_PAST_EXPIRATION}


def test_exit_review_returns_no_action_for_closed_position() -> None:
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)

    result = evaluate_exit(
        position=_position(status="CLOSED", closed_at=evaluated_at - timedelta(days=1)),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )

    assert result.action == ExitAction.NO_ACTION
    assert _codes(result) == {ExitReasonCode.POSITION_ALREADY_CLOSED}


def test_exit_review_returns_no_action_when_current_price_missing() -> None:
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)

    result = evaluate_exit(
        position=_position(),
        evaluated_at=evaluated_at,
        current_price=None,
        policy=_policy(),
    )

    assert result.action == ExitAction.NO_ACTION
    assert _codes(result) == {ExitReasonCode.MISSING_PRICE_DATA}


def test_exit_review_returns_no_action_when_current_price_is_stale() -> None:
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)

    result = evaluate_exit(
        position=_position(),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(hours=1)),
        policy=_policy(),
    )

    assert result.action == ExitAction.NO_ACTION
    assert _codes(result) == {ExitReasonCode.STALE_PRICE_DATA}


def test_evaluate_exits_uses_latest_price_by_symbol() -> None:
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    stale_price = _price(timestamp=evaluated_at - timedelta(hours=1), close=Decimal("548"))
    fresh_price = _price(timestamp=evaluated_at - timedelta(minutes=1), close=Decimal("551"))

    results = evaluate_exits(
        positions=[_position()],
        evaluated_at=evaluated_at,
        current_prices=[stale_price, fresh_price],
        policy=_policy(),
    )

    assert len(results) == 1
    assert results[0].action == ExitAction.REVIEW_EXIT
    assert results[0].details["current_close_price"] == "551"


def test_exit_review_requires_position_id_for_storage() -> None:
    result = evaluate_exit(
        position=_position(id=None),
        evaluated_at=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        current_price=_price(timestamp=datetime(2026, 7, 20, 13, 59, tzinfo=UTC)),
        policy=_policy(),
    )

    with pytest.raises(ValueError, match="position_id is required"):
        result.to_storage_model(config_version="test-config")


def test_persists_exit_review_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    result = evaluate_exit(
        position=_position(id=7),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )
    exit_review = result.to_storage_model(config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_id = insert_exit(connection, exit_review)
        row = connection.execute(
            """
            SELECT position_id, evaluated_at, action, reason_json, config_version
            FROM exits
            WHERE id = ?
            """,
            (inserted_id,),
        ).fetchone()

    assert row[0] == 7
    assert row[1] == "2026-07-20T14:00:00+00:00"
    assert row[2] == ExitAction.REVIEW_EXIT.value
    assert json.loads(row[3])["reasons"][0]["code"] == ExitReasonCode.NEAR_EXPIRATION.value
    assert row[4] == "test-config"


def test_exit_review_produces_audit_event(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    evaluated_at = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    result = evaluate_exit(
        position=_position(id=7),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )

    audit_event = result.to_audit_event(config_version="test-config")

    assert audit_event.event_type == "EXIT_REVIEW_REQUIRED"
    assert audit_event.entity_type == "exit_review"
    assert audit_event.metadata["position_id"] == 7
    assert audit_event.metadata["action"] == ExitAction.REVIEW_EXIT.value
    assert audit_event.metadata["reason_codes"] == [ExitReasonCode.NEAR_EXPIRATION.value]

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute("SELECT event_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "EXIT_REVIEW_REQUIRED"
    assert json.loads(row[1])["metadata"]["reason_codes"] == [ExitReasonCode.NEAR_EXPIRATION.value]


def test_audit_events_for_exit_reviews_includes_hold_and_no_action() -> None:
    evaluated_at = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    hold_result = evaluate_exit(
        position=_position(id=7, expiration_date=date(2026, 7, 24)),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )
    no_action_result = evaluate_exit(
        position=_position(id=8, status="CLOSED", closed_at=evaluated_at - timedelta(days=1)),
        evaluated_at=evaluated_at,
        current_price=_price(timestamp=evaluated_at - timedelta(minutes=1)),
        policy=_policy(),
    )

    audit_events = audit_events_for_exit_reviews([hold_result, no_action_result], config_version="test-config")

    assert [event.event_type for event in audit_events] == ["EXIT_REVIEW_HOLD", "EXIT_REVIEW_NO_ACTION"]


def test_exit_recommendation_holds_when_no_exit_rule_triggers() -> None:
    result = evaluate_exit_recommendation(_exit_inputs(), policy=_recommendation_policy())

    assert result.action == ExitAction.HOLD
    assert _codes(result) == {ExitReasonCode.HOLD_EXIT_CONDITIONS_CLEAR}
    assert result.details["broker_order_submitted"] is False
    assert result.details["live_orders_allowed"] is False


def test_exit_recommendation_takes_profit_at_target() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(current_mark=Decimal("0.70")),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.TAKE_PROFIT
    assert ExitReasonCode.PROFIT_TARGET_HIT in _codes(result)
    assert result.details["profit_pct"] == "0.5625"


def test_exit_recommendation_closes_inside_dte_window() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(evaluated_at=datetime(2026, 7, 16, 14, 0, tzinfo=UTC)),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.CLOSE_POSITION
    assert ExitReasonCode.EXIT_DTE_THRESHOLD in _codes(result)
    assert result.details["dte"] == 8


def test_exit_recommendation_reduces_risk_when_short_delta_doubles() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(current_short_delta_abs=Decimal("0.36")),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.REDUCE_RISK
    assert ExitReasonCode.SHORT_DELTA_DOUBLED in _codes(result)


def test_exit_recommendation_reduces_risk_when_trend_filter_breaks() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(underlying_close=Decimal("535"), trend_filter_price=Decimal("540")),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.REDUCE_RISK
    assert ExitReasonCode.TREND_FILTER_BROKEN in _codes(result)


def test_exit_recommendation_reduces_risk_on_vix_shock_or_red_regime() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(vix_shock=True, regime_state=RegimeLabel.RED),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.REDUCE_RISK
    assert ExitReasonCode.VIX_SHOCK in _codes(result)
    assert ExitReasonCode.REGIME_RED in _codes(result)


def test_exit_recommendation_closes_when_max_loss_threshold_is_hit() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(current_mark=Decimal("4.40")),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.CLOSE_POSITION
    assert ExitReasonCode.MAX_LOSS_THRESHOLD_HIT in _codes(result)
    assert result.details["loss_pct"] == "0.8235294117647058823529411765"


def test_exit_recommendation_uses_kill_switch_exit_as_highest_priority() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(kill_switch_active=True, current_mark=Decimal("4.40")),
        policy=_recommendation_policy(),
    )

    assert result.action == ExitAction.KILL_SWITCH_EXIT
    assert ExitReasonCode.KILL_SWITCH_ACTIVE in _codes(result)
    assert ExitReasonCode.MAX_LOSS_THRESHOLD_HIT in _codes(result)


def test_exit_recommendation_produces_audit_event() -> None:
    result = evaluate_exit_recommendation(
        _exit_inputs(current_mark=Decimal("0.70")),
        policy=_recommendation_policy(),
    )

    audit_event = result.to_audit_event(config_version="test-config", position_id=7)

    assert audit_event.event_type == "EXIT_RECOMMENDATION_TAKE_PROFIT"
    assert audit_event.entity_type == "exit_review"
    assert audit_event.metadata["action"] == ExitAction.TAKE_PROFIT.value
    assert audit_event.metadata["reason_codes"] == [ExitReasonCode.PROFIT_TARGET_HIT.value]


def _policy() -> ExitReviewPolicy:
    return ExitReviewPolicy(expiration_review_dte=7, max_price_age=timedelta(minutes=15))


def _recommendation_policy() -> ExitRecommendationPolicy:
    return ExitRecommendationPolicy(
        profit_take_min_pct=Decimal("0.50"),
        profit_take_max_pct=Decimal("0.70"),
        expiration_close_dte=10,
        delta_multiple_reduce=Decimal("2"),
        max_loss_close_pct=Decimal("0.80"),
    )


def _exit_inputs(
    *,
    evaluated_at: datetime = datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
    entry_credit: Decimal = Decimal("1.60"),
    current_mark: Decimal = Decimal("1.40"),
    initial_short_delta_abs: Decimal = Decimal("0.18"),
    current_short_delta_abs: Decimal = Decimal("0.22"),
    underlying_close: Decimal = Decimal("551"),
    trend_filter_price: Decimal = Decimal("540"),
    regime_state: RegimeLabel = RegimeLabel.GREEN,
    vix_shock: bool = False,
    kill_switch_active: bool = False,
) -> ExitRecommendationInputs:
    return ExitRecommendationInputs(
        position=_position(expiration_date=date(2026, 7, 24)),
        evaluated_at=evaluated_at,
        entry_credit=entry_credit,
        current_mark=current_mark,
        initial_short_delta_abs=initial_short_delta_abs,
        current_short_delta_abs=current_short_delta_abs,
        underlying_close=underlying_close,
        trend_filter_price=trend_filter_price,
        regime_state=regime_state,
        vix_shock=vix_shock,
        kill_switch_active=kill_switch_active,
    )


def _position(
    *,
    id: int | None = 7,
    symbol: str = "SPY",
    expiration_date: date = date(2026, 7, 24),
    status: str = "OPEN",
    closed_at: datetime | None = None,
) -> Position:
    return Position(
        id=id,
        symbol=symbol,
        opened_at=datetime(2026, 6, 19, 15, 1, tzinfo=UTC),
        closed_at=closed_at,
        quantity=1,
        short_put_strike=Decimal("540"),
        long_put_strike=Decimal("535"),
        expiration_date=expiration_date,
        status=status,
        config_version="test-config",
    )


def _price(
    *,
    symbol: str = "SPY",
    timestamp: datetime,
    close: Decimal = Decimal("551"),
) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        timestamp=timestamp,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000,
    )


def _codes(result: ExitReviewResult) -> set[ExitReasonCode]:
    return {reason.code for reason in result.reasons}
