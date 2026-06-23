from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.data_quality import DataQualityResult
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.execution import TicketError, create_manual_execution_ticket
from options_engine.regime import RegimeLabel
from options_engine.risk import (
    KillSwitchAction,
    KillSwitchError,
    KillSwitchInputs,
    KillSwitchReasonCode,
    KillSwitchState,
    RiskCheckResult,
    evaluate_kill_switch_state,
    reset_kill_switch,
)
from options_engine.storage.database import initialize_database, record_audit_event
from options_engine.strategy import (
    CandidateScanStatus,
    ScannedSpread,
    TradeEligibilityDecision,
    TradeEligibilityReasonCode,
    TradeEligibilityStatus,
    evaluate_trade_eligibility,
    scan_spreads,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_kill_switch_green_allows_normal_recommendations() -> None:
    decision = evaluate_kill_switch_state(_kill_inputs())

    assert decision.state == KillSwitchState.GREEN
    assert decision.action == KillSwitchAction.NORMAL_RECOMMENDATION_ALLOWED
    assert decision.allow_new_trades is True
    assert decision.allow_ticket_generation is True
    assert decision.reason_codes == (KillSwitchReasonCode.STATE_GREEN.value,)


def test_kill_switch_yellow_allows_reduced_size_only() -> None:
    decision = evaluate_kill_switch_state(_kill_inputs(current_regime=RegimeLabel.YELLOW))

    assert decision.state == KillSwitchState.YELLOW
    assert decision.action == KillSwitchAction.REDUCED_SIZE_ONLY
    assert decision.allow_new_trades is True
    assert decision.reduced_size_only is True
    assert decision.reason_codes == (KillSwitchReasonCode.REDUCED_SIZE_REQUIRED.value,)


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"data_feed_critical_failure": True}, KillSwitchReasonCode.DATA_FEED_CRITICAL_FAILURE),
        ({"account_equity": None}, KillSwitchReasonCode.ACCOUNT_EQUITY_MISSING),
        ({"open_positions_verified": False}, KillSwitchReasonCode.OPEN_POSITIONS_UNVERIFIED),
        ({"broker_account_reconciled": False}, KillSwitchReasonCode.BROKER_ACCOUNT_RECONCILIATION_FAILED),
        ({"daily_loss_cap_breached": True}, KillSwitchReasonCode.DAILY_LOSS_CAP_BREACHED),
        ({"weekly_loss_cap_breached": True}, KillSwitchReasonCode.WEEKLY_LOSS_CAP_BREACHED),
        ({"monthly_loss_cap_breached": True}, KillSwitchReasonCode.MONTHLY_LOSS_CAP_BREACHED),
        ({"duplicate_order_risk_detected": True}, KillSwitchReasonCode.DUPLICATE_ORDER_RISK_DETECTED),
        ({"wrong_way_order_risk_detected": True}, KillSwitchReasonCode.WRONG_WAY_ORDER_RISK_DETECTED),
    ],
)
def test_kill_switch_black_triggers_hard_shutdown(
    overrides: dict[str, object],
    reason_code: KillSwitchReasonCode,
) -> None:
    decision = evaluate_kill_switch_state(_kill_inputs(**overrides))

    assert decision.state == KillSwitchState.BLACK
    assert decision.action == KillSwitchAction.RISK_OFF_REVIEW_REQUIRED
    assert decision.allow_new_trades is False
    assert decision.allow_ticket_generation is False
    assert decision.risk_off_review_required is True
    assert reason_code.value in decision.reason_codes


