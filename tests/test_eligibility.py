from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.option_chain import OptionQuote, OptionType
from options_engine.regime import RegimeLabel
from options_engine.strategy import (
    EligibilityDecision,
    EligibilityRejectionCode,
    EligibilityResult,
    PutSpreadCandidate,
    evaluate_eligibility,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_eligible_put_spread_candidate_passes_review_gate() -> None:
    result = evaluate_eligibility(_candidate(), _strategy())

    assert result.decision == EligibilityDecision.PASS
    assert result.rejection_reasons == ()
    assert result.details["conservative_credit"] == "1.60"
    assert result.details["credit_to_width"] == "0.32"


def test_bearish_or_unknown_regime_is_rejected() -> None:
    bearish = evaluate_eligibility(_candidate(regime=RegimeLabel.BEARISH), _strategy())
    unknown = evaluate_eligibility(_candidate(regime=RegimeLabel.UNKNOWN), _strategy())

    assert EligibilityRejectionCode.BEARISH_OR_UNKNOWN_REGIME in _codes(bearish)
    assert EligibilityRejectionCode.BEARISH_OR_UNKNOWN_REGIME in _codes(unknown)


def test_dte_outside_configured_range_is_rejected() -> None:
    result = evaluate_eligibility(
        _candidate(short_put=_short_put(expiration_date=date(2026, 7, 10)), long_put=_long_put(expiration_date=date(2026, 7, 10))),
        _strategy(),
    )

    assert EligibilityRejectionCode.INVALID_DTE in _codes(result)


def test_short_delta_outside_configured_range_is_rejected() -> None:
    result = evaluate_eligibility(_candidate(short_put=_short_put(delta=Decimal("-0.30"))), _strategy())

    assert EligibilityRejectionCode.SHORT_DELTA_OUT_OF_RANGE in _codes(result)


def test_wide_bid_ask_width_is_rejected() -> None:
    result = evaluate_eligibility(_candidate(long_put=_long_put(bid=Decimal("0.20"), ask=Decimal("0.50"))), _strategy())

    assert EligibilityRejectionCode.BID_ASK_WIDTH_TOO_WIDE in _codes(result)


def test_low_credit_to_width_is_rejected() -> None:
    result = evaluate_eligibility(
        _candidate(short_put=_short_put(bid=Decimal("1.80"), ask=Decimal("1.90"))),
        _strategy(),
    )

    assert EligibilityRejectionCode.CREDIT_TO_WIDTH_TOO_LOW in _codes(result)


def test_non_positive_conservative_credit_is_rejected() -> None:
    result = evaluate_eligibility(
        _candidate(short_put=_short_put(bid=Decimal("0.45"), ask=Decimal("0.55"))),
        _strategy(),
    )

    assert EligibilityRejectionCode.NON_POSITIVE_CREDIT in _codes(result)


def test_invalid_strike_order_is_rejected() -> None:
    result = evaluate_eligibility(
        _candidate(short_put=_short_put(strike=Decimal("535")), long_put=_long_put(strike=Decimal("540"))),
        _strategy(),
    )

    assert EligibilityRejectionCode.STRIKE_ORDER_INVALID in _codes(result)
    assert EligibilityRejectionCode.INVALID_SPREAD_WIDTH in _codes(result)


def test_non_put_candidate_is_rejected() -> None:
    result = evaluate_eligibility(_candidate(short_put=_short_put(option_type=OptionType.CALL)), _strategy())

    assert EligibilityRejectionCode.OPTION_TYPE_MISMATCH in _codes(result)


def test_mismatched_leg_snapshot_is_rejected() -> None:
    result = evaluate_eligibility(
        _candidate(long_put=_long_put(quote_timestamp=_quote_timestamp() + timedelta(minutes=1))),
        _strategy(),
    )

    assert EligibilityRejectionCode.QUOTE_TIMESTAMP_MISMATCH in _codes(result)


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _candidate(
    *,
    symbol: str = "SPY",
    short_put: OptionQuote | None = None,
    long_put: OptionQuote | None = None,
    evaluated_at: datetime | None = None,
    regime: RegimeLabel = RegimeLabel.NEUTRAL,
) -> PutSpreadCandidate:
    return PutSpreadCandidate(
        symbol=symbol,
        short_put=short_put or _short_put(),
        long_put=long_put or _long_put(),
        evaluated_at=evaluated_at or datetime(2026, 6, 19, 14, 0, tzinfo=UTC),
        regime=regime,
    )


def _short_put(
    *,
    symbol: str = "SPY",
    expiration_date: date = date(2026, 7, 24),
    option_type: OptionType = OptionType.PUT,
    strike: Decimal = Decimal("540"),
    bid: Decimal = Decimal("2.10"),
    ask: Decimal = Decimal("2.20"),
    delta: Decimal = Decimal("-0.18"),
    quote_timestamp: datetime | None = None,
) -> OptionQuote:
    return OptionQuote(
        symbol=symbol,
        quote_timestamp=quote_timestamp or _quote_timestamp(),
        expiration_date=expiration_date,
        dte=(expiration_date - datetime(2026, 6, 19, 14, 0, tzinfo=UTC).date()).days,
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / Decimal("2"),
        iv=Decimal("0.1800"),
        delta=delta,
        gamma=Decimal("0.0150"),
        theta=Decimal("-0.0800"),
        vega=Decimal("0.1200"),
        volume=120,
        open_interest=4500,
    )


def _long_put(
    *,
    symbol: str = "SPY",
    expiration_date: date = date(2026, 7, 24),
    option_type: OptionType = OptionType.PUT,
    strike: Decimal = Decimal("535"),
    bid: Decimal = Decimal("0.45"),
    ask: Decimal = Decimal("0.50"),
    delta: Decimal = Decimal("-0.12"),
    quote_timestamp: datetime | None = None,
) -> OptionQuote:
    return OptionQuote(
        symbol=symbol,
        quote_timestamp=quote_timestamp or _quote_timestamp(),
        expiration_date=expiration_date,
        dte=(expiration_date - datetime(2026, 6, 19, 14, 0, tzinfo=UTC).date()).days,
        option_type=option_type,
        strike=strike,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / Decimal("2"),
        iv=Decimal("0.1700"),
        delta=delta,
        gamma=Decimal("0.0140"),
        theta=Decimal("-0.0700"),
        vega=Decimal("0.1100"),
        volume=98,
        open_interest=3900,
    )


def _quote_timestamp() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _codes(result: EligibilityResult) -> set[EligibilityRejectionCode]:
    return {reason.code for reason in result.rejection_reasons}
