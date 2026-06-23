from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from options_engine.backtest import BacktestConfig, BacktestError, BacktestMode, FillModel
from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.data_quality import (
    DataQualityRejectionCode,
    DataQualityRejectionReason,
    DataQualityResult,
    DataQualitySeverity,
    evaluate_required_data_quality,
)
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.execution import create_ticket
from options_engine.live import (
    DailyPilotSignoff,
    LiveFillEntry,
    LivePilotError,
    PilotSessionStartRequest,
    build_gated_live_risk_dashboard_from_database,
    build_pilot_evidence_packet,
    load_pilot_sessions,
    record_daily_operator_signoff,
    record_live_fill_for_active_session,
    start_pilot_session,
)
from options_engine.regime import (
    AccountReconciliationStatus,
    RegimeInputs,
    RegimeLabel,
    VIXTermStructureStatus,
    classify_regime_state,
)
from options_engine.risk import RiskCheckResult, RiskRejectionCode
from options_engine.risk.kill_switch import KillSwitchInputs, evaluate_kill_switch_state
from options_engine.risk.models import reject
from options_engine.storage.database import (
    connect_database,
    initialize_database,
    insert_regime_state,
    insert_trade_ticket,
    record_audit_event,
)
from options_engine.storage.models import TradeTicket
from options_engine.strategy import TradeEligibilityStatus, evaluate_trade_eligibility, scan_spreads

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_VERSION = "pre-pilot-audit"


def test_pre_pilot_safety_invariants_are_enforced_in_source_and_config() -> None:
    symbols_config = yaml.safe_load((PROJECT_ROOT / "config" / "symbols.yaml").read_text(encoding="utf-8"))
    risk_config = yaml.safe_load((PROJECT_ROOT / "config" / "risk_limits.yaml").read_text(encoding="utf-8"))
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    production_text = _production_text()

    assert set(symbols_config["symbols"]) == {"SPX", "SPY", "QQQ"}
    assert risk_config["risk_limits"]["allow_martingale"] is False
    assert risk_config["risk_limits"]["allow_live_orders"] is False

    forbidden_dependencies = {"ib_insync", "alpaca", "tastytrade", "tradier", "schwab", "robin_stocks"}
    assert not {dependency for dependency in forbidden_dependencies if dependency in pyproject}

    forbidden_patterns = {
        "broker order submission": r"\b(submit_order|place_order|create_order)\s*\(",
        "broker SDK/network execution": r"\b(requests|httpx|websocket)\.",
        "market orders enabled": r"market_order_allowed['\"]?\s*[:=]\s*True",
        "broker submission enabled": r"broker_order_submitted(?:_by_system)?['\"]?\s*[:=]\s*True",
        "auto execution enabled": r"auto_execution['\"]?\s*[:=]\s*True",
        "live orders requested": r"live_order_requested\s*=\s*True",
        "martingale enabled": r"martingale_requested\s*=\s*True",
    }
    for label, pattern in forbidden_patterns.items():
        assert re.search(pattern, production_text) is None, label

    with pytest.raises(BacktestError, match="fantasy mid-only fills"):
        BacktestConfig(mode=BacktestMode.FULL_MVP_RULES, fill_model=FillModel.MID_ONLY)


