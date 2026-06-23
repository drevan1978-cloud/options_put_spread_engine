"""Hard position sizing risk controls."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from options_engine.config.loader import RiskLimits
from options_engine.risk.models import (
    RiskCheckResult,
    RiskLimitAmounts,
    RiskRejectionCode,
    RiskRejectionReason,
    RiskState,
    reject,
)


class RiskSizingError(ValueError):
    """Raised when risk sizing inputs are unsafe or unauditable."""

    def __init__(self, code: RiskRejectionCode, message: str, field: str) -> None:
        super().__init__(message)
        self.reason = reject(code, message, field)


def calculate_max_loss_per_spread(width: Decimal, credit: Decimal, multiplier: int = 100) -> Decimal:
    """Calculate max loss per spread contract from width, credit, and multiplier."""
    if width <= Decimal("0"):
        raise RiskSizingError(RiskRejectionCode.INVALID_RISK_INPUT, "width must be positive", "width")
    if credit < Decimal("0"):
        raise RiskSizingError(RiskRejectionCode.INVALID_RISK_INPUT, "credit must be non-negative", "credit")
    if multiplier <= 0:
        raise RiskSizingError(RiskRejectionCode.INVALID_RISK_INPUT, "multiplier must be positive", "multiplier")

    max_loss = (width - credit) * Decimal(multiplier)
    if max_loss <= Decimal("0"):
        raise RiskSizingError(
            RiskRejectionCode.INVALID_RISK_INPUT,
            "max loss per spread must be positive",
            "max_loss_per_spread",
        )
    return max_loss


def calculate_allowed_trade_risk(
    account_equity: Decimal | None,
    risk_pct: Decimal,
    regime_multiplier: Decimal,
) -> Decimal:
    """Calculate allowed trade risk in account currency."""
    if account_equity is None:
        raise RiskSizingError(
            RiskRejectionCode.INVALID_ACCOUNT_EQUITY,
            "account_equity is required",
            "account_equity",
        )
    if account_equity <= Decimal("0"):
        raise RiskSizingError(
            RiskRejectionCode.INVALID_ACCOUNT_EQUITY,
            "account_equity must be positive",
            "account_equity",
        )
    if risk_pct <= Decimal("0") or risk_pct > Decimal("1"):
        raise RiskSizingError(RiskRejectionCode.INVALID_RISK_INPUT, "risk_pct must be between 0 and 1", "risk_pct")
    if regime_multiplier < Decimal("0") or regime_multiplier > Decimal("1"):
        raise RiskSizingError(
            RiskRejectionCode.INVALID_RISK_INPUT,
            "regime_multiplier must be between 0 and 1",
            "regime_multiplier",
        )
    return account_equity * risk_pct * regime_multiplier


def calculate_contracts(allowed_risk: Decimal, max_loss_per_spread: Decimal) -> int:
    """Calculate whole contracts allowed by risk budget."""
    if allowed_risk <= Decimal("0"):
        raise RiskSizingError(RiskRejectionCode.INVALID_RISK_INPUT, "allowed_risk must be positive", "allowed_risk")
    if max_loss_per_spread <= Decimal("0"):
        raise RiskSizingError(
            RiskRejectionCode.INVALID_RISK_INPUT,
            "max_loss_per_spread must be positive",
            "max_loss_per_spread",
        )

    contracts = int(allowed_risk // max_loss_per_spread)
    if contracts < 1:
        raise RiskSizingError(
            RiskRejectionCode.CONTRACTS_LESS_THAN_ONE,
            "allowed risk is too small for one contract",
            "allowed_risk",
        )
    return contracts


def validate_no_martingale(config: Any) -> RiskCheckResult:
    """Validate that martingale sizing remains disabled."""
    risk_limits = _risk_limits_from_config(config)
    allow_martingale = bool(getattr(risk_limits, "allow_martingale", False))
    if allow_martingale:
        return RiskCheckResult.from_rejections(
            [
                reject(
                    RiskRejectionCode.MARTINGALE_FORBIDDEN,
                    "martingale sizing is forbidden",
                    "allow_martingale",
                )
            ]
        )
    return RiskCheckResult.pass_result()


def validate_risk_limits(config: Any) -> RiskCheckResult:
    """Validate risk-limit configuration and drawdown sizing constraints."""
    risk_limits = _risk_limits_from_config(config)
    rejection_reasons: list[RiskRejectionReason] = []

    for field_name in (
        "max_risk_per_trade_cluster_pct",
        "max_risk_per_expiration_pct",
        "max_total_portfolio_heat_pct",
        "max_weekly_loss_pct",
        "max_monthly_drawdown_pct",
    ):
        value = getattr(risk_limits, field_name, None)
        if value is None or value <= Decimal("0") or value > Decimal("1"):
            rejection_reasons.append(
                reject(
                    RiskRejectionCode.INVALID_RISK_INPUT,
                    f"{field_name} must be between 0 and 1",
                    field_name,
                )
            )

    max_stopped_baskets = getattr(risk_limits, "max_consecutive_stopped_baskets", None)
    if max_stopped_baskets is None or max_stopped_baskets < 0:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "max_consecutive_stopped_baskets must be non-negative",
                "max_consecutive_stopped_baskets",
            )
        )

    rejection_reasons.extend(validate_no_martingale(config).rejection_reasons)

    if bool(getattr(risk_limits, "allow_live_orders", False)):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.LIVE_ORDERS_FORBIDDEN,
                "live orders are forbidden",
                "allow_live_orders",
            )
        )

    drawdown_active = bool(getattr(config, "drawdown_active", False))
    current_risk_pct = getattr(config, "current_risk_pct", None)
    requested_risk_pct = getattr(config, "requested_risk_pct", None)
    if (
        drawdown_active
        and current_risk_pct is not None
        and requested_risk_pct is not None
        and requested_risk_pct > current_risk_pct
    ):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.SIZE_INCREASE_DURING_DRAWDOWN,
                "size increase during drawdown is forbidden",
                "requested_risk_pct",
            )
        )

    return RiskCheckResult.from_rejections(rejection_reasons)


def calculate_risk_limit_amounts(account_equity: Decimal, risk_limits: RiskLimits) -> RiskLimitAmounts:
    """Calculate dollar-denominated limits from account equity and configured percentages."""
    if account_equity <= Decimal("0"):
        return RiskLimitAmounts(
            max_trade_cluster_risk=Decimal("0"),
            max_expiration_risk=Decimal("0"),
            max_portfolio_heat=Decimal("0"),
            max_weekly_loss=Decimal("0"),
            max_monthly_drawdown=Decimal("0"),
        )

    return RiskLimitAmounts(
        max_trade_cluster_risk=account_equity * risk_limits.max_risk_per_trade_cluster_pct,
        max_expiration_risk=account_equity * risk_limits.max_risk_per_expiration_pct,
        max_portfolio_heat=account_equity * risk_limits.max_total_portfolio_heat_pct,
        max_weekly_loss=account_equity * risk_limits.max_weekly_loss_pct,
        max_monthly_drawdown=account_equity * risk_limits.max_monthly_drawdown_pct,
    )


def evaluate_position_sizing(state: RiskState, risk_limits: RiskLimits) -> RiskCheckResult:
    """Evaluate hard position sizing limits without selecting or ranking trades."""
    rejection_reasons: list[RiskRejectionReason] = []
    limit_amounts = calculate_risk_limit_amounts(state.account_equity, risk_limits)

    if state.account_equity <= Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_ACCOUNT_EQUITY,
                "account_equity must be positive",
                "account_equity",
            )
        )

    if state.proposed_max_loss is None:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.PROPOSED_MAX_LOSS_REQUIRED,
                "proposed_max_loss is required before risk review",
                "proposed_max_loss",
            )
        )
        return RiskCheckResult.from_rejections(rejection_reasons)

    if state.proposed_max_loss <= Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "proposed_max_loss must be positive",
                "proposed_max_loss",
            )
        )

    if state.current_trade_cluster_risk < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "current_trade_cluster_risk must be non-negative",
                "current_trade_cluster_risk",
            )
        )

    if state.current_expiration_risk < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "current_expiration_risk must be non-negative",
                "current_expiration_risk",
            )
        )

    can_compare_percentage_limits = state.account_equity > Decimal("0") and state.proposed_max_loss > Decimal("0")

    projected_cluster_risk = state.current_trade_cluster_risk + state.proposed_max_loss
    if can_compare_percentage_limits and projected_cluster_risk > limit_amounts.max_trade_cluster_risk:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.CLUSTER_RISK_LIMIT_EXCEEDED,
                "projected trade-cluster risk exceeds configured limit",
                "current_trade_cluster_risk",
            )
        )

    projected_expiration_risk = state.current_expiration_risk + state.proposed_max_loss
    if can_compare_percentage_limits and projected_expiration_risk > limit_amounts.max_expiration_risk:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.EXPIRATION_RISK_LIMIT_EXCEEDED,
                "projected expiration risk exceeds configured limit",
                "current_expiration_risk",
            )
        )

    return RiskCheckResult.from_rejections(rejection_reasons)


def _risk_limits_from_config(config: Any) -> Any:
    return getattr(config, "risk_limits", config)
