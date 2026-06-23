"""Hard risk-control package."""

from options_engine.config.loader import RiskLimits
from options_engine.risk.kill_switch import (
    KillSwitchAction,
    KillSwitchDecision,
    KillSwitchError,
    KillSwitchInputs,
    KillSwitchReason,
    KillSwitchReasonCode,
    KillSwitchResetResult,
    KillSwitchState,
    evaluate_kill_switch,
    evaluate_kill_switch_state,
    reset_kill_switch,
)
from options_engine.risk.models import (
    RiskCheckResult,
    RiskDecision,
    RiskLimitAmounts,
    RiskRejectionCode,
    RiskRejectionReason,
    RiskState,
    combine_results,
)
from options_engine.risk.portfolio_heat import (
    PortfolioHeatMetrics,
    PortfolioRiskInput,
    PortfolioRiskPosition,
    calculate_portfolio_heat_metrics,
    calculate_projected_portfolio_heat,
    evaluate_portfolio_heat,
    evaluate_portfolio_risk,
)
from options_engine.risk.sizing import calculate_risk_limit_amounts, evaluate_position_sizing
from options_engine.risk.sizing import (
    RiskSizingError,
    calculate_allowed_trade_risk,
    calculate_contracts,
    calculate_max_loss_per_spread,
    validate_no_martingale,
    validate_risk_limits,
)


def evaluate_hard_risk_limits(state: RiskState, risk_limits: RiskLimits) -> RiskCheckResult:
    """Evaluate all hard risk controls before any strategy scanning can proceed."""
    return combine_results(
        (
            evaluate_position_sizing(state, risk_limits),
            evaluate_portfolio_heat(state, risk_limits),
            evaluate_kill_switch(state, risk_limits),
        )
    )


__all__ = [
    "RiskCheckResult",
    "RiskDecision",
    "RiskLimitAmounts",
    "RiskRejectionCode",
    "RiskRejectionReason",
    "RiskSizingError",
    "RiskState",
    "KillSwitchAction",
    "KillSwitchDecision",
    "KillSwitchError",
    "KillSwitchInputs",
    "KillSwitchReason",
    "KillSwitchReasonCode",
    "KillSwitchResetResult",
    "KillSwitchState",
    "PortfolioHeatMetrics",
    "PortfolioRiskInput",
    "PortfolioRiskPosition",
    "calculate_allowed_trade_risk",
    "calculate_contracts",
    "calculate_max_loss_per_spread",
    "calculate_portfolio_heat_metrics",
    "calculate_projected_portfolio_heat",
    "calculate_risk_limit_amounts",
    "evaluate_hard_risk_limits",
    "evaluate_kill_switch",
    "evaluate_kill_switch_state",
    "evaluate_portfolio_heat",
    "evaluate_portfolio_risk",
    "evaluate_position_sizing",
    "reset_kill_switch",
    "validate_no_martingale",
    "validate_risk_limits",
]