def test_kill_switch_writes_audit_log(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    decision = evaluate_kill_switch_state(_kill_inputs(open_positions_verified=False))
    audit_event = decision.to_audit_event(config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute(
            "SELECT event_type, entity_type, payload_json FROM audit_log WHERE id = ?",
            (inserted_id,),
        ).fetchone()

    payload = json.loads(row[2])["metadata"]
    assert row[0] == "KILL_SWITCH_BLACK"
    assert row[1] == "kill_switch"
    assert payload["state"] == KillSwitchState.BLACK.value
    assert KillSwitchReasonCode.OPEN_POSITIONS_UNVERIFIED.value in payload["reason_codes"]
    assert payload["allow_ticket_generation"] is False


def test_black_kill_switch_blocks_ticket_generation() -> None:
    scanned_spread = _watchlist_spread()
    eligibility = _approved_decision(scanned_spread)
    kill_switch = evaluate_kill_switch_state(_kill_inputs(open_positions_verified=False))

    with pytest.raises(TicketError, match="BLACK kill switch blocks manual ticket generation"):
        create_manual_execution_ticket(
            scanned_spread=scanned_spread,
            decision=eligibility,
            account_equity=Decimal("100000"),
            projected_portfolio_heat=Decimal("0.0500"),
            config_version="test-config",
            exit_plan="Exit at 50% profit or configured stop.",
            kill_switch=kill_switch,
        )


def test_red_kill_switch_blocks_new_trades_and_scanner_watchlist() -> None:
    kill_switch = evaluate_kill_switch_state(_kill_inputs(current_regime=RegimeLabel.RED))
    scanned_spread = _watchlist_spread()

    decision = evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=DataQualityResult.pass_result(checked_at=_evaluated_at()),
        regime=RegimeLabel.GREEN,
        risk_result=RiskCheckResult.pass_result(),
        contracts=1,
        candidate_id=42,
        timestamp=_evaluated_at(),
        kill_switch=kill_switch,
    )
    scan_result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
        kill_switch=kill_switch,
    )

    assert kill_switch.state == KillSwitchState.RED
    assert decision.status == TradeEligibilityStatus.NO_TRADE
    assert TradeEligibilityReasonCode.KILL_SWITCH_BLOCKS_NEW_TRADES.value in decision.reason_codes
    assert KillSwitchReasonCode.RED_REGIME.value in decision.reason_codes
    assert decision.risk_summary["kill_switch_state"] == KillSwitchState.RED.value
    assert scan_result.eligible_spreads == ()
    assert {spread.status for spread in scan_result.rejected_spreads} == {CandidateScanStatus.BLOCKED_BY_RISK}


def test_kill_switch_reset_requires_explicit_reason() -> None:
    decision = evaluate_kill_switch_state(_kill_inputs(open_positions_verified=False))

    with pytest.raises(KillSwitchError, match="reset_reason is required"):
        reset_kill_switch(
            decision,
            reset_reason=" ",
            reset_at=_evaluated_at(),
        )

    reset_result = reset_kill_switch(
        decision,
        reset_reason="Manual reconciliation completed and open positions verified.",
        reset_at=_evaluated_at(),
    )

    assert reset_result.previous_state == KillSwitchState.BLACK
    assert reset_result.new_state == KillSwitchState.GREEN
    assert reset_result.previous_reason_codes == (KillSwitchReasonCode.OPEN_POSITIONS_UNVERIFIED.value,)
    assert reset_result.reset_reason == "Manual reconciliation completed and open positions verified."


def _kill_inputs(**overrides: object) -> KillSwitchInputs:
    values: dict[str, object] = {
        "evaluated_at": _evaluated_at(),
        "account_equity": Decimal("100000"),
    }
    values.update(overrides)
    return KillSwitchInputs(**values)


def _approved_decision(scanned_spread: ScannedSpread) -> TradeEligibilityDecision:
    decision = evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=DataQualityResult.pass_result(checked_at=_evaluated_at()),
        regime=RegimeLabel.GREEN,
        risk_result=RiskCheckResult.pass_result(),
        contracts=1,
        candidate_id=42,
        timestamp=_evaluated_at(),
    )
    assert decision.status == TradeEligibilityStatus.APPROVED
    return decision


def _watchlist_spread() -> ScannedSpread:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
    )
    return result.eligible_spreads[0]


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _evaluated_at() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _quote_timestamp() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _option_chain_snapshot() -> OptionChainSnapshot:
    expiration = date(2026, 7, 24)
    quotes = (
        _quote(expiration_date=expiration, strike="540", bid="2.10", ask="2.20", delta="-0.18"),
        _quote(expiration_date=expiration, strike="535", bid="0.45", ask="0.50", delta="-0.12"),
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