def test_pre_pilot_audit_completeness_for_major_decision_paths(tmp_path: Path) -> None:
    database_path = tmp_path / "audit.sqlite"
    initialize_database(database_path)
    now = _now()
    scanned_spread = _eligible_scanned_spread()
    dq_failure = DataQualityResult.from_rejections(
        [
            DataQualityRejectionReason(
                code=DataQualityRejectionCode.MISSING_ACCOUNT_EQUITY,
                message="account equity is required",
                field="account_equity",
                severity=DataQualitySeverity.CRITICAL,
            )
        ],
        checked_at=now,
    )
    regime = classify_regime_state(
        RegimeInputs(
            symbol="SPY",
            as_of=now,
            underlying_close=Decimal("540"),
            moving_average_50=Decimal("530"),
            implied_volatility=Decimal("0.20"),
            realized_volatility=Decimal("0.18"),
            vix_term_structure=VIXTermStructureStatus.NORMAL,
            data_quality=DataQualityResult.pass_result(now),
            account_reconciliation=AccountReconciliationStatus.RECONCILED,
            open_risk_verified=True,
        )
    )
    risk_failure = RiskCheckResult.from_rejections(
        [reject(RiskRejectionCode.INVALID_ACCOUNT_EQUITY, "account equity missing", "account_equity")]
    )
    eligibility = evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=dq_failure,
        regime=RegimeLabel.GREEN,
        risk_result=risk_failure,
        contracts=1,
        candidate_id=101,
        timestamp=now,
    )
    ticket = create_ticket(scanned_spread, config_version=CONFIG_VERSION, candidate_id=101)
    kill_switch = evaluate_kill_switch_state(
        KillSwitchInputs(evaluated_at=now, account_equity=None, current_regime=RegimeLabel.GREEN)
    )

    with connect_database(database_path) as connection:
        record_audit_event(connection, dq_failure.to_audit_event(CONFIG_VERSION))
        insert_regime_state(connection, regime.to_storage_model(CONFIG_VERSION))
        record_audit_event(connection, eligibility.to_audit_event(CONFIG_VERSION))
        record_audit_event(connection, ticket.to_audit_event())
        record_audit_event(connection, kill_switch.to_audit_event(CONFIG_VERSION))
        session = start_pilot_session(connection, _session_request(now))
        ticket_id = insert_trade_ticket(connection, _trade_ticket(now))
        record_live_fill_for_active_session(
            connection,
            _live_fill(ticket_id=ticket_id, filled_at=now),
            config_version=CONFIG_VERSION,
            pilot_id=session.pilot_id,
        )
        signoff = record_daily_operator_signoff(
            connection,
            DailyPilotSignoff(
                pilot_id=session.pilot_id,
                signoff_date=now.date(),
                operator="operator",
                signed_at=now,
                report_reviewed=True,
                positions_reconciled=True,
                slippage_reviewed=True,
                violations_reviewed=True,
                notes="audit review complete",
            ),
            config_version=CONFIG_VERSION,
        )
        event_types = {
            row[0]
            for row in connection.execute("SELECT event_type FROM audit_log ORDER BY id").fetchall()
        }
        stored_regime_payload = connection.execute("SELECT details_json FROM regime_states").fetchone()[0]

    assert eligibility.status == TradeEligibilityStatus.NO_TRADE
    assert "INVALID_ACCOUNT_EQUITY" in eligibility.reason_codes
    assert signoff.passed is True
    assert {
        "DATA_QUALITY_FAILED",
        "TRADE_ELIGIBILITY_NO_TRADE",
        "MANUAL_TICKET_DRAFTED",
        "KILL_SWITCH_BLACK",
        "LIVE_PILOT_SESSION_STARTED",
        "LIVE_PILOT_SESSION_GATE_READY",
        "LIVE_FILL_RECORDED",
        "LIVE_FILL_SLIPPAGE_RECORDED",
        "LIVE_PILOT_SESSION_FILL_RECORDED",
        "LIVE_PILOT_DAILY_SIGNOFF_PASSED",
    }.issubset(event_types)
    assert json.loads(stored_regime_payload)["reason_codes"]


def test_no_trade_contract_for_missing_data_red_black_missing_equity_and_unverified_positions(tmp_path: Path) -> None:
    database_path = tmp_path / "notrade.sqlite"
    initialize_database(database_path)
    now = _now()
    scanned_spread = _eligible_scanned_spread()

    missing_data = evaluate_required_data_quality(
        symbol="SPY",
        now=now,
        price_bars=[],
        option_chains=[],
        account_equity=None,
        open_positions=None,
    )
    missing_data_decision = evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=missing_data,
        regime=RegimeLabel.GREEN,
        risk_result=RiskCheckResult.pass_result(),
        contracts=1,
        candidate_id=None,
        timestamp=now,
    )
    red_decision = evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=DataQualityResult.pass_result(now),
        regime=RegimeLabel.RED,
        risk_result=RiskCheckResult.pass_result(),
        contracts=1,
        candidate_id=None,
        timestamp=now,
    )
    black_kill_switch = evaluate_kill_switch_state(
        KillSwitchInputs(
            evaluated_at=now,
            account_equity=Decimal("100000"),
            open_positions_verified=False,
            current_regime=RegimeLabel.GREEN,
        )
    )
    unverified_positions_decision = evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=DataQualityResult.pass_result(now),
        regime=RegimeLabel.GREEN,
        risk_result=RiskCheckResult.pass_result(),
        contracts=1,
        candidate_id=None,
        timestamp=now,
        kill_switch=black_kill_switch,
    )

    with connect_database(database_path) as connection:
        gate = build_gated_live_risk_dashboard_from_database
        with pytest.raises(LivePilotError, match="NO_ACTIVE_SESSION"):
            gate(
                connection,
                now.date(),
                generated_at=now,
                config_version=CONFIG_VERSION,
                pilot_id="missing-session",
            )

    assert missing_data_decision.status == TradeEligibilityStatus.NO_TRADE
    assert "DATA_QUALITY_FAILED" in missing_data_decision.reason_codes
    assert "MISSING_ACCOUNT_EQUITY" in missing_data_decision.reason_codes
    assert red_decision.status == TradeEligibilityStatus.NO_TRADE
    assert "REGIME_NOT_ALLOWED" in red_decision.reason_codes
    assert unverified_positions_decision.status == TradeEligibilityStatus.NO_TRADE
    assert "KILL_SWITCH_BLOCKS_NEW_TRADES" in unverified_positions_decision.reason_codes
    assert "OPEN_POSITIONS_UNVERIFIED" in unverified_positions_decision.reason_codes


