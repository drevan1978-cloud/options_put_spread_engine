"""Eligibility checks for one proposed defined-risk put credit spread."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from options_engine.config.loader import StrategyDefaults
from options_engine.data.data_quality import DataQualityResult
from options_engine.data.option_chain import OptionQuote, OptionType
from options_engine.regime import RegimeClassification, RegimeLabel
from options_engine.risk import RiskCheckResult
from options_engine.risk.kill_switch import KillSwitchDecision
from options_engine.storage.models import AuditEvent


class EligibilityDecision(StrEnum):
    """Eligibility decision states."""

    PASS = "PASS"
    NO_TRADE = "NO_TRADE"


class EligibilityRejectionCode(StrEnum):
    """Stable rejection codes for spread eligibility failures."""

    BEARISH_OR_UNKNOWN_REGIME = "BEARISH_OR_UNKNOWN_REGIME"
    BID_ASK_WIDTH_TOO_WIDE = "BID_ASK_WIDTH_TOO_WIDE"
    CREDIT_TO_WIDTH_TOO_LOW = "CREDIT_TO_WIDTH_TOO_LOW"
    EXPIRATION_MISMATCH = "EXPIRATION_MISMATCH"
    INVALID_DTE = "INVALID_DTE"
    INVALID_SPREAD_WIDTH = "INVALID_SPREAD_WIDTH"
    NON_POSITIVE_CREDIT = "NON_POSITIVE_CREDIT"
    OPTION_TYPE_MISMATCH = "OPTION_TYPE_MISMATCH"
    QUOTE_TIMESTAMP_MISMATCH = "QUOTE_TIMESTAMP_MISMATCH"
    SHORT_DELTA_OUT_OF_RANGE = "SHORT_DELTA_OUT_OF_RANGE"
    STRIKE_ORDER_INVALID = "STRIKE_ORDER_INVALID"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"


class TradeEligibilityStatus(StrEnum):
    """Final trade eligibility decision status."""

    APPROVED = "APPROVED"
    WATCHLIST = "WATCHLIST"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"


class TradeEligibilityReasonCode(StrEnum):
    """Stable reason codes for final trade eligibility decisions."""

    APPROVED = "APPROVED"
    CANDIDATE_BLOCKED_BY_DATA = "CANDIDATE_BLOCKED_BY_DATA"
    CANDIDATE_BLOCKED_BY_REGIME = "CANDIDATE_BLOCKED_BY_REGIME"
    CANDIDATE_BLOCKED_BY_RISK = "CANDIDATE_BLOCKED_BY_RISK"
    CONTRACTS_LESS_THAN_ONE = "CONTRACTS_LESS_THAN_ONE"
    DATA_QUALITY_FAILED = "DATA_QUALITY_FAILED"
    LIQUIDITY_BLOCKED = "LIQUIDITY_BLOCKED"
    KILL_SWITCH_BLOCKS_NEW_TRADES = "KILL_SWITCH_BLOCKS_NEW_TRADES"
    MISSING_CONTRACTS = "MISSING_CONTRACTS"
    REGIME_NOT_ALLOWED = "REGIME_NOT_ALLOWED"
    RISK_CHECK_FAILED = "RISK_CHECK_FAILED"
    SCANNER_REJECTED = "SCANNER_REJECTED"
    TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"


@dataclass(frozen=True, slots=True)
class PutSpreadCandidate:
    """One proposed put credit spread candidate to evaluate."""

    symbol: str
    short_put: OptionQuote
    long_put: OptionQuote
    evaluated_at: datetime
    regime: RegimeLabel


@dataclass(frozen=True, slots=True)
class EligibilityRejectionReason:
    """One auditable reason a proposed spread is not eligible."""

    code: EligibilityRejectionCode
    message: str
    field: str


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Eligibility result for one proposed spread."""

    decision: EligibilityDecision
    rejection_reasons: tuple[EligibilityRejectionReason, ...]
    details: dict[str, Any]

    @property
    def passed(self) -> bool:
        """Return true when the proposed spread passed eligibility checks."""
        return self.decision == EligibilityDecision.PASS

    @classmethod
    def from_rejections(
        cls,
        rejection_reasons: list[EligibilityRejectionReason],
        details: dict[str, Any],
    ) -> EligibilityResult:
        """Create an eligibility result from rejection reasons."""
        decision = EligibilityDecision.PASS if not rejection_reasons else EligibilityDecision.NO_TRADE
        return cls(decision=decision, rejection_reasons=tuple(rejection_reasons), details=details)


