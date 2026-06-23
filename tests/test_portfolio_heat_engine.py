from __future__ import annotations

from datetime import date
from decimal import Decimal

from options_engine.config.loader import RiskLimits
from options_engine.risk import (
    PortfolioRiskInput,
    PortfolioRiskPosition,
    RiskCheckResult,
    RiskDecision,
    RiskRejectionCode,
    calculate_portfolio_heat_metrics,
    evaluate_portfolio_risk,
)


def test_calculates_portfolio_heat_metrics_with_projected_trade() -> None:
    portfolio = _portfolio(
        open_positions=(
            _position("SPY", "2026-07-24", "500"),
            _position("QQQ", "2026-08-21", "1000"),
        ),
        proposed_trade=_position("SPY", "2026-07-24", "300"),
        weekly_realized_loss=Decimal("250"),
        monthly_drawdown=Decimal("1000"),
        consecutive_stopped_baskets=1,
    )

    metrics = calculate_portfolio_heat_metrics(portfolio)

    assert metrics.total_open_max_loss == Decimal("1500")
    assert metrics.portfolio_heat_pct == Decimal("0.015")
    assert metrics.risk_by_expiration == {
        date(2026, 7, 24): Decimal("500"),
        date(2026, 8, 21): Decimal("1000"),
    }
    assert metrics.risk_by_underlying == {"QQQ": Decimal("1000"), "SPY": Decimal("500")}
    assert metrics.projected_heat_after_trade == Decimal("1800")
    assert metrics.projected_heat_pct_after_trade == Decimal("0.018")
    assert metrics.projected_risk_by_expiration[date(2026, 7, 24)] == Decimal("800")
    assert metrics.projected_risk_by_underlying["SPY"] == Decimal("800")
    assert metrics.weekly_realized_loss_pct == Decimal("0.0025")
    assert metrics.monthly_drawdown_pct == Decimal("0.01")
    assert metrics.consecutive_stopped_baskets == 1


def test_safe_portfolio_risk_passes_before_approval() -> None:
    result = evaluate_portfolio_risk(_portfolio(), _risk_limits())

    assert result.decision == RiskDecision.PASS
    assert result.rejection_reasons == ()


def test_rejects_projected_portfolio_heat_above_cap() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(
            open_positions=(
                _position("SPY", "2026-07-24", "3000"),
                _position("QQQ", "2026-08-21", "2500"),
            ),
            proposed_trade=_position("DIA", "2026-09-18", "600"),
        ),
        _risk_limits(
            max_risk_per_trade_cluster_pct=Decimal("1"),
            max_risk_per_expiration_pct=Decimal("1"),
            max_total_portfolio_heat_pct=Decimal("0.06"),
        ),
    )

    assert RiskRejectionCode.PORTFOLIO_HEAT_LIMIT_EXCEEDED in _codes(result)


def test_rejects_projected_expiration_risk_above_cap() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(
            open_positions=(_position("SPY", "2026-07-24", "2800"),),
            proposed_trade=_position("QQQ", "2026-07-24", "300"),
        ),
        _risk_limits(
            max_risk_per_trade_cluster_pct=Decimal("1"),
            max_risk_per_expiration_pct=Decimal("0.03"),
            max_total_portfolio_heat_pct=Decimal("1"),
        ),
    )

    assert RiskRejectionCode.EXPIRATION_RISK_LIMIT_EXCEEDED in _codes(result)


def test_rejects_projected_underlying_risk_above_cap() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(
            open_positions=(_position("SPY", "2026-07-24", "800"),),
            proposed_trade=_position("SPY", "2026-08-21", "300"),
        ),
        _risk_limits(
            max_risk_per_trade_cluster_pct=Decimal("0.01"),
            max_risk_per_expiration_pct=Decimal("1"),
            max_total_portfolio_heat_pct=Decimal("1"),
        ),
    )

    assert RiskRejectionCode.UNDERLYING_RISK_LIMIT_EXCEEDED in _codes(result)


def test_rejects_weekly_loss_cap_breached() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(weekly_realized_loss=Decimal("2000")),
        _risk_limits(),
    )

    assert RiskRejectionCode.WEEKLY_LOSS_LIMIT_EXCEEDED in _codes(result)


