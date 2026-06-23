from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.data_quality import (
    DataQualityDecision,
    DataQualityRejectionCode,
    DataQualityRejectionReason,
    DataQualityResult,
    DataQualitySeverity,
)
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.regime import RegimeLabel
from options_engine.risk import RiskCheckResult, RiskRejectionCode, RiskRejectionReason
from options_engine.storage.database import initialize_database, record_audit_event
from options_engine.strategy import (
    ScannedSpread,
    TradeEligibilityReasonCode,
    TradeEligibilityStatus,
    evaluate_trade_eligibility,
    scan_spreads,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_trade_eligibility_approves_when_all_gates_pass() -> None:
    decision = _decision(_watchlist_spread(), candidate_id=42, contracts=1)

    assert decision.status == TradeEligibilityStatus.APPROVED
    assert decision.reason_codes == (TradeEligibilityReasonCode.APPROVED.value,)
    assert decision.candidate_id == 42
    assert decision.risk_summary["contracts"] == 1
    assert decision.risk_summary["scanner_status"] == "WATCHLIST"


def test_trade_eligibility_watchlists_when_contracts_are_missing() -> None:
    decision = _decision(_watchlist_spread(), contracts=None)

    assert decision.status == TradeEligibilityStatus.WATCHLIST
    assert TradeEligibilityReasonCode.MISSING_CONTRACTS.value in decision.reason_codes


def test_trade_eligibility_no_trade_when_data_quality_fails() -> None:
    decision = _decision(_watchlist_spread(), data_quality=_data_quality_failure())

    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.DATA_QUALITY_FAILED.value in decision.reason_codes
    assert DataQualityRejectionCode.MISSING_PRICE_DATA.value in decision.reason_codes


def test_trade_eligibility_no_trade_when_regime_is_not_allowed() -> None:
    decision = _decision(_watchlist_spread(), regime=RegimeLabel.RED)

    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.REGIME_NOT_ALLOWED.value in decision.reason_codes


def test_trade_eligibility_no_trade_when_risk_check_fails() -> None:
    decision = _decision(_watchlist_spread(), risk_result=_risk_failure())

    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.RISK_CHECK_FAILED.value in decision.reason_codes
    assert RiskRejectionCode.PORTFOLIO_HEAT_LIMIT_EXCEEDED.value in decision.reason_codes


def test_trade_eligibility_rejects_when_liquidity_blocks_candidate() -> None:
    decision = _decision(_blocked_by_liquidity_spread())

    assert decision.status == TradeEligibilityStatus.REJECTED
    assert TradeEligibilityReasonCode.LIQUIDITY_BLOCKED.value in decision.reason_codes


def test_trade_eligibility_no_trade_when_scanner_blocks_by_data() -> None:
    decision = _decision(_blocked_by_data_spread())

    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.CANDIDATE_BLOCKED_BY_DATA.value in decision.reason_codes


def test_trade_eligibility_no_trade_when_scanner_blocks_by_risk() -> None:
    decision = _decision(_blocked_by_risk_spread())

    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.CANDIDATE_BLOCKED_BY_RISK.value in decision.reason_codes


def test_trade_eligibility_no_trade_when_contracts_less_than_one() -> None:
    decision = _decision(_watchlist_spread(), contracts=0)

    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.CONTRACTS_LESS_THAN_ONE.value in decision.reason_codes


def test_trade_eligibility_decision_creates_audit_log_entry(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    decision = _decision(_watchlist_spread(), candidate_id=42, contracts=1)

    audit_event = decision.to_audit_event(config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute("SELECT event_type, entity_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "TRADE_ELIGIBILITY_APPROVED"
    assert row[1] == "trade_eligibility_decision"
    payload = json.loads(row[2])["metadata"]
    assert payload["candidate_id"] == 42
    assert payload["status"] == TradeEligibilityStatus.APPROVED.value
    assert payload["reason_codes"] == [TradeEligibilityReasonCode.APPROVED.value]


def _decision(
    scanned_spread: ScannedSpread,
    *,
    data_quality: DataQualityResult | None = None,
    regime: RegimeLabel = RegimeLabel.GREEN,
    risk_result: RiskCheckResult | None = None,
    contracts: int | None = 1,
    candidate_id: int | None = 1,
) -> object:
    return evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=data_quality or _data_quality_pass(),
        regime=regime,
        risk_result=risk_result or RiskCheckResult.pass_result(),
        contracts=contracts,
        candidate_id=candidate_id,
        timestamp=_evaluated_at(),
    )


def _watchlist_spread() -> ScannedSpread:
    return _scan_result().eligible_spreads[0]


def _blocked_by_liquidity_spread() -> ScannedSpread:
    return _scan_result(long_bid="0.20", long_ask="0.50").rejected_spreads[0]


def _blocked_by_data_spread() -> ScannedSpread:
    return _scan_result(short_delta="-0.30").rejected_spreads[0]


def _blocked_by_risk_spread() -> ScannedSpread:
    return _scan_result(risk_budget=Decimal("1")).rejected_spreads[0]


def _scan_result(
    *,
    short_bid: str = "2.10",
    short_ask: str = "2.20",
    short_delta: str = "-0.18",
    long_bid: str = "0.45",
    long_ask: str = "0.50",
    risk_budget: Decimal | None = Decimal("100"),
) -> object:
    return scan_spreads(
        option_chain=_option_chain_snapshot(
            short_bid=short_bid,
            short_ask=short_ask,
            short_delta=short_delta,
            long_bid=long_bid,
            long_ask=long_ask,
        ),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        risk_budget=risk_budget,
    )


def _data_quality_pass() -> DataQualityResult:
    return DataQualityResult.pass_result(checked_at=_evaluated_at())


def _data_quality_failure() -> DataQualityResult:
    return DataQualityResult(
        decision=DataQualityDecision.NO_TRADE,
        rejection_reasons=(
            DataQualityRejectionReason(
                code=DataQualityRejectionCode.MISSING_PRICE_DATA,
                message="required price data is missing",
                field="price_bars",
                severity=DataQualitySeverity.ERROR,
            ),
        ),
        checked_at=_evaluated_at(),
        severity=DataQualitySeverity.ERROR,
        reason_code=DataQualityRejectionCode.MISSING_PRICE_DATA.value,
        message="required price data is missing",
    )


def _risk_failure() -> RiskCheckResult:
    return RiskCheckResult.from_rejections(
        [
            RiskRejectionReason(
                code=RiskRejectionCode.PORTFOLIO_HEAT_LIMIT_EXCEEDED,
                message="projected portfolio heat exceeds configured limit",
                field="projected_heat_after_trade",
            )
        ]
    )


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _evaluated_at() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _quote_timestamp() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _option_chain_snapshot(
    *,
    short_bid: str,
    short_ask: str,
    short_delta: str,
    long_bid: str,
    long_ask: str,
) -> OptionChainSnapshot:
    expiration = date(2026, 7, 24)
    quotes = (
        _quote(expiration_date=expiration, strike="540", bid=short_bid, ask=short_ask, delta=short_delta),
        _quote(expiration_date=expiration, strike="535", bid=long_bid, ask=long_ask, delta="-0.12"),
    )
    return OptionChainSnapshot(
        symbol="SPY",
        quote_timestamp=_quote_timestamp(),
        expiration_date=expiration,
        quotes=quotes,
    )


def _quote(
    *,
    expiration_date: date,
    strike: str,
    bid: str,
    ask: str,
    delta: str,
) -> OptionQuote:
    return OptionQuote(
        symbol="SPY",
        quote_timestamp=_quote_timestamp(),
        expiration_date=expiration_date,
        dte=(expiration_date - _evaluated_at().date()).days,
        option_type=OptionType.PUT,
        strike=Decimal(strike),
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        iv=Decimal("0.1800"),
        delta=Decimal(delta),
        gamma=Decimal("0.0150"),
        theta=Decimal("-0.0800"),
        vega=Decimal("0.1200"),
        volume=100,
        open_interest=1000,
    )