def test_evidence_packet_replays_session_state_from_audit_log(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.sqlite"
    output_path = tmp_path / "evidence.json"
    initialize_database(database_path)
    now = _now()

    with connect_database(database_path) as connection:
        session = start_pilot_session(connection, _session_request(now))
        ticket_id = insert_trade_ticket(connection, _trade_ticket(now))
        fill_result = record_live_fill_for_active_session(
            connection,
            _live_fill(ticket_id=ticket_id, filled_at=now),
            config_version=CONFIG_VERSION,
            pilot_id=session.pilot_id,
        )
        record_daily_operator_signoff(
            connection,
            DailyPilotSignoff(
                pilot_id=session.pilot_id,
                signoff_date=now.date(),
                operator="operator",
                signed_at=now,
                report_reviewed=True,
                positions_reconciled=True,
                slippage_reviewed=True,
                violations_reviewed=True,
                notes="daily signoff complete",
            ),
            config_version=CONFIG_VERSION,
        )
        replayed_session = load_pilot_sessions(connection)[0]
        evidence = build_pilot_evidence_packet(connection, pilot_id=session.pilot_id, generated_at=now)
        evidence.write_json(output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert replayed_session.to_dict() == evidence.pilot_session.to_dict()
    assert payload["pilot_session"] == replayed_session.to_dict()
    assert payload["pilot_session"]["trade_count"] == 1
    assert payload["broker_orders_submitted_by_system"] is False
    assert payload["fills"][0]["id"] == fill_result.fill_result.fill_id
    assert payload["slippage_events"][0]["metadata"]["fill_id"] == fill_result.fill_result.fill_id
    assert any(event["event_type"] == "LIVE_PILOT_SESSION_FILL_RECORDED" for event in payload["audit_events"])


def _production_text() -> str:
    paths = list((PROJECT_ROOT / "src").rglob("*.py")) + [
        PROJECT_ROOT / "config" / "risk_limits.yaml",
        PROJECT_ROOT / "config" / "symbols.yaml",
        PROJECT_ROOT / "pyproject.toml",
    ]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _eligible_scanned_spread() -> object:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_now(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
    )
    return result.eligible_spreads[0]


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _option_chain_snapshot() -> OptionChainSnapshot:
    expiration = date(2026, 7, 24)
    return OptionChainSnapshot(
        symbol="SPY",
        quote_timestamp=_now(),
        expiration_date=expiration,
        quotes=(
            _quote(expiration_date=expiration, strike="540", bid="2.10", ask="2.20", delta="-0.18"),
            _quote(expiration_date=expiration, strike="535", bid="0.45", ask="0.50", delta="-0.12"),
        ),
    )


def _quote(*, expiration_date: date, strike: str, bid: str, ask: str, delta: str) -> OptionQuote:
    return OptionQuote(
        symbol="SPY",
        quote_timestamp=_now(),
        expiration_date=expiration_date,
        dte=(expiration_date - _now().date()).days,
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


def _session_request(started_at: datetime) -> PilotSessionStartRequest:
    return PilotSessionStartRequest(
        pilot_id="pilot-audit",
        operator="operator",
        config_version=CONFIG_VERSION,
        started_at=started_at,
    )


def _trade_ticket(created_at: datetime) -> TradeTicket:
    return TradeTicket(
        candidate_id=None,
        symbol="SPY",
        order_type="LIMIT",
        limit_price=Decimal("1.50"),
        status="DRAFT",
        notes="MANUAL_EXECUTION_REQUIRED; NO_MARKET_ORDERS",
        config_version=CONFIG_VERSION,
        created_at=created_at,
    )


def _live_fill(*, ticket_id: int, filled_at: datetime) -> LiveFillEntry:
    return LiveFillEntry(
        ticket_id=ticket_id,
        position_id=None,
        filled_at=filled_at,
        quantity=1,
        price=Decimal("1.45"),
        expected_credit=Decimal("1.50"),
        manual_execution_confirmed=True,
        execution_kill_switch_state="GREEN",
    )


def _now() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)
