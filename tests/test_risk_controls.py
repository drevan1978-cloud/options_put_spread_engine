from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from options_engine.config.loader import RiskLimits, load_config
from options_engine.risk import (
    RiskCheckResult,
    RiskDecision,
    RiskRejectionCode,
    RiskState,
    calculate_risk_limit_amounts,
    evaluate_hard_risk_limits,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_calculates_account_based_risk_limits() -> None:
    risk_limits = _risk_limits()

    limit_amounts = calculate_risk_limit_amounts(Decimal("100000"), risk_limits)

    assert limit_amounts.max_trade_cluster_risk == Decimal("1000.00")
    assert limit_amounts.max_expiration_risk == Decimal("3000.00")
    assert limit_amounts.max_portfolio_heat == Decimal("6000.00")
    assert limit_amounts.max_weekly_loss == Decimal("2000.00")
    assert limit_amounts.max_monthly_drawdown == Decimal("5000.00")


def test_safe_risk_state_passes_hard_risk_limits() -> None:
    result = evaluate_hard_risk_limits(_state(), _risk_limits())

    assert result.decision == RiskDecision.PASS
    assert result.rejection_reasons == ()


def test_missing_proposed_max_loss_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(_state(proposed_max_loss=None), _risk_limits())

    assert result.decision == RiskDecision.NO_TRADE
    assert _codes(result) == {RiskRejectionCode.PROPOSED_MAX_LOSS_REQUIRED}


def test_cluster_risk_limit_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(
        _state(current_trade_cluster_risk=Decimal("800"), proposed_max_loss=Decimal("300")),
        _risk_limits(),
    )

    assert RiskRejectionCode.CLUSTER_RISK_LIMIT_EXCEEDED in _codes(result)


def test_expiration_risk_limit_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(
        _state(current_expiration_risk=Decimal("2800"), proposed_max_loss=Decimal("300")),
        _risk_limits(),
    )

    assert RiskRejectionCode.EXPIRATION_RISK_LIMIT_EXCEEDED in _codes(result)


def test_portfolio_heat_limit_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(
        _state(current_portfolio_heat=Decimal("5800"), proposed_max_loss=Decimal("300")),
        _risk_limits(),
    )

    assert RiskRejectionCode.PORTFOLIO_HEAT_LIMIT_EXCEEDED in _codes(result)


def test_weekly_loss_limit_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(_state(weekly_realized_loss=Decimal("2000")), _risk_limits())

    assert RiskRejectionCode.WEEKLY_LOSS_LIMIT_EXCEEDED in _codes(result)


def test_monthly_drawdown_limit_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(_state(monthly_drawdown=Decimal("5000")), _risk_limits())

    assert RiskRejectionCode.MONTHLY_DRAWDOWN_LIMIT_EXCEEDED in _codes(result)


def test_consecutive_stopped_baskets_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(_state(consecutive_stopped_baskets=2), _risk_limits())

    assert RiskRejectionCode.CONSECUTIVE_STOPPED_BASKETS_EXCEEDED in _codes(result)


def test_forbidden_controls_return_no_trade_reasons() -> None:
    result = evaluate_hard_risk_limits(
        _state(martingale_requested=True, live_order_requested=True),
        _risk_limits(),
    )

    assert RiskRejectionCode.MARTINGALE_FORBIDDEN in _codes(result)
    assert RiskRejectionCode.LIVE_ORDERS_FORBIDDEN in _codes(result)


def test_invalid_account_equity_returns_no_trade_reason() -> None:
    result = evaluate_hard_risk_limits(_state(account_equity=Decimal("0")), _risk_limits())

    assert RiskRejectionCode.INVALID_ACCOUNT_EQUITY in _codes(result)


def _risk_limits() -> RiskLimits:
    return load_config(PROJECT_ROOT / "config").risk_limits


def _state(**overrides: object) -> RiskState:
    values: dict[str, object] = {
        "account_equity": Decimal("100000"),
        "proposed_max_loss": Decimal("500"),
        "current_trade_cluster_risk": Decimal("250"),
        "current_expiration_risk": Decimal("1000"),
        "current_portfolio_heat": Decimal("1000"),
        "weekly_realized_loss": Decimal("100"),
        "monthly_drawdown": Decimal("100"),
        "consecutive_stopped_baskets": 0,
        "martingale_requested": False,
        "live_order_requested": False,
    }
    values.update(overrides)
    return RiskState(**values)  # type: ignore[arg-type]


def _codes(result: RiskCheckResult) -> set[RiskRejectionCode]:
    return {reason.code for reason in result.rejection_reasons}
