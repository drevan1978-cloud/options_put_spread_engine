"""Typed models for hard risk-control decisions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class RiskDecision(StrEnum):
    """Risk-control decision states."""

    PASS = "PASS"
    NO_TRADE = "NO_TRADE"


class RiskRejectionCode(StrEnum):
    """Stable rejection codes for hard risk-control failures."""

    CLUSTER_RISK_LIMIT_EXCEEDED = "CLUSTER_RISK_LIMIT_EXCEEDED"
    CONSECUTIVE_STOPPED_BASKETS_EXCEEDED = "CONSECUTIVE_STOPPED_BASKETS_EXCEEDED"
    CONTRACTS_LESS_THAN_ONE = "CONTRACTS_LESS_THAN_ONE"
    EXPIRATION_RISK_LIMIT_EXCEEDED = "EXPIRATION_RISK_LIMIT_EXCEEDED"
    INVALID_ACCOUNT_EQUITY = "INVALID_ACCOUNT_EQUITY"
    INVALID_RISK_INPUT = "INVALID_RISK_INPUT"
    LIVE_ORDERS_FORBIDDEN = "LIVE_ORDERS_FORBIDDEN"
    MARTINGALE_FORBIDDEN = "MARTINGALE_FORBIDDEN"
    MONTHLY_DRAWDOWN_LIMIT_EXCEEDED = "MONTHLY_DRAWDOWN_LIMIT_EXCEEDED"
    PORTFOLIO_HEAT_LIMIT_EXCEEDED = "PORTFOLIO_HEAT_LIMIT_EXCEEDED"
    PROPOSED_MAX_LOSS_REQUIRED = "PROPOSED_MAX_LOSS_REQUIRED"
    SIZE_INCREASE_DURING_DRAWDOWN = "SIZE_INCREASE_DURING_DRAWDOWN"
    UNDERLYING_RISK_LIMIT_EXCEEDED = "UNDERLYING_RISK_LIMIT_EXCEEDED"
    WEEKLY_LOSS_LIMIT_EXCEEDED = "WEEKLY_LOSS_LIMIT_EXCEEDED"


@dataclass(frozen=True, slots=True)
class RiskRejectionReason:
    """One auditable reason a request is rejected by hard risk controls."""

    code: RiskRejectionCode
    message: str
    field: str


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Result of one or more hard risk-control checks."""

    decision: RiskDecision
    rejection_reasons: tuple[RiskRejectionReason, ...]

    @property
    def passed(self) -> bool:
        """Return true when no hard risk rule rejected the request."""
        return self.decision == RiskDecision.PASS

    @classmethod
    def pass_result(cls) -> RiskCheckResult:
        """Create a passing risk result."""
        return cls(decision=RiskDecision.PASS, rejection_reasons=())

    @classmethod
    def from_rejections(cls, rejection_reasons: list[RiskRejectionReason]) -> RiskCheckResult:
        """Create a risk result from rejection reasons."""
        if not rejection_reasons:
            return cls.pass_result()
        return cls(decision=RiskDecision.NO_TRADE, rejection_reasons=tuple(rejection_reasons))


@dataclass(frozen=True, slots=True)
class RiskState:
    """Inputs required to evaluate hard pre-trade risk controls."""

    account_equity: Decimal
    proposed_max_loss: Decimal | None
    current_trade_cluster_risk: Decimal
    current_expiration_risk: Decimal
    current_portfolio_heat: Decimal
    weekly_realized_loss: Decimal
    monthly_drawdown: Decimal
    consecutive_stopped_baskets: int
    martingale_requested: bool = False
    live_order_requested: bool = False


@dataclass(frozen=True, slots=True)
class RiskLimitAmounts:
    """Dollar-denominated risk limits derived from account equity."""

    max_trade_cluster_risk: Decimal
    max_expiration_risk: Decimal
    max_portfolio_heat: Decimal
    max_weekly_loss: Decimal
    max_monthly_drawdown: Decimal


def combine_results(results: tuple[RiskCheckResult, ...]) -> RiskCheckResult:
    """Combine risk check results into one auditable decision."""
    rejection_reasons: list[RiskRejectionReason] = []
    for result in results:
        rejection_reasons.extend(result.rejection_reasons)
    return RiskCheckResult.from_rejections(rejection_reasons)


def reject(code: RiskRejectionCode, message: str, field: str) -> RiskRejectionReason:
    """Create one risk rejection reason."""
    return RiskRejectionReason(code=code, message=message, field=field)
