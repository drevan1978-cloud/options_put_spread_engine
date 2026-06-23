from __future__ import annotations

import sqlite3
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.config.loader import StrategyDefaults, load_config
from options_engine.data.data_quality import DataQualityResult
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote, OptionType
from options_engine.execution import (
    MANUAL_EXECUTION_REQUIRED,
    NO_MARKET_ORDERS,
    TicketError,
    TicketOrderType,
    TicketStatus,
    create_manual_execution_ticket,
    create_ticket,
)
from options_engine.regime import RegimeLabel
from options_engine.risk import RiskCheckResult
from options_engine.storage.database import initialize_database, insert_trade_ticket, record_audit_event
from options_engine.strategy import (
    ScannedSpread,
    TradeEligibilityDecision,
    TradeEligibilityStatus,
    evaluate_trade_eligibility,
    scan_spreads,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_create_ticket_from_eligible_scanned_spread() -> None:
    scanned_spread = _eligible_scanned_spread()

    draft = create_ticket(scanned_spread, config_version="test-config", candidate_id=42)

    assert draft.ticket.candidate_id == 42
    assert draft.ticket.symbol == "SPY"
    assert draft.ticket.order_type == TicketOrderType.LIMIT.value
    assert draft.ticket.limit_price == Decimal("1.60")
    assert draft.ticket.status == TicketStatus.DRAFT.value
    assert draft.ticket.config_version == "test-config"
    assert "MANUAL REVIEW ONLY" in draft.ticket.notes
    assert "not submitted to broker" in draft.ticket.notes
    assert "no market orders" in draft.ticket.notes


def test_create_ticket_rejects_rejected_scanned_spread() -> None:
    rejected_spread = _rejected_scanned_spread()

    with pytest.raises(TicketError, match="only be created for eligible"):
        create_ticket(rejected_spread, config_version="test-config")


def test_persists_manual_trade_ticket_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    draft = create_ticket(_eligible_scanned_spread(), config_version="test-config", candidate_id=None)

    with sqlite3.connect(database_path) as connection:
        inserted_id = insert_trade_ticket(connection, draft.ticket)
        row = connection.execute(
            """
            SELECT candidate_id, symbol, order_type, limit_price, status, notes, config_version
            FROM trade_tickets
            WHERE id = ?
            """,
            (inserted_id,),
        ).fetchone()

    assert row[0] is None
    assert row[1] == "SPY"
    assert row[2] == TicketOrderType.LIMIT.value
    assert row[3] == "1.60"
    assert row[4] == TicketStatus.DRAFT.value
    assert "not submitted to broker" in row[5]
    assert row[6] == "test-config"


def test_manual_ticket_draft_produces_audit_event(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    draft = create_ticket(_eligible_scanned_spread(), config_version="test-config", candidate_id=42)

    audit_event = draft.to_audit_event()

    assert audit_event.event_type == "MANUAL_TICKET_DRAFTED"
    assert audit_event.entity_type == "trade_ticket"
    assert audit_event.metadata["broker_order_submitted"] is False
    assert audit_event.metadata["market_order_allowed"] is False

    with sqlite3.connect(database_path) as connection:
        inserted_id = record_audit_event(connection, audit_event)
        row = connection.execute("SELECT event_type, entity_type, payload_json FROM audit_log WHERE id = ?", (inserted_id,)).fetchone()

    assert row[0] == "MANUAL_TICKET_DRAFTED"
    assert row[1] == "trade_ticket"
    assert json.loads(row[2])["metadata"]["candidate_id"] == 42


def test_create_manual_execution_ticket_from_approved_decision() -> None:
    scanned_spread = _eligible_scanned_spread()
    decision = _approved_decision(scanned_spread, candidate_id=42, contracts=2)

    ticket = create_manual_execution_ticket(
        scanned_spread=scanned_spread,
        decision=decision,
        account_equity=Decimal("100000"),
        projected_portfolio_heat=Decimal("0.0800"),
        config_version="test-config",
        exit_plan="Exit at 50% profit or configured stop.",
        worst_acceptable_credit=Decimal("1.50"),
    )

    assert ticket.candidate_id == 42
    assert ticket.symbol == "SPY"
    assert ticket.expiration == date(2026, 7, 24)
    assert ticket.short_strike == Decimal("540")
    assert ticket.long_strike == Decimal("535")
    assert ticket.contracts == 2
    assert ticket.multiplier == Decimal("100")
    assert ticket.target_credit == Decimal("1.60")
    assert ticket.worst_acceptable_credit == Decimal("1.50")
    assert ticket.mid_price == Decimal("1.675")
    assert ticket.natural_price == Decimal("1.60")
    assert ticket.max_loss == Decimal("680.00")
    assert ticket.account_risk_pct == Decimal("0.0068")
    assert ticket.projected_portfolio_heat == Decimal("0.0800")
    assert ticket.regime_state == RegimeLabel.GREEN.value
    assert ticket.rejection_risks == ()
    assert MANUAL_EXECUTION_REQUIRED in ticket.warnings
    assert NO_MARKET_ORDERS in ticket.warnings

    storage_model = ticket.to_storage_model()
    payload = json.loads(storage_model.notes)
    assert storage_model.order_type == TicketOrderType.LIMIT.value
    assert storage_model.status == TicketStatus.DRAFT.value
    assert storage_model.limit_price == Decimal("1.60")
    assert payload["multiplier"] == "100"
    assert payload["max_loss"] == "680.00"
    assert payload["ticket_type"] == MANUAL_EXECUTION_REQUIRED
    assert NO_MARKET_ORDERS in payload["warnings"]
    assert payload["broker_order_submitted"] is False
    assert payload["market_order_allowed"] is False


def test_manual_execution_ticket_persists_to_database(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    scanned_spread = _eligible_scanned_spread()
    ticket = create_manual_execution_ticket(
        scanned_spread=scanned_spread,
        decision=_approved_decision(scanned_spread, candidate_id=None, contracts=1),
        account_equity=Decimal("100000"),
        projected_portfolio_heat=Decimal("0.0500"),
        config_version="test-config",
        exit_plan="Exit at 50% profit or configured stop.",
    )

    with sqlite3.connect(database_path) as connection:
        inserted_id = insert_trade_ticket(connection, ticket.to_storage_model())
        row = connection.execute(
            """
            SELECT candidate_id, symbol, order_type, limit_price, status, notes, config_version
            FROM trade_tickets
            WHERE id = ?
            """,
            (inserted_id,),
        ).fetchone()

    payload = json.loads(row[5])
    assert row[0] is None
    assert row[1] == "SPY"
    assert row[2] == TicketOrderType.LIMIT.value
    assert row[3] == "1.60"
    assert row[4] == TicketStatus.DRAFT.value
    assert payload["ticket_type"] == MANUAL_EXECUTION_REQUIRED
    assert payload["warnings"] == [MANUAL_EXECUTION_REQUIRED, NO_MARKET_ORDERS]
    assert payload["exit_plan"] == "Exit at 50% profit or configured stop."
    assert row[6] == "test-config"


def test_rejected_decision_cannot_create_manual_execution_ticket() -> None:
    scanned_spread = _liquidity_blocked_scanned_spread()
    decision = _decision(scanned_spread, candidate_id=42, contracts=1)

    assert decision.status == TradeEligibilityStatus.REJECTED
    with pytest.raises(TicketError, match="APPROVED eligibility decision"):
        create_manual_execution_ticket(
            scanned_spread=scanned_spread,
            decision=decision,
            account_equity=Decimal("100000"),
            projected_portfolio_heat=Decimal("0.0500"),
            config_version="test-config",
            exit_plan="Exit at 50% profit or configured stop.",
        )


def test_manual_execution_ticket_produces_audit_event() -> None:
    scanned_spread = _eligible_scanned_spread()
    ticket = create_manual_execution_ticket(
        scanned_spread=scanned_spread,
        decision=_approved_decision(scanned_spread, candidate_id=42, contracts=1),
        account_equity=Decimal("100000"),
        projected_portfolio_heat=Decimal("0.0500"),
        config_version="test-config",
        exit_plan="Exit at 50% profit or configured stop.",
    )

    audit_event = ticket.to_audit_event()

    assert audit_event.event_type == "MANUAL_EXECUTION_TICKET_CREATED"
    assert audit_event.entity_type == "trade_ticket"
    assert audit_event.metadata["ticket_type"] == MANUAL_EXECUTION_REQUIRED
    assert NO_MARKET_ORDERS in audit_event.metadata["warnings"]


def _eligible_scanned_spread() -> ScannedSpread:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.NEUTRAL,
        strategy=_strategy(),
    )
    return result.eligible_spreads[0]


def _rejected_scanned_spread() -> ScannedSpread:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.BEARISH,
        strategy=_strategy(),
    )
    return result.rejected_spreads[0]


def _liquidity_blocked_scanned_spread() -> ScannedSpread:
    result = scan_spreads(
        option_chain=_option_chain_snapshot(long_bid="0.20", long_ask="0.50"),
        evaluated_at=_evaluated_at(),
        regime=RegimeLabel.GREEN,
        strategy=_strategy(),
    )
    return result.rejected_spreads[0]


def _approved_decision(
    scanned_spread: ScannedSpread,
    *,
    candidate_id: int | None,
    contracts: int,
) -> TradeEligibilityDecision:
    decision = _decision(scanned_spread, candidate_id=candidate_id, contracts=contracts)
    assert decision.status == TradeEligibilityStatus.APPROVED
    return decision


def _decision(
    scanned_spread: ScannedSpread,
    *,
    candidate_id: int | None,
    contracts: int,
) -> TradeEligibilityDecision:
    return evaluate_trade_eligibility(
        scanned_spread=scanned_spread,
        data_quality=DataQualityResult.pass_result(checked_at=_evaluated_at()),
        regime=RegimeLabel.GREEN,
        risk_result=RiskCheckResult.pass_result(),
        contracts=contracts,
        candidate_id=candidate_id,
        timestamp=_evaluated_at(),
    )


def _strategy() -> StrategyDefaults:
    return load_config(PROJECT_ROOT / "config").strategy


def _evaluated_at() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _quote_timestamp() -> datetime:
    return datetime(2026, 6, 19, 14, 0, tzinfo=UTC)


def _option_chain_snapshot(
    *,
    short_bid: str = "2.10",
    short_ask: str = "2.20",
    short_delta: str = "-0.18",
    long_bid: str = "0.45",
    long_ask: str = "0.50",
) -> OptionChainSnapshot:
    expiration = date(2026, 7, 24)
    return OptionChainSnapshot(
        symbol="SPY",
        quote_timestamp=_quote_timestamp(),
        expiration_date=expiration,
        quotes=(
            _quote(expiration_date=expiration, strike="540", bid=short_bid, ask=short_ask, delta=short_delta),
            _quote(expiration_date=expiration, strike="535", bid=long_bid, ask=long_ask, delta="-0.12"),
        ),
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
