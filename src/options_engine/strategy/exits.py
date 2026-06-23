"""Deterministic exit review logic for local positions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from options_engine.data.market_data import PriceBar
from options_engine.regime import RegimeLabel
from options_engine.storage.models import AuditEvent, Exit, Position


class ExitAction(StrEnum):
    """Manual exit review actions."""

    HOLD = "HOLD"
    TAKE_PROFIT = "TAKE_PROFIT"
    REDUCE_RISK = "REDUCE_RISK"
    CLOSE_POSITION = "CLOSE_POSITION"
    KILL_SWITCH_EXIT = "KILL_SWITCH_EXIT"
    REVIEW_EXIT = "REVIEW_EXIT"
    NO_ACTION = "NO_ACTION"


class ExitReasonCode(StrEnum):
    """Stable reason codes for exit review decisions."""

    AT_OR_PAST_EXPIRATION = "AT_OR_PAST_EXPIRATION"
    EXIT_DTE_THRESHOLD = "EXIT_DTE_THRESHOLD"
    FUTURE_PRICE_DATA = "FUTURE_PRICE_DATA"
    HOLD_EXIT_CONDITIONS_CLEAR = "HOLD_EXIT_CONDITIONS_CLEAR"
    HOLD_DTE_ABOVE_THRESHOLD = "HOLD_DTE_ABOVE_THRESHOLD"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    MAX_LOSS_THRESHOLD_HIT = "MAX_LOSS_THRESHOLD_HIT"
    MISSING_PRICE_DATA = "MISSING_PRICE_DATA"
    NEAR_EXPIRATION = "NEAR_EXPIRATION"
    POSITION_ALREADY_CLOSED = "POSITION_ALREADY_CLOSED"
    PROFIT_TARGET_HIT = "PROFIT_TARGET_HIT"
    REGIME_RED = "REGIME_RED"
    SHORT_DELTA_DOUBLED = "SHORT_DELTA_DOUBLED"
    STALE_PRICE_DATA = "STALE_PRICE_DATA"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"
    TREND_FILTER_BROKEN = "TREND_FILTER_BROKEN"
    VIX_SHOCK = "VIX_SHOCK"


@dataclass(frozen=True, slots=True)
class ExitReviewPolicy:
    """Policy thresholds for deterministic exit review."""

    expiration_review_dte: int = 7
    max_price_age: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.expiration_review_dte < 0:
            raise ValueError("expiration_review_dte must be non-negative")
        if self.max_price_age <= timedelta(0):
            raise ValueError("max_price_age must be positive")


@dataclass(frozen=True, slots=True)
class ExitRecommendationPolicy:
    """Policy thresholds for deterministic exit recommendations."""

    profit_take_min_pct: Decimal = Decimal("0.50")
    profit_take_max_pct: Decimal = Decimal("0.70")
    expiration_close_dte: int = 10
    delta_multiple_reduce: Decimal = Decimal("2")
    max_loss_close_pct: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        if self.profit_take_min_pct <= Decimal("0") or self.profit_take_min_pct > Decimal("1"):
            raise ValueError("profit_take_min_pct must be between 0 and 1")
        if self.profit_take_max_pct < self.profit_take_min_pct or self.profit_take_max_pct > Decimal("1"):
            raise ValueError("profit_take_max_pct must be between profit_take_min_pct and 1")
        if self.expiration_close_dte < 0:
            raise ValueError("expiration_close_dte must be non-negative")
        if self.delta_multiple_reduce <= Decimal("1"):
            raise ValueError("delta_multiple_reduce must be greater than 1")
        if self.max_loss_close_pct <= Decimal("0") or self.max_loss_close_pct > Decimal("1"):
            raise ValueError("max_loss_close_pct must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ExitRecommendationInputs:
    """Inputs for the Milestone 14 deterministic exit engine."""

    position: Position
    evaluated_at: datetime
    entry_credit: Decimal
    current_mark: Decimal
    initial_short_delta_abs: Decimal
    current_short_delta_abs: Decimal
    underlying_close: Decimal
    trend_filter_price: Decimal
    regime_state: RegimeLabel | str
    vix_shock: bool = False
    kill_switch_active: bool = False
    multiplier: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        if _is_naive(self.evaluated_at):
            raise ValueError("evaluated_at must be timezone-aware")
        if _is_naive(self.position.opened_at):
            raise ValueError("position.opened_at must be timezone-aware")
        if self.entry_credit <= Decimal("0"):
            raise ValueError("entry_credit must be positive")
        if self.current_mark < Decimal("0"):
            raise ValueError("current_mark must be non-negative")
        if self.initial_short_delta_abs <= Decimal("0") or self.initial_short_delta_abs > Decimal("1"):
            raise ValueError("initial_short_delta_abs must be between 0 and 1")
        if self.current_short_delta_abs < Decimal("0") or self.current_short_delta_abs > Decimal("1"):
            raise ValueError("current_short_delta_abs must be between 0 and 1")
        if self.underlying_close <= Decimal("0"):
            raise ValueError("underlying_close must be positive")
        if self.trend_filter_price <= Decimal("0"):
            raise ValueError("trend_filter_price must be positive")
        if self.multiplier <= Decimal("0"):
            raise ValueError("multiplier must be positive")


@dataclass(frozen=True, slots=True)
class ExitReason:
    """One auditable reason attached to an exit review."""

    code: ExitReasonCode
    message: str
    field: str

    def to_dict(self) -> dict[str, str]:
        """Serialize the exit reason for audit storage."""
        return {
            "code": self.code.value,
            "message": self.message,
            "field": self.field,
        }


@dataclass(frozen=True, slots=True)
class ExitReviewResult:
    """Auditable exit review result for one local position."""

    position: Position
    evaluated_at: datetime
    action: ExitAction
    reasons: tuple[ExitReason, ...]
    details: dict[str, Any]

    def reason_json(self) -> str:
        """Serialize decision details and reasons for storage."""
        payload = {
            "action": self.action.value,
            "details": self.details,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }
        return json.dumps(payload, sort_keys=True)

    def to_storage_model(self, config_version: str, position_id: int | None = None) -> Exit:
        """Convert this review result to the persistent exit review model."""
        resolved_position_id = position_id if position_id is not None else self.position.id
        if resolved_position_id is None:
            raise ValueError("position_id is required to store an exit review")
        if not config_version:
            raise ValueError("config_version is required")
        return Exit(
            position_id=resolved_position_id,
            evaluated_at=self.evaluated_at,
            action=self.action.value,
            reason_json=self.reason_json(),
            config_version=config_version,
        )

    def to_audit_event(self, config_version: str, position_id: int | None = None) -> AuditEvent:
        """Convert this exit review result to a structured audit event."""
        resolved_position_id = position_id if position_id is not None else self.position.id
        if resolved_position_id is None:
            raise ValueError("position_id is required to audit an exit review")
        if not config_version:
            raise ValueError("config_version is required")
        return AuditEvent(
            event_type=_exit_event_type(self.action),
            entity_type="exit_review",
            message=_exit_event_message(self.action),
            metadata={
                "position_id": resolved_position_id,
                "symbol": self.position.symbol.strip().upper(),
                "action": self.action.value,
                "reason_codes": [reason.code.value for reason in self.reasons],
                "details": self.details,
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.evaluated_at,
        )


def evaluate_exit(
    position: Position,
    evaluated_at: datetime,
    current_price: PriceBar | None,
    policy: ExitReviewPolicy | None = None,
) -> ExitReviewResult:
    """Evaluate one local position for manual exit review."""
    active_policy = policy or ExitReviewPolicy()
    reasons: list[ExitReason] = []

    if _is_naive(evaluated_at):
        reasons.append(_reason(ExitReasonCode.TIMEZONE_REQUIRED, "evaluated_at must be timezone-aware", "evaluated_at"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy)

    if _is_naive(position.opened_at):
        reasons.append(_reason(ExitReasonCode.TIMEZONE_REQUIRED, "position.opened_at must be timezone-aware", "opened_at"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy)

    if position.closed_at is not None or position.status.upper() == "CLOSED":
        reasons.append(_reason(ExitReasonCode.POSITION_ALREADY_CLOSED, "position is already closed", "status"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy)

    if current_price is None:
        reasons.append(_reason(ExitReasonCode.MISSING_PRICE_DATA, "current price data is required", "current_price"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy)

    if current_price.symbol != position.symbol.strip().upper():
        reasons.append(_reason(ExitReasonCode.SYMBOL_MISMATCH, "current price symbol must match position", "symbol"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy, current_price)

    if current_price.timestamp > evaluated_at:
        reasons.append(_reason(ExitReasonCode.FUTURE_PRICE_DATA, "current price timestamp is in the future", "current_price"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy, current_price)

    if evaluated_at - current_price.timestamp > active_policy.max_price_age:
        reasons.append(_reason(ExitReasonCode.STALE_PRICE_DATA, "current price data is stale", "current_price"))
        return _result(position, evaluated_at, ExitAction.NO_ACTION, reasons, active_policy, current_price)

    dte = (position.expiration_date - evaluated_at.date()).days
    if dte <= 0:
        reasons.append(_reason(ExitReasonCode.AT_OR_PAST_EXPIRATION, "position is at or past expiration", "expiration_date"))
        return _result(position, evaluated_at, ExitAction.REVIEW_EXIT, reasons, active_policy, current_price)

    if dte <= active_policy.expiration_review_dte:
        reasons.append(_reason(ExitReasonCode.NEAR_EXPIRATION, "position is near expiration", "expiration_date"))
        return _result(position, evaluated_at, ExitAction.REVIEW_EXIT, reasons, active_policy, current_price)

    reasons.append(_reason(ExitReasonCode.HOLD_DTE_ABOVE_THRESHOLD, "position DTE is above review threshold", "expiration_date"))
    return _result(position, evaluated_at, ExitAction.HOLD, reasons, active_policy, current_price)


def evaluate_exit_recommendation(
    inputs: ExitRecommendationInputs,
    policy: ExitRecommendationPolicy | None = None,
) -> ExitReviewResult:
    """Recommend a manual exit action without placing or routing orders."""
    active_policy = policy or ExitRecommendationPolicy()
    reasons = _recommendation_reasons(inputs, active_policy)
    action = _recommended_action(reasons)
    if not reasons:
        reasons.append(
            _reason(
                ExitReasonCode.HOLD_EXIT_CONDITIONS_CLEAR,
                "no configured exit condition is active",
                "exit_conditions",
            )
        )
    return ExitReviewResult(
        position=inputs.position,
        evaluated_at=inputs.evaluated_at,
        action=action,
        reasons=tuple(reasons),
        details=_recommendation_details(inputs, active_policy),
    )


def evaluate_exits(
    positions: list[Position],
    evaluated_at: datetime,
    current_prices: list[PriceBar],
    policy: ExitReviewPolicy | None = None,
) -> list[ExitReviewResult]:
    """Evaluate multiple local positions for manual exit review."""
    latest_prices = _latest_prices_by_symbol(current_prices)
    return [
        evaluate_exit(
            position=position,
            evaluated_at=evaluated_at,
            current_price=latest_prices.get(position.symbol.strip().upper()),
            policy=policy,
        )
        for position in positions
    ]


def audit_events_for_exit_reviews(results: list[ExitReviewResult], config_version: str) -> list[AuditEvent]:
    """Convert exit review results to structured audit events."""
    return [result.to_audit_event(config_version) for result in results]


def _recommendation_reasons(
    inputs: ExitRecommendationInputs,
    policy: ExitRecommendationPolicy,
) -> list[ExitReason]:
    if inputs.position.closed_at is not None or inputs.position.status.upper() == "CLOSED":
        return [
            _reason(
                ExitReasonCode.POSITION_ALREADY_CLOSED,
                "position is already closed",
                "status",
            )
        ]

    reasons: list[ExitReason] = []
    dte = _current_dte(inputs)
    profit_pct = _profit_pct(inputs)
    loss_pct = _loss_pct(inputs)
    regime_state = _regime_value(inputs.regime_state)

    if inputs.kill_switch_active:
        reasons.append(_reason(ExitReasonCode.KILL_SWITCH_ACTIVE, "kill switch is active", "kill_switch_active"))
    if loss_pct >= policy.max_loss_close_pct:
        reasons.append(_reason(ExitReasonCode.MAX_LOSS_THRESHOLD_HIT, "max loss threshold is hit", "current_mark"))
    if dte <= policy.expiration_close_dte:
        reasons.append(_reason(ExitReasonCode.EXIT_DTE_THRESHOLD, "position is inside close-by-DTE threshold", "expiration_date"))
    if inputs.current_short_delta_abs >= inputs.initial_short_delta_abs * policy.delta_multiple_reduce:
        reasons.append(_reason(ExitReasonCode.SHORT_DELTA_DOUBLED, "short strike delta has doubled", "current_short_delta_abs"))
    if inputs.underlying_close < inputs.trend_filter_price:
        reasons.append(_reason(ExitReasonCode.TREND_FILTER_BROKEN, "underlying broke the trend filter", "underlying_close"))
    if inputs.vix_shock:
        reasons.append(_reason(ExitReasonCode.VIX_SHOCK, "VIX shock flag is active", "vix_shock"))
    if regime_state == RegimeLabel.RED.value:
        reasons.append(_reason(ExitReasonCode.REGIME_RED, "current regime is RED", "regime_state"))
    if profit_pct >= policy.profit_take_min_pct:
        reasons.append(_reason(ExitReasonCode.PROFIT_TARGET_HIT, "profit target threshold is hit", "current_mark"))

    return reasons


def _recommended_action(reasons: list[ExitReason]) -> ExitAction:
    reason_codes = {reason.code for reason in reasons}
    if ExitReasonCode.KILL_SWITCH_ACTIVE in reason_codes:
        return ExitAction.KILL_SWITCH_EXIT
    if reason_codes.intersection({ExitReasonCode.MAX_LOSS_THRESHOLD_HIT, ExitReasonCode.EXIT_DTE_THRESHOLD}):
        return ExitAction.CLOSE_POSITION
    if reason_codes.intersection(
        {
            ExitReasonCode.SHORT_DELTA_DOUBLED,
            ExitReasonCode.TREND_FILTER_BROKEN,
            ExitReasonCode.VIX_SHOCK,
            ExitReasonCode.REGIME_RED,
        }
    ):
        return ExitAction.REDUCE_RISK
    if ExitReasonCode.PROFIT_TARGET_HIT in reason_codes:
        return ExitAction.TAKE_PROFIT
    return ExitAction.HOLD


def _recommendation_details(
    inputs: ExitRecommendationInputs,
    policy: ExitRecommendationPolicy,
) -> dict[str, Any]:
    return {
        "symbol": inputs.position.symbol.strip().upper(),
        "position_status": inputs.position.status,
        "opened_at": inputs.position.opened_at.isoformat(),
        "closed_at": None if inputs.position.closed_at is None else inputs.position.closed_at.isoformat(),
        "expiration_date": inputs.position.expiration_date.isoformat(),
        "dte": _current_dte(inputs),
        "entry_credit": str(inputs.entry_credit),
        "current_mark": str(inputs.current_mark),
        "unrealized_pnl": str(_unrealized_pnl(inputs)),
        "profit_pct": str(_profit_pct(inputs)),
        "loss_pct": str(_loss_pct(inputs)),
        "max_profit": str(_max_profit(inputs)),
        "max_loss": str(_max_loss(inputs)),
        "initial_short_delta_abs": str(inputs.initial_short_delta_abs),
        "current_short_delta_abs": str(inputs.current_short_delta_abs),
        "underlying_close": str(inputs.underlying_close),
        "trend_filter_price": str(inputs.trend_filter_price),
        "regime_state": _regime_value(inputs.regime_state),
        "vix_shock": inputs.vix_shock,
        "kill_switch_active": inputs.kill_switch_active,
        "multiplier": str(inputs.multiplier),
        "profit_take_min_pct": str(policy.profit_take_min_pct),
        "profit_take_max_pct": str(policy.profit_take_max_pct),
        "expiration_close_dte": policy.expiration_close_dte,
        "delta_multiple_reduce": str(policy.delta_multiple_reduce),
        "max_loss_close_pct": str(policy.max_loss_close_pct),
        "broker_order_submitted": False,
        "live_orders_allowed": False,
    }


def _latest_prices_by_symbol(current_prices: list[PriceBar]) -> dict[str, PriceBar]:
    latest_prices: dict[str, PriceBar] = {}
    for price in current_prices:
        existing = latest_prices.get(price.symbol)
        if existing is None or price.timestamp > existing.timestamp:
            latest_prices[price.symbol] = price
    return latest_prices


def _result(
    position: Position,
    evaluated_at: datetime,
    action: ExitAction,
    reasons: list[ExitReason],
    policy: ExitReviewPolicy,
    current_price: PriceBar | None = None,
) -> ExitReviewResult:
    details = {
        "symbol": position.symbol.strip().upper(),
        "position_status": position.status,
        "opened_at": position.opened_at.isoformat(),
        "closed_at": None if position.closed_at is None else position.closed_at.isoformat(),
        "expiration_date": position.expiration_date.isoformat(),
        "dte": (position.expiration_date - evaluated_at.date()).days,
        "expiration_review_dte": policy.expiration_review_dte,
        "max_price_age_seconds": int(policy.max_price_age.total_seconds()),
        "current_price_timestamp": None if current_price is None else current_price.timestamp.isoformat(),
        "current_close_price": None if current_price is None else str(current_price.close),
    }
    return ExitReviewResult(
        position=position,
        evaluated_at=evaluated_at,
        action=action,
        reasons=tuple(reasons),
        details=details,
    )


def _reason(code: ExitReasonCode, message: str, field: str) -> ExitReason:
    return ExitReason(code=code, message=message, field=field)


def _is_naive(value: datetime) -> bool:
    return value.tzinfo is None or value.utcoffset() is None


def _exit_event_type(action: ExitAction) -> str:
    match action:
        case ExitAction.KILL_SWITCH_EXIT:
            return "EXIT_RECOMMENDATION_KILL_SWITCH_EXIT"
        case ExitAction.CLOSE_POSITION:
            return "EXIT_RECOMMENDATION_CLOSE_POSITION"
        case ExitAction.REDUCE_RISK:
            return "EXIT_RECOMMENDATION_REDUCE_RISK"
        case ExitAction.TAKE_PROFIT:
            return "EXIT_RECOMMENDATION_TAKE_PROFIT"
        case ExitAction.REVIEW_EXIT:
            return "EXIT_REVIEW_REQUIRED"
        case ExitAction.HOLD:
            return "EXIT_REVIEW_HOLD"
        case ExitAction.NO_ACTION:
            return "EXIT_REVIEW_NO_ACTION"
        case _:
            raise ValueError(f"unsupported exit action: {action}")


def _exit_event_message(action: ExitAction) -> str:
    match action:
        case ExitAction.KILL_SWITCH_EXIT:
            return "Exit recommendation: kill-switch exit required"
        case ExitAction.CLOSE_POSITION:
            return "Exit recommendation: close position"
        case ExitAction.REDUCE_RISK:
            return "Exit recommendation: reduce risk"
        case ExitAction.TAKE_PROFIT:
            return "Exit recommendation: take profit"
        case ExitAction.REVIEW_EXIT:
            return "Exit review requires manual attention"
        case ExitAction.HOLD:
            return "Exit review indicates hold"
        case ExitAction.NO_ACTION:
            return "Exit review produced no action"
        case _:
            raise ValueError(f"unsupported exit action: {action}")


def _current_dte(inputs: ExitRecommendationInputs) -> int:
    return max((inputs.position.expiration_date - inputs.evaluated_at.date()).days, 0)


def _unrealized_pnl(inputs: ExitRecommendationInputs) -> Decimal:
    return (inputs.entry_credit - inputs.current_mark) * Decimal(inputs.position.quantity) * inputs.multiplier


def _max_profit(inputs: ExitRecommendationInputs) -> Decimal:
    return inputs.entry_credit * Decimal(inputs.position.quantity) * inputs.multiplier


def _max_loss(inputs: ExitRecommendationInputs) -> Decimal:
    spread_width = inputs.position.short_put_strike - inputs.position.long_put_strike
    max_loss_per_spread = spread_width - inputs.entry_credit
    if spread_width <= Decimal("0") or max_loss_per_spread <= Decimal("0"):
        raise ValueError("position strikes and entry_credit must produce positive max loss")
    return max_loss_per_spread * Decimal(inputs.position.quantity) * inputs.multiplier


def _profit_pct(inputs: ExitRecommendationInputs) -> Decimal:
    max_profit = _max_profit(inputs)
    if max_profit <= Decimal("0"):
        return Decimal("0")
    return max(_unrealized_pnl(inputs), Decimal("0")) / max_profit


def _loss_pct(inputs: ExitRecommendationInputs) -> Decimal:
    max_loss = _max_loss(inputs)
    if max_loss <= Decimal("0"):
        return Decimal("0")
    return max(-_unrealized_pnl(inputs), Decimal("0")) / max_loss


def _regime_value(regime: RegimeLabel | str) -> str:
    value = regime.value if isinstance(regime, RegimeLabel) else regime.strip().upper()
    allowed_values = {label.value for label in RegimeLabel}
    if value not in allowed_values:
        raise ValueError("regime_state must be a known regime state")
    return value
