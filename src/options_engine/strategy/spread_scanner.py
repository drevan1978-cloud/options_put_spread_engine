"""Deterministic put spread scanner over already-loaded option chains."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from options_engine.config.loader import StrategyDefaults
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.regime import RegimeLabel
from options_engine.risk.kill_switch import KillSwitchDecision
from options_engine.storage.models import AuditEvent, TradeCandidate
from options_engine.strategy.eligibility import (
    EligibilityDecision,
    EligibilityResult,
    PutSpreadCandidate,
    evaluate_eligibility,
)


class CandidateScanStatus(StrEnum):
    """Status for a scanned spread candidate."""

    WATCHLIST = "WATCHLIST"
    REJECTED = "REJECTED"
    BLOCKED_BY_REGIME = "BLOCKED_BY_REGIME"
    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"
    BLOCKED_BY_LIQUIDITY = "BLOCKED_BY_LIQUIDITY"
    BLOCKED_BY_DATA = "BLOCKED_BY_DATA"

    ELIGIBLE_FOR_REVIEW = "WATCHLIST"


@dataclass(frozen=True, slots=True)
class ScannedSpread:
    """One enumerated spread and its eligibility audit result."""

    candidate: PutSpreadCandidate
    eligibility: EligibilityResult
    conservative_credit: Decimal
    width: Decimal
    max_loss: Decimal
    breakeven: Decimal
    max_bid_ask_width_pct: Decimal
    credit_to_width: Decimal
    scan_status: CandidateScanStatus

    @property
    def status(self) -> CandidateScanStatus:
        """Return candidate scan status."""
        return self.scan_status

    def reason_json(self) -> str:
        """Serialize eligibility details and rejection reasons for audit storage."""
        payload = {
            "status": self.status.value,
            "eligibility_decision": self.eligibility.decision.value,
            "details": self.eligibility.details,
            "scanner_metrics": {
                "width": str(self.width),
                "conservative_credit": str(self.conservative_credit),
                "max_loss": str(self.max_loss),
                "breakeven": str(self.breakeven),
                "max_bid_ask_width_pct": str(self.max_bid_ask_width_pct),
                "credit_to_width": str(self.credit_to_width),
            },
            "rejection_reasons": [
                {
                    "code": reason.code.value,
                    "message": reason.message,
                    "field": reason.field,
                }
                for reason in self.eligibility.rejection_reasons
            ],
        }
        return json.dumps(payload, sort_keys=True)

    def to_storage_model(self, config_version: str) -> TradeCandidate:
        """Convert this scanned spread to the persistent trade candidate model."""
        return TradeCandidate(
            symbol=self.candidate.symbol.strip().upper(),
            expiration_date=self.candidate.short_put.expiration_date,
            short_put_strike=self.candidate.short_put.strike,
            long_put_strike=self.candidate.long_put.strike,
            max_loss=self.max_loss,
            status=self.status.value,
            reason_json=self.reason_json(),
            config_version=config_version,
        )

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this scanned spread decision to a structured audit event."""
        event_type = (
            "CANDIDATE_WATCHLIST"
            if self.status == CandidateScanStatus.WATCHLIST
            else "CANDIDATE_REJECTED"
        )
        message = (
            "Candidate generated for watchlist"
            if self.status == CandidateScanStatus.WATCHLIST
            else f"Candidate blocked by scanner status: {self.status.value}"
        )
        rejection_reason_codes = [reason.code.value for reason in self.eligibility.rejection_reasons]
        return AuditEvent(
            event_type=event_type,
            entity_type="trade_candidate",
            message=message,
            metadata={
                "symbol": self.candidate.symbol.strip().upper(),
                "expiration_date": self.candidate.short_put.expiration_date.isoformat(),
                "short_put_strike": str(self.candidate.short_put.strike),
                "long_put_strike": str(self.candidate.long_put.strike),
                "width": str(self.width),
                "conservative_credit": str(self.conservative_credit),
                "max_loss": str(self.max_loss),
                "breakeven": str(self.breakeven),
                "max_bid_ask_width_pct": str(self.max_bid_ask_width_pct),
                "credit_to_width": str(self.credit_to_width),
                "status": self.status.value,
                "eligibility_decision": self.eligibility.decision.value,
                "rejection_reason_codes": rejection_reason_codes,
                "regime": self.candidate.regime.value,
                "evaluated_at": self.candidate.evaluated_at.isoformat(),
                "quote_timestamp": self.candidate.short_put.quote_timestamp.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.candidate.evaluated_at,
        )


@dataclass(frozen=True, slots=True)
class SpreadScanResult:
    """Result of scanning one option-chain snapshot."""

    symbol: str
    quote_timestamp: datetime
    expiration_date: date
    regime: RegimeLabel
    spreads: tuple[ScannedSpread, ...]

    @property
    def eligible_spreads(self) -> tuple[ScannedSpread, ...]:
        """Return spreads generated for watchlist."""
        return tuple(spread for spread in self.spreads if spread.status == CandidateScanStatus.WATCHLIST)

    @property
    def rejected_spreads(self) -> tuple[ScannedSpread, ...]:
        """Return spreads not generated for watchlist."""
        return tuple(spread for spread in self.spreads if spread.status != CandidateScanStatus.WATCHLIST)


def scan_spreads(
    option_chain: OptionChainSnapshot,
    evaluated_at: datetime,
    regime: RegimeLabel,
    strategy: StrategyDefaults,
    risk_budget: Decimal | None = None,
    kill_switch: KillSwitchDecision | None = None,
) -> SpreadScanResult:
    """Enumerate put verticals and generate scanner candidates."""
    put_quotes = _sorted_puts(option_chain.quotes)
    scanned_spreads: list[ScannedSpread] = []

    for short_put in put_quotes:
        for long_put in put_quotes:
            if short_put.strike <= long_put.strike:
                continue
            candidate = PutSpreadCandidate(
                symbol=option_chain.symbol,
                short_put=short_put,
                long_put=long_put,
                evaluated_at=evaluated_at,
                regime=regime,
            )
            eligibility = evaluate_eligibility(candidate, strategy)
            conservative_credit = _conservative_credit(short_put, long_put)
            width = _spread_width(short_put, long_put)
            max_loss = _max_loss(short_put, long_put)
            scanned_spreads.append(
                ScannedSpread(
                    candidate=candidate,
                    eligibility=eligibility,
                    conservative_credit=conservative_credit,
                    width=width,
                    max_loss=max_loss,
                    breakeven=_breakeven(short_put, conservative_credit),
                    max_bid_ask_width_pct=_max_bid_ask_width_pct(short_put, long_put),
                    credit_to_width=_credit_to_width(conservative_credit, width),
                    scan_status=_scan_status(eligibility, max_loss, risk_budget, kill_switch),
                )
            )

    return SpreadScanResult(
        symbol=option_chain.symbol,
        quote_timestamp=option_chain.quote_timestamp,
        expiration_date=option_chain.expiration_date,
        regime=regime,
        spreads=tuple(scanned_spreads),
    )


def storage_models_for_scan(scan_result: SpreadScanResult, config_version: str) -> list[TradeCandidate]:
    """Convert every scanned spread into storage models for audit persistence."""
    return [spread.to_storage_model(config_version) for spread in scan_result.spreads]


def audit_events_for_scan(scan_result: SpreadScanResult, config_version: str) -> list[AuditEvent]:
    """Convert every scanned spread decision into structured audit events."""
    return [spread.to_audit_event(config_version) for spread in scan_result.spreads]


def _sorted_puts(quotes: tuple[OptionQuote, ...]) -> list[OptionQuote]:
    return sorted(
        [quote for quote in quotes if quote.option_type == OptionType.PUT],
        key=lambda quote: quote.strike,
        reverse=True,
    )


def _conservative_credit(short_put: OptionQuote, long_put: OptionQuote) -> Decimal:
    return short_put.bid - long_put.ask


def _spread_width(short_put: OptionQuote, long_put: OptionQuote) -> Decimal:
    return short_put.strike - long_put.strike


def _max_loss(short_put: OptionQuote, long_put: OptionQuote) -> Decimal:
    width = _spread_width(short_put, long_put)
    credit = _conservative_credit(short_put, long_put)
    if width <= Decimal("0"):
        return Decimal("0")
    return width - credit


def _breakeven(short_put: OptionQuote, conservative_credit: Decimal) -> Decimal:
    return short_put.strike - conservative_credit


def _credit_to_width(conservative_credit: Decimal, width: Decimal) -> Decimal:
    if width <= Decimal("0") or conservative_credit <= Decimal("0"):
        return Decimal("0")
    return conservative_credit / width


def _max_bid_ask_width_pct(short_put: OptionQuote, long_put: OptionQuote) -> Decimal:
    return max(_bid_ask_width_pct(short_put), _bid_ask_width_pct(long_put))


def _bid_ask_width_pct(quote: OptionQuote) -> Decimal:
    if quote.mid <= Decimal("0"):
        return Decimal("Infinity")
    return (quote.ask - quote.bid) / quote.mid


def _scan_status(
    eligibility: EligibilityResult,
    max_loss: Decimal,
    risk_budget: Decimal | None,
    kill_switch: KillSwitchDecision | None,
) -> CandidateScanStatus:
    if kill_switch is not None and not kill_switch.allow_new_trades:
        return CandidateScanStatus.BLOCKED_BY_RISK
    if risk_budget is not None and (risk_budget <= Decimal("0") or max_loss > risk_budget):
        return CandidateScanStatus.BLOCKED_BY_RISK
    if eligibility.decision == EligibilityDecision.PASS:
        return CandidateScanStatus.WATCHLIST

    reason_codes = {reason.code.value for reason in eligibility.rejection_reasons}
    if "BEARISH_OR_UNKNOWN_REGIME" in reason_codes:
        return CandidateScanStatus.BLOCKED_BY_REGIME
    if reason_codes.intersection({"INVALID_DTE", "SHORT_DELTA_OUT_OF_RANGE", "TIMEZONE_REQUIRED", "OPTION_TYPE_MISMATCH"}):
        return CandidateScanStatus.BLOCKED_BY_DATA
    if reason_codes.intersection({"BID_ASK_WIDTH_TOO_WIDE", "CREDIT_TO_WIDTH_TOO_LOW", "NON_POSITIVE_CREDIT"}):
        return CandidateScanStatus.BLOCKED_BY_LIQUIDITY
    return CandidateScanStatus.REJECTED
