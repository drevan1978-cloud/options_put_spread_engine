"""Strategy-adjacent eligibility package.

This package evaluates proposed candidates only; it does not scan for trades.
"""

from options_engine.strategy.eligibility import (
    EligibilityDecision,
    EligibilityRejectionCode,
    EligibilityRejectionReason,
    EligibilityResult,
    PutSpreadCandidate,
    TradeEligibilityDecision,
    TradeEligibilityReasonCode,
    TradeEligibilityStatus,
    evaluate_eligibility,
    evaluate_trade_eligibility,
)
from options_engine.strategy.exits import (
    ExitAction,
    ExitRecommendationInputs,
    ExitRecommendationPolicy,
    ExitReason,
    ExitReasonCode,
    ExitReviewPolicy,
    ExitReviewResult,
    audit_events_for_exit_reviews,
    evaluate_exit,
    evaluate_exit_recommendation,
    evaluate_exits,
)
from options_engine.strategy.spread_scanner import (
    CandidateScanStatus,
    ScannedSpread,
    SpreadScanResult,
    audit_events_for_scan,
    scan_spreads,
    storage_models_for_scan,
)

__all__ = [
    "CandidateScanStatus",
    "EligibilityDecision",
    "EligibilityRejectionCode",
    "EligibilityRejectionReason",
    "EligibilityResult",
    "ExitAction",
    "ExitRecommendationInputs",
    "ExitRecommendationPolicy",
    "ExitReason",
    "ExitReasonCode",
    "ExitReviewPolicy",
    "ExitReviewResult",
    "PutSpreadCandidate",
    "ScannedSpread",
    "SpreadScanResult",
    "TradeEligibilityDecision",
    "TradeEligibilityReasonCode",
    "TradeEligibilityStatus",
    "audit_events_for_scan",
    "audit_events_for_exit_reviews",
    "evaluate_exit",
    "evaluate_exit_recommendation",
    "evaluate_exits",
    "evaluate_eligibility",
    "evaluate_trade_eligibility",
    "scan_spreads",
    "storage_models_for_scan",
]
