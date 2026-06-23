"""Paper-trading workflow using conservative local-only simulations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from options_engine.config.loader import StrategyDefaults
from options_engine.data.data_quality import DataQualityResult
from options_engine.data.option_chain import OptionChainSnapshot
from options_engine.execution.position_monitor import add_position_from_filled_ticket, update_position_mark
from options_engine.execution.ticket import ManualExecutionTicket, create_manual_execution_ticket
from options_engine.regime import RegimeLabel
from options_engine.reporting import DailyReport, build_daily_report_from_database
from options_engine.risk import KillSwitchDecision, RiskCheckResult
from options_engine.storage.database import (
    insert_exit,
    insert_fill,
    insert_position,
    insert_trade_candidate,
    insert_trade_ticket,
    record_audit_event,
)
from options_engine.storage.models import AuditEvent, Exit, Fill, Position, TradeCandidate, TradeTicket
from options_engine.strategy import (
    ExitRecommendationInputs,
    ExitReviewResult,
    ScannedSpread,
    TradeEligibilityDecision,
    evaluate_exit_recommendation,
    evaluate_trade_eligibility,
    scan_spreads,
)

PAPER_FILL_SOURCE = "paper_simulator_conservative"


class PaperTradingError(ValueError):
    """Raised when a paper-trading workflow cannot run safely."""


class PaperFillStatus(StrEnum):
    """Paper fill simulation result states."""

    FILLED = "FILLED"
    NOT_FILLED = "NOT_FILLED"


class PaperTradingStatus(StrEnum):
    """Paper trade workflow lifecycle states."""

    COMPLETED = "COMPLETED"
    NOT_FILLED = "NOT_FILLED"
    NO_CANDIDATE = "NO_CANDIDATE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PaperEntryMarket:
    """Conservative entry market observed for a paper ticket."""

    observed_at: datetime
    natural_credit: Decimal
    mid_credit: Decimal

    def __post_init__(self) -> None:
        _validate_timestamp("observed_at", self.observed_at)
        if self.natural_credit < Decimal("0"):
            raise PaperTradingError("natural_credit must be non-negative")
        if self.mid_credit < Decimal("0"):
            raise PaperTradingError("mid_credit must be non-negative")
        if self.mid_credit < self.natural_credit:
            raise PaperTradingError("mid_credit must be greater than or equal to natural_credit")


@dataclass(frozen=True, slots=True)
class PaperExitMarket:
    """Exit/mark inputs for paper position monitoring and exit recommendation."""

    marked_at: datetime
    current_mark: Decimal
    theoretical_mid: Decimal
    current_short_delta_abs: Decimal
    underlying_close: Decimal
    trend_filter_price: Decimal
    vix_shock: bool = False

    def __post_init__(self) -> None:
        _validate_timestamp("marked_at", self.marked_at)
        if self.current_mark < Decimal("0"):
            raise PaperTradingError("current_mark must be non-negative")
        if self.theoretical_mid <= Decimal("0"):
            raise PaperTradingError("theoretical_mid must be positive")
        if self.current_short_delta_abs < Decimal("0") or self.current_short_delta_abs > Decimal("1"):
            raise PaperTradingError("current_short_delta_abs must be between 0 and 1")
        if self.underlying_close <= Decimal("0"):
            raise PaperTradingError("underlying_close must be positive")
        if self.trend_filter_price <= Decimal("0"):
            raise PaperTradingError("trend_filter_price must be positive")


@dataclass(frozen=True, slots=True)
class PaperFillComparison:
    """Expected-vs-simulated fill comparison for audit."""

    expected_target_credit: Decimal
    worst_acceptable_credit: Decimal
    natural_credit: Decimal
    mid_credit: Decimal
    simulated_credit: Decimal | None
    slippage_vs_target: Decimal | None
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe fill comparison details."""
        return {
            "expected_target_credit": str(self.expected_target_credit),
            "worst_acceptable_credit": str(self.worst_acceptable_credit),
            "natural_credit": str(self.natural_credit),
            "mid_credit": str(self.mid_credit),
            "simulated_credit": None if self.simulated_credit is None else str(self.simulated_credit),
            "slippage_vs_target": None if self.slippage_vs_target is None else str(self.slippage_vs_target),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class PaperFillSimulation:
    """Conservative paper fill result."""

    status: PaperFillStatus
    comparison: PaperFillComparison
    filled_at: datetime

    @property
    def fill_price(self) -> Decimal | None:
        """Return the simulated fill price when filled."""
        return self.comparison.simulated_credit

    def to_audit_event(self, config_version: str, ticket_id: int | None) -> AuditEvent:
        """Convert this fill simulation to an audit event."""
        if not config_version:
            raise PaperTradingError("config_version is required")
        return AuditEvent(
            event_type=f"PAPER_FILL_{self.status.value}",
            entity_type="paper_fill",
            message=f"Paper fill simulation result: {self.status.value}",
            metadata={
                "ticket_id": ticket_id,
                "status": self.status.value,
                "comparison": self.comparison.to_dict(),
                "broker_order_submitted": False,
                "live_orders_allowed": False,
                "conservative_fills_only": True,
                "filled_at": self.filled_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.filled_at,
        )


@dataclass(frozen=True, slots=True)
class PaperTradingRequest:
    """Inputs required to run one daily paper-trading workflow."""

    option_chain: OptionChainSnapshot
    evaluated_at: datetime
    strategy: StrategyDefaults
    data_quality: DataQualityResult
    regime: RegimeLabel
    risk_result: RiskCheckResult
    contracts: int
    account_equity: Decimal
    projected_portfolio_heat: Decimal
    config_version: str
    entry_market: PaperEntryMarket
    exit_market: PaperExitMarket
    kill_switch: KillSwitchDecision | None = None
    risk_budget: Decimal | None = None
    worst_acceptable_credit: Decimal | None = None
    report_output_path: Path | None = None

    def __post_init__(self) -> None:
        _validate_timestamp("evaluated_at", self.evaluated_at)
        if self.contracts < 1:
            raise PaperTradingError("contracts must be at least 1")
        if self.account_equity <= Decimal("0"):
            raise PaperTradingError("account_equity must be positive")
        if self.projected_portfolio_heat < Decimal("0"):
            raise PaperTradingError("projected_portfolio_heat must be non-negative")
        if not self.config_version:
            raise PaperTradingError("config_version is required")


@dataclass(frozen=True, slots=True)
class PaperTradingResult:
    """Result of one daily paper-trading workflow."""

    status: PaperTradingStatus
    scanned_candidates: int
    selected_candidate_id: int | None
    ticket_id: int | None
    fill_id: int | None
    position_id: int | None
    exit_id: int | None
    fill_simulation: PaperFillSimulation | None
    eligibility_decision: TradeEligibilityDecision | None
    exit_recommendation: ExitReviewResult | None
    report: DailyReport
    report_path: Path | None


def simulate_paper_fill(
    ticket: ManualExecutionTicket,
    market: PaperEntryMarket,
) -> PaperFillSimulation:
    """Simulate a paper fill using conservative natural-credit logic only."""
    if market.natural_credit >= ticket.worst_acceptable_credit:
        simulated_credit = min(ticket.target_credit, market.natural_credit)
        comparison = PaperFillComparison(
            expected_target_credit=ticket.target_credit,
            worst_acceptable_credit=ticket.worst_acceptable_credit,
            natural_credit=market.natural_credit,
            mid_credit=market.mid_credit,
            simulated_credit=simulated_credit,
            slippage_vs_target=ticket.target_credit - simulated_credit,
            reason_code="CONSERVATIVE_NATURAL_FILL",
        )
        return PaperFillSimulation(
            status=PaperFillStatus.FILLED,
            comparison=comparison,
            filled_at=market.observed_at,
        )

    reason_code = "UNREALISTIC_MID_REQUIRED" if market.mid_credit >= ticket.worst_acceptable_credit else "LIMIT_NOT_REACHED"
    comparison = PaperFillComparison(
        expected_target_credit=ticket.target_credit,
        worst_acceptable_credit=ticket.worst_acceptable_credit,
        natural_credit=market.natural_credit,
        mid_credit=market.mid_credit,
        simulated_credit=None,
        slippage_vs_target=None,
        reason_code=reason_code,
    )
    return PaperFillSimulation(
        status=PaperFillStatus.NOT_FILLED,
        comparison=comparison,
        filled_at=market.observed_at,
    )


def run_daily_paper_workflow(
    connection: sqlite3.Connection,
    request: PaperTradingRequest,
) -> PaperTradingResult:
    """Run one full local paper-trading lifecycle and persist all artifacts."""
    scan_result = scan_spreads(
        option_chain=request.option_chain,
        evaluated_at=request.evaluated_at,
        regime=request.regime,
        strategy=request.strategy,
        risk_budget=request.risk_budget,
        kill_switch=request.kill_switch,
    )
    candidate_ids = _persist_candidates(connection, tuple(scan_result.spreads), request.config_version)
    if not scan_result.eligible_spreads:
        report, report_path = _build_and_write_report(connection, request)
        return PaperTradingResult(
            status=PaperTradingStatus.NO_CANDIDATE,
            scanned_candidates=len(scan_result.spreads),
            selected_candidate_id=None,
            ticket_id=None,
            fill_id=None,
            position_id=None,
            exit_id=None,
            fill_simulation=None,
            eligibility_decision=None,
            exit_recommendation=None,
            report=report,
            report_path=report_path,
        )

    selected_spread = scan_result.eligible_spreads[0]
    selected_candidate_id = candidate_ids[scan_result.spreads.index(selected_spread)]
    eligibility_decision = evaluate_trade_eligibility(
        scanned_spread=selected_spread,
        data_quality=request.data_quality,
        regime=request.regime,
        risk_result=request.risk_result,
        contracts=request.contracts,
        candidate_id=selected_candidate_id,
        timestamp=request.evaluated_at,
        kill_switch=request.kill_switch,
    )
    if not eligibility_decision.approved:
        report, report_path = _build_and_write_report(connection, request)
        return PaperTradingResult(
            status=PaperTradingStatus.REJECTED,
            scanned_candidates=len(scan_result.spreads),
            selected_candidate_id=selected_candidate_id,
            ticket_id=None,
            fill_id=None,
            position_id=None,
            exit_id=None,
            fill_simulation=None,
            eligibility_decision=eligibility_decision,
            exit_recommendation=None,
            report=report,
            report_path=report_path,
        )

    paper_ticket = create_manual_execution_ticket(
        scanned_spread=selected_spread,
        decision=eligibility_decision,
        account_equity=request.account_equity,
        projected_portfolio_heat=request.projected_portfolio_heat,
        config_version=request.config_version,
        exit_plan="Paper workflow exit engine recommendation required before manual action.",
        worst_acceptable_credit=request.worst_acceptable_credit,
        entry_reason="PAPER_TRADING_ONLY - no broker order submitted",
        created_at=request.evaluated_at,
        kill_switch=request.kill_switch,
    )
    ticket_id = insert_trade_ticket(connection, paper_ticket.to_storage_model())
    record_audit_event(connection, paper_ticket.to_audit_event())

    fill_simulation = simulate_paper_fill(paper_ticket, request.entry_market)
    record_audit_event(connection, fill_simulation.to_audit_event(request.config_version, ticket_id))
    if fill_simulation.status != PaperFillStatus.FILLED or fill_simulation.fill_price is None:
        report, report_path = _build_and_write_report(connection, request)
        return PaperTradingResult(
            status=PaperTradingStatus.NOT_FILLED,
            scanned_candidates=len(scan_result.spreads),
            selected_candidate_id=selected_candidate_id,
            ticket_id=ticket_id,
            fill_id=None,
            position_id=None,
            exit_id=None,
            fill_simulation=fill_simulation,
            eligibility_decision=eligibility_decision,
            exit_recommendation=None,
            report=report,
            report_path=report_path,
        )

    transient_fill = Fill(
        ticket_id=ticket_id,
        position_id=None,
        filled_at=fill_simulation.filled_at,
        quantity=paper_ticket.contracts,
        price=fill_simulation.fill_price,
        source=PAPER_FILL_SOURCE,
        config_version=request.config_version,
    )
    selected_trade_candidate = selected_spread.to_storage_model(request.config_version)
    position_record = add_position_from_filled_ticket(
        fill=transient_fill,
        trade_candidate=selected_trade_candidate,
        config_version=request.config_version,
    )
    position_id = insert_position(connection, position_record.position)
    stored_position = replace(position_record.position, id=position_id)
    stored_fill = replace(transient_fill, position_id=position_id)
    fill_id = insert_fill(connection, stored_fill)
    record_audit_event(connection, position_record.to_audit_event())

    mark_snapshot = update_position_mark(
        position=stored_position,
        fill=stored_fill,
        mark_price=request.exit_market.current_mark,
        theoretical_mid=request.exit_market.theoretical_mid,
        marked_at=request.exit_market.marked_at,
        short_delta=-request.exit_market.current_short_delta_abs,
        current_regime=request.regime,
        multiplier=paper_ticket.multiplier,
    )
    record_audit_event(connection, mark_snapshot.to_audit_event())

    exit_recommendation = evaluate_exit_recommendation(
        ExitRecommendationInputs(
            position=stored_position,
            evaluated_at=request.exit_market.marked_at,
            entry_credit=fill_simulation.fill_price,
            current_mark=request.exit_market.current_mark,
            initial_short_delta_abs=abs(selected_spread.candidate.short_put.delta),
            current_short_delta_abs=request.exit_market.current_short_delta_abs,
            underlying_close=request.exit_market.underlying_close,
            trend_filter_price=request.exit_market.trend_filter_price,
            regime_state=request.regime,
            vix_shock=request.exit_market.vix_shock,
            kill_switch_active=False if request.kill_switch is None else request.kill_switch.risk_off_review_required,
            multiplier=paper_ticket.multiplier,
        )
    )
    exit_id = insert_exit(
        connection,
        exit_recommendation.to_storage_model(request.config_version, position_id=position_id),
    )
    record_audit_event(connection, exit_recommendation.to_audit_event(request.config_version, position_id=position_id))

    report, report_path = _build_and_write_report(connection, request)
    return PaperTradingResult(
        status=PaperTradingStatus.COMPLETED,
        scanned_candidates=len(scan_result.spreads),
        selected_candidate_id=selected_candidate_id,
        ticket_id=ticket_id,
        fill_id=fill_id,
        position_id=position_id,
        exit_id=exit_id,
        fill_simulation=fill_simulation,
        eligibility_decision=eligibility_decision,
        exit_recommendation=exit_recommendation,
        report=report,
        report_path=report_path,
    )


def _persist_candidates(
    connection: sqlite3.Connection,
    spreads: tuple[ScannedSpread, ...],
    config_version: str,
) -> list[int]:
    return [insert_trade_candidate(connection, spread.to_storage_model(config_version)) for spread in spreads]


def _build_and_write_report(
    connection: sqlite3.Connection,
    request: PaperTradingRequest,
) -> tuple[DailyReport, Path | None]:
    report = build_daily_report_from_database(connection, report_date=request.evaluated_at.date())
    if request.report_output_path is None:
        return report, None
    return report, report.write_json(request.report_output_path)


def _validate_timestamp(field_name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PaperTradingError(f"{field_name} must be timezone-aware")