def test_rejects_monthly_drawdown_cap_breached() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(monthly_drawdown=Decimal("5000")),
        _risk_limits(),
    )

    assert RiskRejectionCode.MONTHLY_DRAWDOWN_LIMIT_EXCEEDED in _codes(result)


def test_rejects_consecutive_stopped_baskets_exceeded() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(consecutive_stopped_baskets=2),
        _risk_limits(),
    )

    assert RiskRejectionCode.CONSECUTIVE_STOPPED_BASKETS_EXCEEDED in _codes(result)


def test_rejects_missing_proposed_trade_before_approval() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(proposed_trade=None, include_default_proposed_trade=False),
        _risk_limits(),
    )

    assert RiskRejectionCode.PROPOSED_MAX_LOSS_REQUIRED in _codes(result)


def test_rejects_invalid_proposed_trade_risk_before_approval() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(proposed_trade=_position("SPY", "2026-07-24", "0")),
        _risk_limits(),
    )

    assert RiskRejectionCode.INVALID_RISK_INPUT in _codes(result)


def test_rejection_reasons_are_returned_for_multiple_caps() -> None:
    result = evaluate_portfolio_risk(
        _portfolio(
            open_positions=(_position("SPY", "2026-07-24", "800"),),
            proposed_trade=_position("SPY", "2026-07-24", "2300"),
            weekly_realized_loss=Decimal("2000"),
        ),
        _risk_limits(
            max_risk_per_trade_cluster_pct=Decimal("0.01"),
            max_risk_per_expiration_pct=Decimal("0.03"),
            max_total_portfolio_heat_pct=Decimal("1"),
        ),
    )

    assert _codes(result) == {
        RiskRejectionCode.UNDERLYING_RISK_LIMIT_EXCEEDED,
        RiskRejectionCode.EXPIRATION_RISK_LIMIT_EXCEEDED,
        RiskRejectionCode.WEEKLY_LOSS_LIMIT_EXCEEDED,
    }


def _portfolio(
    *,
    account_equity: Decimal | None = Decimal("100000"),
    open_positions: tuple[PortfolioRiskPosition, ...] | None = None,
    proposed_trade: PortfolioRiskPosition | None = None,
    include_default_proposed_trade: bool = True,
    weekly_realized_loss: Decimal = Decimal("100"),
    monthly_drawdown: Decimal = Decimal("100"),
    consecutive_stopped_baskets: int = 0,
) -> PortfolioRiskInput:
    return PortfolioRiskInput(
        account_equity=account_equity,
        open_positions=open_positions if open_positions is not None else (_position("SPY", "2026-07-24", "500"),),
        proposed_trade=proposed_trade
        if proposed_trade is not None or not include_default_proposed_trade
        else _position("QQQ", "2026-08-21", "300"),
        weekly_realized_loss=weekly_realized_loss,
        monthly_drawdown=monthly_drawdown,
        consecutive_stopped_baskets=consecutive_stopped_baskets,
    )


def _position(underlying: str, expiration: str, max_loss: str) -> PortfolioRiskPosition:
    return PortfolioRiskPosition(
        underlying=underlying,
        expiration_date=date.fromisoformat(expiration),
        max_loss=Decimal(max_loss),
    )


def _risk_limits(
    *,
    max_risk_per_trade_cluster_pct: Decimal = Decimal("0.01"),
    max_risk_per_expiration_pct: Decimal = Decimal("0.03"),
    max_total_portfolio_heat_pct: Decimal = Decimal("0.06"),
    max_weekly_loss_pct: Decimal = Decimal("0.02"),
    max_monthly_drawdown_pct: Decimal = Decimal("0.05"),
    max_consecutive_stopped_baskets: int = 2,
) -> RiskLimits:
    return RiskLimits(
        max_risk_per_trade_cluster_pct=max_risk_per_trade_cluster_pct,
        max_risk_per_expiration_pct=max_risk_per_expiration_pct,
        max_total_portfolio_heat_pct=max_total_portfolio_heat_pct,
        max_weekly_loss_pct=max_weekly_loss_pct,
        max_monthly_drawdown_pct=max_monthly_drawdown_pct,
        max_consecutive_stopped_baskets=max_consecutive_stopped_baskets,
        allow_martingale=False,
        allow_live_orders=False,
    )


def _codes(result: RiskCheckResult) -> set[RiskRejectionCode]:
    return {reason.code for reason in result.rejection_reasons}
