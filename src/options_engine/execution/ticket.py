"""Manual trade ticket creation for eligible put spread candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from options_engine.risk.kill_switch import KillSwitchDecision
from options_engine.storage.models import AuditEvent, TradeTicket
from options_engine.strategy.eligibility import TradeEligibilityDecision, TradeEligibilityStatus
from options_engine.strategy.spread_scanner import CandidateScanStatus, ScannedSpread

MANUAL_EXECUTION_REQUIRED = "MANUAL_EXECUTION_REQUIRED"
NO_MARKET_ORDERS = "NO_MARKET_ORDERS"
MANUAL_TICKET_WARNINGS = (MANUAL_EXECUTION_REQUIRED, NO_MARKET_ORDERS)


class TicketError(ValueError):
    """Raised when a manual ticket cannot be created safely."""


class TicketStatus(StrEnum):
    """Manual ticket lifecycle states."""

    DRAFT = "DRAFT"


class TicketOrderType(StrEnum):
    """Allowed manual ticket order types."""

    LIMIT = "LIMIT"


@dataclass(frozen=True, slots=True)
class ManualTicketDraft:
    """Manual review ticket draft that is never submitted automatically."""

    ticket: TradeTicket
    source_status: CandidateScanStatus
    conservative_credit: Decimal
    max_loss: Decimal

    def to_audit_event(self) -> AuditEvent:
        """Convert this manual ticket draft to a structured audit event."""
        return AuditEvent(
            event_type="MANUAL_TICKET_DRAFTED",
            entity_type="trade_ticket",
            message="Manual-review ticket drafted locally; no broker order submitted",
            metadata={
                "candidate_id": self.ticket.candidate_id,
                "symbol": self.ticket.symbol,
                "order_type": self.ticket.order_type,
                "limit_price": str(self.ticket.limit_price),
                "status": self.ticket.status,
                "source_status": self.source_status.value,
                "conservative_credit": str(self.conservative_credit),
                "max_loss": str(self.max_loss),
                "broker_order_submitted": False,
                "market_order_allowed": False,
                "config_version": self.ticket.config_version,
            },
            config_version=self.ticket.config_version,
            created_at=self.ticket.created_at,
        )


@dataclass(frozen=True, slots=True)
class ManualExecutionTicket:
    """Structured manual execution ticket for an approved candidate."""

    candidate_id: int | None
    symbol: str
    expiration: date
    short_strike: Decimal
    long_strike: Decimal
    contracts: int
    multiplier: Decimal
    target_credit: Decimal
    worst_acceptable_credit: Decimal
    mid_price: Decimal
    natural_price: Decimal
    max_loss: Decimal
    account_risk_pct: Decimal
    projected_portfolio_heat: Decimal
    regime_state: str
    entry_reason: str
    rejection_risks: tuple[str, ...]
    exit_plan: str
    config_version: str
    created_at: datetime
    warnings: tuple[str, ...] = MANUAL_TICKET_WARNINGS

    def to_payload(self) -> dict[str, object]:
        """Return the complete auditable manual ticket payload."""
        return {
            "ticket_type": MANUAL_EXECUTION_REQUIRED,
            "warnings": list(self.warnings),
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "expiration": self.expiration.isoformat(),
            "short_strike": str(self.short_strike),
            "long_strike": str(self.long_strike),
            "contracts": self.contracts,
            "multiplier": str(self.multiplier),
            "target_credit": str(self.target_credit),
            "worst_acceptable_credit": str(self.worst_acceptable_credit),
            "mid_price": str(self.mid_price),
            "natural_price": str(self.natural_price),
            "max_loss": str(self.max_loss),
            "account_risk_pct": str(self.account_risk_pct),
            "projected_portfolio_heat": str(self.projected_portfolio_heat),
            "regime_state": self.regime_state,
            "entry_reason": self.entry_reason,
            "rejection_risks": list(self.rejection_risks),
            "exit_plan": self.exit_plan,
            "created_at": self.created_at.isoformat(),
            "order_type": TicketOrderType.LIMIT.value,
            "broker_order_submitted": False,
            "market_order_allowed": False,
            "config_version": self.config_version,
        }

    def to_storage_model(self) -> TradeTicket:
        """Convert the manual execution ticket to the durable storage model."""
        return TradeTicket(
            candidate_id=self.candidate_id,
            symbol=self.symbol,
            order_type=TicketOrderType.LIMIT.value,
            limit_price=self.target_credit,
            status=TicketStatus.DRAFT.value,
            notes=json.dumps(self.to_payload(), sort_keys=True),
            config_version=self.config_version,
            created_at=self.created_at,
        )

    def to_audit_event(self) -> AuditEvent:
        """Convert this manual execution ticket to a structured audit event."""
        return AuditEvent(
            event_type="MANUAL_EXECUTION_TICKET_CREATED",
            entity_type="trade_ticket",
            message="Manual execution ticket created locally; no broker order submitted",
            metadata=self.to_payload(),
            config_version=self.config_version,
            created_at=self.created_at,
        )


def create_ticket(
    scanned_spread: ScannedSpread,
    config_version: str,
    candidate_id: int | None = None,
    kill_switch: KillSwitchDecision | None = None,
) -> ManualTicketDraft:
    """Create a local manual-review ticket from an eligible scanned spread."""
    _ensure_ticket_not_blocked(kill_switch)
    if scanned_spread.status != CandidateScanStatus.ELIGIBLE_FOR_REVIEW:
        raise TicketError("manual tickets can only be created for eligible scanned spreads")

    if scanned_spread.conservative_credit <= Decimal("0"):
        raise TicketError("manual ticket limit reference must be positive")

    candidate = scanned_spread.candidate
    ticket = TradeTicket(
        candidate_id=candidate_id,
        symbol=candidate.symbol.strip().upper(),
        order_type=TicketOrderType.LIMIT.value,
        limit_price=scanned_spread.conservative_credit,
        status=TicketStatus.DRAFT.value,
        notes=_ticket_notes(scanned_spread),
        config_version=config_version,
    )
    return ManualTicketDraft(
        ticket=ticket,
        source_status=scanned_spread.status,
        conservative_credit=scanned_spread.conservative_credit,
        max_loss=scanned_spread.max_loss,
    )


def create_manual_execution_ticket(
    scanned_spread: ScannedSpread,
    decision: TradeEligibilityDecision,
    *,
    account_equity: Decimal,
    projected_portfolio_heat: Decimal,
    config_version: str,
    exit_plan: str,
    target_credit: Decimal | None = None,
    worst_acceptable_credit: Decimal | None = None,
    multiplier: Decimal = Decimal("100"),
    entry_reason: str | None = None,
    created_at: datetime | None = None,
    kill_switch: KillSwitchDecision | None = None,
) -> ManualExecutionTicket:
    """Create a manual execution ticket from one final approved decision."""
    _ensure_ticket_not_blocked(kill_switch)
    if decision.status != TradeEligibilityStatus.APPROVED:
        raise TicketError("manual execution tickets require an APPROVED eligibility decision")
    if scanned_spread.status != CandidateScanStatus.WATCHLIST:
        raise TicketError("manual execution tickets require a watchlist scanner candidate")
    if not config_version:
        raise TicketError("config_version is required")
    if account_equity <= Decimal("0"):
        raise TicketError("account_equity must be positive")
    if multiplier <= Decimal("0"):
        raise TicketError("multiplier must be positive")
    if projected_portfolio_heat < Decimal("0"):
        raise TicketError("projected_portfolio_heat must be non-negative")
    if not exit_plan.strip():
        raise TicketError("exit_plan is required")

    contracts = _decision_contracts(decision)
    ticket_created_at = created_at or decision.timestamp
    _validate_timestamp(ticket_created_at)

    candidate = scanned_spread.candidate
    mid_price = candidate.short_put.mid - candidate.long_put.mid
    natural_price = scanned_spread.conservative_credit
    ticket_target_credit = natural_price if target_credit is None else target_credit
    ticket_worst_credit = ticket_target_credit if worst_acceptable_credit is None else worst_acceptable_credit

    _validate_credit("target_credit", ticket_target_credit)
    _validate_credit("worst_acceptable_credit", ticket_worst_credit)
    if ticket_worst_credit > ticket_target_credit:
        raise TicketError("worst_acceptable_credit cannot exceed target_credit")
    if scanned_spread.max_loss <= Decimal("0"):
        raise TicketError("max_loss must be positive")

    max_loss_per_spread = scanned_spread.max_loss * multiplier
    total_trade_risk = max_loss_per_spread * Decimal(contracts)
    return ManualExecutionTicket(
        candidate_id=decision.candidate_id,
        symbol=candidate.symbol.strip().upper(),
        expiration=candidate.short_put.expiration_date,
        short_strike=candidate.short_put.strike,
        long_strike=candidate.long_put.strike,
        contracts=contracts,
        multiplier=multiplier,
        target_credit=ticket_target_credit,
        worst_acceptable_credit=ticket_worst_credit,
        mid_price=mid_price,
        natural_price=natural_price,
        max_loss=total_trade_risk,
        account_risk_pct=total_trade_risk / account_equity,
        projected_portfolio_heat=projected_portfolio_heat,
        regime_state=_decision_regime(decision),
        entry_reason=entry_reason or "Final eligibility decision approved for manual execution review",
        rejection_risks=tuple(code for code in decision.reason_codes if code != TradeEligibilityStatus.APPROVED.value),
        exit_plan=exit_plan.strip(),
        config_version=config_version,
        created_at=ticket_created_at,
    )


def _ticket_notes(scanned_spread: ScannedSpread) -> str:
    candidate = scanned_spread.candidate
    return (
        "MANUAL REVIEW ONLY - not submitted to broker. "
        f"Put credit spread {candidate.symbol.strip().upper()} "
        f"{candidate.short_put.expiration_date.isoformat()} "
        f"short {candidate.short_put.strike} / long {candidate.long_put.strike}; "
        f"limit credit reference {scanned_spread.conservative_credit}; "
        f"max loss {scanned_spread.max_loss}; "
        "no market orders."
    )


def _decision_contracts(decision: TradeEligibilityDecision) -> int:
    contracts = decision.risk_summary.get("contracts")
    if not isinstance(contracts, int) or isinstance(contracts, bool):
        raise TicketError("approved decision must include integer contracts in risk_summary")
    if contracts < 1:
        raise TicketError("approved decision contracts must be at least 1")
    return contracts


def _decision_regime(decision: TradeEligibilityDecision) -> str:
    regime = decision.risk_summary.get("regime")
    if not isinstance(regime, str) or not regime:
        raise TicketError("approved decision must include regime in risk_summary")
    return regime


def _validate_credit(field_name: str, value: Decimal) -> None:
    if value <= Decimal("0"):
        raise TicketError(f"{field_name} must be positive")


def _validate_timestamp(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TicketError("created_at must be timezone-aware")


def _ensure_ticket_not_blocked(kill_switch: KillSwitchDecision | None) -> None:
    if kill_switch is None:
        return
    if not kill_switch.allow_ticket_generation:
        raise TicketError(
            f"{kill_switch.state.value} kill switch blocks manual ticket generation: "
            f"{', '.join(kill_switch.reason_codes)}"
        )
