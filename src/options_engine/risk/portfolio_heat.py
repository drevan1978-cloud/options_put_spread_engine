"""Hard portfolio heat risk controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from options_engine.config.loader import RiskLimits
from options_engine.risk.models import (
    RiskCheckResult,
    RiskRejectionCode,
    RiskRejectionReason,
    RiskState,
    reject,
)
from options_engine.risk.sizing import calculate_risk_limit_amounts


@dataclass(frozen=True, slots=True)
class PortfolioRiskPosition:
    """Open or proposed defined-risk exposure used by portfolio heat checks."""

    underlying: str
    expiration_date: date
    max_loss: Decimal

    def __post_init__(self) -> None:
        normalized_underlying = self.underlying.strip().upper()
        if not normalized_underlying:
            raise ValueError("underlying is required")
        object.__setattr__(self, "underlying", normalized_underlying)


@dataclass(frozen=True, slots=True)
class PortfolioRiskInput:
    """Inputs required to evaluate portfolio-level risk before approval."""

    account_equity: Decimal | None
    open_positions: tuple[PortfolioRiskPosition, ...]
    proposed_trade: PortfolioRiskPosition | None
    weekly_realized_loss: Decimal
    monthly_drawdown: Decimal
    consecutive_stopped_baskets: int


@dataclass(frozen=True, slots=True)
class PortfolioHeatMetrics:
    """Portfolio-level heat metrics before and after a proposed trade."""

    total_open_max_loss: Decimal
    portfolio_heat_pct: Decimal
    risk_by_expiration: dict[date, Decimal]
    risk_by_underlying: dict[str, Decimal]
    projected_heat_after_trade: Decimal
    projected_heat_pct_after_trade: Decimal
    projected_risk_by_expiration: dict[date, Decimal]
    projected_risk_by_underlying: dict[str, Decimal]
    weekly_realized_loss_pct: Decimal
    monthly_drawdown_pct: Decimal
    consecutive_stopped_baskets: int


def calculate_projected_portfolio_heat(state: RiskState) -> Decimal:
    """Calculate projected portfolio heat if the proposed max loss were added."""
    proposed_max_loss = state.proposed_max_loss or Decimal("0")
    return state.current_portfolio_heat + proposed_max_loss


def calculate_portfolio_heat_metrics(portfolio: PortfolioRiskInput) -> PortfolioHeatMetrics:
    """Calculate portfolio heat metrics with proposed trade risk included separately."""
    total_open_max_loss = sum((position.max_loss for position in portfolio.open_positions), Decimal("0"))
    risk_by_expiration = _risk_by_expiration(portfolio.open_positions)
    risk_by_underlying = _risk_by_underlying(portfolio.open_positions)

    projected_positions = (
        portfolio.open_positions
        if portfolio.proposed_trade is None
        else (*portfolio.open_positions, portfolio.proposed_trade)
    )
    projected_heat_after_trade = sum((position.max_loss for position in projected_positions), Decimal("0"))
    return PortfolioHeatMetrics(
        total_open_max_loss=total_open_max_loss,
        portfolio_heat_pct=_pct(total_open_max_loss, portfolio.account_equity),
        risk_by_expiration=risk_by_expiration,
        risk_by_underlying=risk_by_underlying,
        projected_heat_after_trade=projected_heat_after_trade,
        projected_heat_pct_after_trade=_pct(projected_heat_after_trade, portfolio.account_equity),
        projected_risk_by_expiration=_risk_by_expiration(projected_positions),
        projected_risk_by_underlying=_risk_by_underlying(projected_positions),
        weekly_realized_loss_pct=_pct(portfolio.weekly_realized_loss, portfolio.account_equity),
        monthly_drawdown_pct=_pct(portfolio.monthly_drawdown, portfolio.account_equity),
        consecutive_stopped_baskets=portfolio.consecutive_stopped_baskets,
    )


def evaluate_portfolio_risk(portfolio: PortfolioRiskInput, risk_limits: RiskLimits) -> RiskCheckResult:
    """Evaluate portfolio-level risk limits including the proposed trade."""
    rejection_reasons: list[RiskRejectionReason] = []
    metrics = calculate_portfolio_heat_metrics(portfolio)

    if portfolio.account_equity is None or portfolio.account_equity <= Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_ACCOUNT_EQUITY,
                "account_equity must be positive",
                "account_equity",
            )
        )
        return RiskCheckResult.from_rejections(rejection_reasons)

    _validate_portfolio_inputs(portfolio, rejection_reasons)
    limit_amounts = calculate_risk_limit_amounts(portfolio.account_equity, risk_limits)

    if metrics.projected_heat_after_trade > limit_amounts.max_portfolio_heat:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.PORTFOLIO_HEAT_LIMIT_EXCEEDED,
                "projected portfolio heat exceeds configured limit",
                "projected_heat_after_trade",
            )
        )

    if portfolio.proposed_trade is not None:
        projected_expiration_risk = metrics.projected_risk_by_expiration[portfolio.proposed_trade.expiration_date]
        if projected_expiration_risk > limit_amounts.max_expiration_risk:
            rejection_reasons.append(
                reject(
                    RiskRejectionCode.EXPIRATION_RISK_LIMIT_EXCEEDED,
                    "projected expiration risk exceeds configured limit",
                    "risk_by_expiration",
                )
            )

        projected_underlying_risk = metrics.projected_risk_by_underlying[portfolio.proposed_trade.underlying]
        if projected_underlying_risk > limit_amounts.max_trade_cluster_risk:
            rejection_reasons.append(
                reject(
                    RiskRejectionCode.UNDERLYING_RISK_LIMIT_EXCEEDED,
                    "projected underlying risk exceeds configured cluster limit",
                    "risk_by_underlying",
                )
            )

    if metrics.weekly_realized_loss_pct >= risk_limits.max_weekly_loss_pct:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.WEEKLY_LOSS_LIMIT_EXCEEDED,
                "weekly realized loss is at or beyond configured limit",
                "weekly_realized_loss",
            )
        )

    if metrics.monthly_drawdown_pct >= risk_limits.max_monthly_drawdown_pct:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.MONTHLY_DRAWDOWN_LIMIT_EXCEEDED,
                "monthly drawdown is at or beyond configured limit",
                "monthly_drawdown",
            )
        )

    if portfolio.consecutive_stopped_baskets >= risk_limits.max_consecutive_stopped_baskets:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.CONSECUTIVE_STOPPED_BASKETS_EXCEEDED,
                "consecutive stopped baskets are at or beyond configured limit",
                "consecutive_stopped_baskets",
            )
        )

    return RiskCheckResult.from_rejections(rejection_reasons)


def evaluate_portfolio_heat(state: RiskState, risk_limits: RiskLimits) -> RiskCheckResult:
    """Evaluate aggregate portfolio heat limits."""
    rejection_reasons: list[RiskRejectionReason] = []
    limit_amounts = calculate_risk_limit_amounts(state.account_equity, risk_limits)

    if state.current_portfolio_heat < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "current_portfolio_heat must be non-negative",
                "current_portfolio_heat",
            )
        )

    projected_heat = calculate_projected_portfolio_heat(state)
    if state.account_equity > Decimal("0") and projected_heat > limit_amounts.max_portfolio_heat:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.PORTFOLIO_HEAT_LIMIT_EXCEEDED,
                "projected portfolio heat exceeds configured limit",
                "current_portfolio_heat",
            )
        )

    return RiskCheckResult.from_rejections(rejection_reasons)


def _validate_portfolio_inputs(
    portfolio: PortfolioRiskInput,
    rejection_reasons: list[RiskRejectionReason],
) -> None:
    for index, position in enumerate(portfolio.open_positions):
        if position.max_loss < Decimal("0"):
            rejection_reasons.append(
                reject(
                    RiskRejectionCode.INVALID_RISK_INPUT,
                    "open position max_loss must be non-negative",
                    f"open_positions[{index}].max_loss",
                )
            )

    if portfolio.proposed_trade is None:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.PROPOSED_MAX_LOSS_REQUIRED,
                "proposed trade risk is required before portfolio approval",
                "proposed_trade",
            )
        )
    elif portfolio.proposed_trade.max_loss <= Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "proposed trade max_loss must be positive",
                "proposed_trade.max_loss",
            )
        )

    if portfolio.weekly_realized_loss < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "weekly_realized_loss must be non-negative",
                "weekly_realized_loss",
            )
        )

    if portfolio.monthly_drawdown < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "monthly_drawdown must be non-negative",
                "monthly_drawdown",
            )
        )

    if portfolio.consecutive_stopped_baskets < 0:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "consecutive_stopped_baskets must be non-negative",
                "consecutive_stopped_baskets",
            )
        )


def _risk_by_expiration(positions: tuple[PortfolioRiskPosition, ...]) -> dict[date, Decimal]:
    risk_by_expiration: dict[date, Decimal] = {}
    for position in positions:
        risk_by_expiration[position.expiration_date] = (
            risk_by_expiration.get(position.expiration_date, Decimal("0")) + position.max_loss
        )
    return dict(sorted(risk_by_expiration.items()))


def _risk_by_underlying(positions: tuple[PortfolioRiskPosition, ...]) -> dict[str, Decimal]:
    risk_by_underlying: dict[str, Decimal] = {}
    for position in positions:
        risk_by_underlying[position.underlying] = risk_by_underlying.get(position.underlying, Decimal("0")) + position.max_loss
    return dict(sorted(risk_by_underlying.items()))


def _pct(value: Decimal, account_equity: Decimal | None) -> Decimal:
    if account_equity is None or account_equity <= Decimal("0"):
        return Decimal("0")
    return value / account_equity