class ScannedSpreadLike(Protocol):
    """Protocol for scanner outputs consumed by final eligibility."""

    candidate: PutSpreadCandidate
    conservative_credit: Decimal
    width: Decimal
    max_loss: Decimal
    breakeven: Decimal
    max_bid_ask_width_pct: Decimal
    credit_to_width: Decimal

    @property
    def status(self) -> Any:
        """Return scanner status."""


@dataclass(frozen=True, slots=True)
class TradeEligibilityDecision:
    """Final auditable trade eligibility decision."""

    status: TradeEligibilityStatus
    reason_codes: tuple[str, ...]
    candidate_id: int | None
    risk_summary: dict[str, object]
    timestamp: datetime

    @property
    def approved(self) -> bool:
        """Return true only for final approved decisions."""
        return self.status == TradeEligibilityStatus.APPROVED

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this final eligibility decision to a structured audit event."""
        if not config_version:
            raise ValueError("config_version is required")
        return AuditEvent(
            event_type=f"TRADE_ELIGIBILITY_{self.status.value}",
            entity_type="trade_eligibility_decision",
            message=f"Trade eligibility decision: {self.status.value}",
            metadata={
                "status": self.status.value,
                "reason_codes": list(self.reason_codes),
                "candidate_id": self.candidate_id,
                "risk_summary": self.risk_summary,
                "timestamp": self.timestamp.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.timestamp,
        )


def evaluate_trade_eligibility(
    scanned_spread: ScannedSpreadLike,
    data_quality: DataQualityResult,
    regime: RegimeClassification | RegimeLabel,
    risk_result: RiskCheckResult,
    contracts: int | None,
    *,
    candidate_id: int | None,
    timestamp: datetime,
    kill_switch: KillSwitchDecision | None = None,
) -> TradeEligibilityDecision:
    """Combine scanner, data-quality, regime, risk, and sizing results into a final decision."""
    reason_codes: list[str] = []
    status = TradeEligibilityStatus.APPROVED

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        reason_codes.append(TradeEligibilityReasonCode.TIMEZONE_REQUIRED.value)
        status = TradeEligibilityStatus.NO_TRADE

    scanner_status = _value(scanned_spread.status)
    regime_label = _regime_label(regime)

    if not data_quality.passed:
        reason_codes.append(TradeEligibilityReasonCode.DATA_QUALITY_FAILED.value)
        reason_codes.extend(reason.code.value for reason in data_quality.rejection_reasons)
        if data_quality.reason_code != "PASS":
            reason_codes.append(data_quality.reason_code)
        status = TradeEligibilityStatus.NO_TRADE

    if regime_label not in {RegimeLabel.GREEN, RegimeLabel.YELLOW}:
        reason_codes.append(TradeEligibilityReasonCode.REGIME_NOT_ALLOWED.value)
        status = TradeEligibilityStatus.NO_TRADE

    if not risk_result.passed:
        reason_codes.append(TradeEligibilityReasonCode.RISK_CHECK_FAILED.value)
        reason_codes.extend(reason.code.value for reason in risk_result.rejection_reasons)
        status = TradeEligibilityStatus.NO_TRADE

    if kill_switch is not None and not kill_switch.allow_new_trades:
        reason_codes.append(TradeEligibilityReasonCode.KILL_SWITCH_BLOCKS_NEW_TRADES.value)
        reason_codes.extend(kill_switch.reason_codes)
        status = TradeEligibilityStatus.NO_TRADE

    if contracts is None:
        reason_codes.append(TradeEligibilityReasonCode.MISSING_CONTRACTS.value)
        if status == TradeEligibilityStatus.APPROVED:
            status = TradeEligibilityStatus.WATCHLIST
    elif contracts < 1:
        reason_codes.append(TradeEligibilityReasonCode.CONTRACTS_LESS_THAN_ONE.value)
        status = TradeEligibilityStatus.NO_TRADE

    if scanner_status == "BLOCKED_BY_REGIME":
        reason_codes.append(TradeEligibilityReasonCode.CANDIDATE_BLOCKED_BY_REGIME.value)
        status = TradeEligibilityStatus.NO_TRADE
    elif scanner_status == "BLOCKED_BY_RISK":
        reason_codes.append(TradeEligibilityReasonCode.CANDIDATE_BLOCKED_BY_RISK.value)
        status = TradeEligibilityStatus.NO_TRADE
    elif scanner_status == "BLOCKED_BY_DATA":
        reason_codes.append(TradeEligibilityReasonCode.CANDIDATE_BLOCKED_BY_DATA.value)
        status = TradeEligibilityStatus.NO_TRADE
    elif scanner_status == "BLOCKED_BY_LIQUIDITY":
        reason_codes.append(TradeEligibilityReasonCode.LIQUIDITY_BLOCKED.value)
        if status == TradeEligibilityStatus.APPROVED:
            status = TradeEligibilityStatus.REJECTED
    elif scanner_status not in {"WATCHLIST", "ELIGIBLE_FOR_REVIEW"}:
        reason_codes.append(TradeEligibilityReasonCode.SCANNER_REJECTED.value)
        if status == TradeEligibilityStatus.APPROVED:
            status = TradeEligibilityStatus.REJECTED

    if status == TradeEligibilityStatus.APPROVED:
        reason_codes.append(TradeEligibilityReasonCode.APPROVED.value)

    return TradeEligibilityDecision(
        status=status,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        candidate_id=candidate_id,
        risk_summary=_risk_summary(
            scanned_spread,
            data_quality,
            regime_label,
            risk_result,
            contracts,
            scanner_status,
            kill_switch,
        ),
        timestamp=timestamp,
    )


def evaluate_eligibility(candidate: PutSpreadCandidate, strategy: StrategyDefaults) -> EligibilityResult:
    """Evaluate whether one proposed put spread is eligible for manual review."""
    rejection_reasons: list[EligibilityRejectionReason] = []
    requested_symbol = candidate.symbol.strip().upper()

    if candidate.evaluated_at.tzinfo is None or candidate.evaluated_at.utcoffset() is None:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.TIMEZONE_REQUIRED,
                "evaluated_at must be timezone-aware",
                "evaluated_at",
            )
        )

    if candidate.regime in {RegimeLabel.BEARISH, RegimeLabel.UNKNOWN}:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.BEARISH_OR_UNKNOWN_REGIME,
                "put credit spreads are not eligible in bearish or unknown regimes",
                "regime",
            )
        )

    if candidate.short_put.symbol != requested_symbol or candidate.long_put.symbol != requested_symbol:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.SYMBOL_MISMATCH,
                "candidate legs must match requested symbol",
                "symbol",
            )
        )

    if candidate.short_put.option_type != OptionType.PUT or candidate.long_put.option_type != OptionType.PUT:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.OPTION_TYPE_MISMATCH,
                "candidate legs must both be puts",
                "option_type",
            )
        )

    if candidate.short_put.expiration_date != candidate.long_put.expiration_date:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.EXPIRATION_MISMATCH,
                "candidate legs must share the same expiration",
                "expiration_date",
            )
        )

    if candidate.short_put.quote_timestamp != candidate.long_put.quote_timestamp:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.QUOTE_TIMESTAMP_MISMATCH,
                "candidate legs must come from the same quote snapshot",
                "quote_timestamp",
            )
        )

    dte = (candidate.short_put.expiration_date - candidate.evaluated_at.date()).days
    if dte < strategy.min_dte or dte > strategy.max_dte:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.INVALID_DTE,
                "candidate expiration is outside configured DTE range",
                "expiration_date",
            )
        )

    if candidate.short_put.strike <= candidate.long_put.strike:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.STRIKE_ORDER_INVALID,
                "short put strike must be above long put strike",
                "strike",
            )
        )

    width = candidate.short_put.strike - candidate.long_put.strike
    if width <= Decimal("0"):
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.INVALID_SPREAD_WIDTH,
                "spread width must be positive",
                "strike",
            )
        )

    short_delta_abs = abs(candidate.short_put.delta)
    if short_delta_abs < strategy.min_short_delta_abs or short_delta_abs > strategy.max_short_delta_abs:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.SHORT_DELTA_OUT_OF_RANGE,
                "absolute short-put delta is outside configured range",
                "short_put.delta",
            )
        )

    short_width_pct = _bid_ask_width_pct(candidate.short_put)
    long_width_pct = _bid_ask_width_pct(candidate.long_put)
    max_leg_width_pct = max(short_width_pct, long_width_pct)
    if max_leg_width_pct > strategy.max_bid_ask_width_pct:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.BID_ASK_WIDTH_TOO_WIDE,
                "one or more option legs exceed configured bid/ask width percentage",
                "bid_ask_width",
            )
        )

    conservative_credit = candidate.short_put.bid - candidate.long_put.ask
    if conservative_credit <= Decimal("0"):
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.NON_POSITIVE_CREDIT,
                "conservative credit must be positive using short bid minus long ask",
                "credit",
            )
        )

    credit_to_width = conservative_credit / width if width > Decimal("0") else Decimal("0")
    if width > Decimal("0") and conservative_credit > Decimal("0") and credit_to_width < strategy.min_credit_to_width:
        rejection_reasons.append(
            _reject(
                EligibilityRejectionCode.CREDIT_TO_WIDTH_TOO_LOW,
                "conservative credit-to-width is below configured minimum",
                "credit_to_width",
            )
        )

    details = {
        "symbol": requested_symbol,
        "regime": candidate.regime.value,
        "dte": dte,
        "short_put_strike": str(candidate.short_put.strike),
        "long_put_strike": str(candidate.long_put.strike),
        "width": str(width),
        "short_delta_abs": str(short_delta_abs),
        "short_bid_ask_width_pct": str(short_width_pct),
        "long_bid_ask_width_pct": str(long_width_pct),
        "max_bid_ask_width_pct": str(max_leg_width_pct),
        "conservative_credit": str(conservative_credit),
        "credit_to_width": str(credit_to_width),
        "min_dte": strategy.min_dte,
        "max_dte": strategy.max_dte,
        "min_short_delta_abs": str(strategy.min_short_delta_abs),
        "max_short_delta_abs": str(strategy.max_short_delta_abs),
        "min_credit_to_width": str(strategy.min_credit_to_width),
        "max_bid_ask_width_pct": str(strategy.max_bid_ask_width_pct),
    }
    return EligibilityResult.from_rejections(rejection_reasons, details)


def _bid_ask_width_pct(quote: OptionQuote) -> Decimal:
    midpoint = (quote.bid + quote.ask) / Decimal("2")
    if midpoint <= Decimal("0"):
        return Decimal("Infinity")
    return (quote.ask - quote.bid) / midpoint


def _reject(
    code: EligibilityRejectionCode,
    message: str,
    field: str,
) -> EligibilityRejectionReason:
    return EligibilityRejectionReason(code=code, message=message, field=field)


def _regime_label(regime: RegimeClassification | RegimeLabel) -> RegimeLabel:
    if isinstance(regime, RegimeClassification):
        return regime.regime
    return regime


def _risk_summary(
    scanned_spread: ScannedSpreadLike,
    data_quality: DataQualityResult,
    regime_label: RegimeLabel,
    risk_result: RiskCheckResult,
    contracts: int | None,
    scanner_status: str,
    kill_switch: KillSwitchDecision | None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "scanner_status": scanner_status,
        "data_quality_passed": data_quality.passed,
        "data_quality_severity": data_quality.severity.value,
        "regime": regime_label.value,
        "risk_passed": risk_result.passed,
        "risk_reason_codes": [reason.code.value for reason in risk_result.rejection_reasons],
        "contracts": contracts,
        "width": str(scanned_spread.width),
        "conservative_credit": str(scanned_spread.conservative_credit),
        "max_loss": str(scanned_spread.max_loss),
        "breakeven": str(scanned_spread.breakeven),
        "max_bid_ask_width_pct": str(scanned_spread.max_bid_ask_width_pct),
        "credit_to_width": str(scanned_spread.credit_to_width),
    }
    if kill_switch is not None:
        summary.update(
            {
                "kill_switch_state": kill_switch.state.value,
                "kill_switch_action": kill_switch.action.value,
                "kill_switch_reason_codes": list(kill_switch.reason_codes),
                "kill_switch_allow_new_trades": kill_switch.allow_new_trades,
                "kill_switch_allow_ticket_generation": kill_switch.allow_ticket_generation,
            }
        )
    return summary


def _value(value: object) -> str:
    return str(getattr(value, "value", value))
