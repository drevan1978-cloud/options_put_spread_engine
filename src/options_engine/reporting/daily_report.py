"""Daily risk report generation from local storage records."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from options_engine.storage.models import (
    AuditLog,
    Exit,
    Fill,
    Position,
    RegimeState,
    RiskSnapshot,
    TradeCandidate,
    TradeTicket,
)

MISSING = "MISSING"
OPEN_STATUS = "OPEN"
PENDING_TICKET_STATUSES = {"DRAFT", "PENDING"}
WATCHLIST_STATUSES = {"WATCHLIST", "ELIGIBLE_FOR_REVIEW"}
CONTRACT_MULTIPLIER = Decimal("100")
LIVE_FILL_CLASSIFICATION_EVENT = "LIVE_FILL_CLASSIFIED"


@dataclass(frozen=True, slots=True)
class DailyReportInput:
    """Already-loaded local records for one daily risk report."""

    report_date: date
    trade_candidates: list[TradeCandidate] = field(default_factory=list)
    trade_tickets: list[TradeTicket] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    exits: list[Exit] = field(default_factory=list)
    regime_states: list[RegimeState] = field(default_factory=list)
    risk_snapshots: list[RiskSnapshot] = field(default_factory=list)
    audit_logs: list[AuditLog] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DailyReport:
    """Structured daily risk report summary."""

    report_date: date
    candidates_scanned: int
    candidate_status_counts: dict[str, int]
    tickets_drafted: int
    fills_recorded: int
    open_positions: int
    exit_review_counts: dict[str, int]
    rejection_reason_counts: dict[str, int]
    report_issues: tuple[str, ...]
    account_equity: str = MISSING
    current_regime: str = MISSING
    kill_switch_state: str = MISSING
    open_max_loss: str = MISSING
    portfolio_heat: str = MISSING
    risk_by_expiration: dict[str, str] = field(default_factory=dict)
    risk_by_underlying: dict[str, str] = field(default_factory=dict)
    daily_pnl: str = MISSING
    weekly_pnl: str = MISSING
    monthly_drawdown: str = MISSING
    pending_tickets: tuple[dict[str, object], ...] = ()
    rejected_trades: tuple[dict[str, object], ...] = ()
    exit_recommendations: tuple[dict[str, object], ...] = ()
    data_quality_warnings: tuple[str, ...] = (f"{MISSING}: no data quality audit entries found",)
    open_position_details: tuple[dict[str, object], ...] = ()
    clean_pilot_fills: int = 0
    violation_observation_fills: int = 0
    unclassified_fills: int = 0

    def to_markdown(self) -> str:
        """Render the daily risk report as console-friendly Markdown."""
        lines = [
            f"# Daily Report - {self.report_date.isoformat()}",
            "",
            "## Risk State",
            f"- Date: {self.report_date.isoformat()}",
            f"- Account equity: {self.account_equity}",
            f"- Current regime: {self.current_regime}",
            f"- Kill switch state: {self.kill_switch_state}",
            f"- Open max loss: {self.open_max_loss}",
            f"- Portfolio heat: {self.portfolio_heat}",
            f"- Daily PnL: {self.daily_pnl}",
            f"- Weekly PnL: {self.weekly_pnl}",
            f"- Monthly drawdown: {self.monthly_drawdown}",
            "",
            "## Summary",
            f"- Candidates scanned: {self.candidates_scanned}",
            f"- Tickets drafted: {self.tickets_drafted}",
            f"- Fills recorded: {self.fills_recorded}",
            f"- Clean pilot fills: {self.clean_pilot_fills}",
            f"- Violation-observation fills: {self.violation_observation_fills}",
            f"- Unclassified fills: {self.unclassified_fills}",
            f"- Open positions: {self.open_positions}",
            "",
            "## Open Positions",
            *_format_records(self.open_position_details),
            "",
            "## Risk By Expiration",
            *_format_mapping(self.risk_by_expiration),
            "",
            "## Risk By Underlying",
            *_format_mapping(self.risk_by_underlying),
            "",
            "## Pending Tickets",
            *_format_records(self.pending_tickets),
            "",
            "## Rejected Trades",
            *_format_records(self.rejected_trades),
            "",
            "## Exit Recommendations",
            *_format_records(self.exit_recommendations),
            "",
            "## Data Quality Warnings",
            *_format_lines(self.data_quality_warnings),
            "",
            "## Candidate Status Counts",
            *_format_counts(self.candidate_status_counts),
            "",
            "## Exit Review Counts",
            *_format_counts(self.exit_review_counts),
            "",
            "## Rejection Reason Counts",
            *_format_counts(self.rejection_reason_counts),
        ]
        if self.report_issues:
            lines.extend(["", "## Report Issues", *[f"- {issue}" for issue in self.report_issues]])
        return "\n".join(lines)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serializable risk report payload."""
        return {
            "date": self.report_date.isoformat(),
            "account_equity": self.account_equity,
            "current_regime": self.current_regime,
            "kill_switch_state": self.kill_switch_state,
            "open_positions": list(self.open_position_details),
            "open_positions_count": self.open_positions,
            "open_max_loss": self.open_max_loss,
            "portfolio_heat": self.portfolio_heat,
            "risk_by_expiration": self.risk_by_expiration,
            "risk_by_underlying": self.risk_by_underlying,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "monthly_drawdown": self.monthly_drawdown,
            "pending_tickets": list(self.pending_tickets),
            "rejected_trades": list(self.rejected_trades),
            "exit_recommendations": list(self.exit_recommendations),
            "data_quality_warnings": list(self.data_quality_warnings),
            "candidate_status_counts": self.candidate_status_counts,
            "exit_review_counts": self.exit_review_counts,
            "rejection_reason_counts": self.rejection_reason_counts,
            "candidates_scanned": self.candidates_scanned,
            "tickets_drafted": self.tickets_drafted,
            "fills_recorded": self.fills_recorded,
            "clean_pilot_fills": self.clean_pilot_fills,
            "violation_observation_fills": self.violation_observation_fills,
            "unclassified_fills": self.unclassified_fills,
            "report_issues": list(self.report_issues),
        }

    def write_json(self, output_path: Path) -> Path:
        """Write this daily risk report to a JSON file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(self.to_json_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return output_path


def build_daily_report(report_input: DailyReportInput) -> DailyReport:
    """Build a daily risk report from already-loaded local records."""
    candidate_status_counts = Counter(candidate.status for candidate in report_input.trade_candidates)
    exit_review_counts = Counter(exit_review.action for exit_review in report_input.exits)
    rejection_reason_counts: Counter[str] = Counter()
    report_issues: list[str] = []

    rejected_trades = tuple(_rejected_trade_records(report_input.trade_candidates, rejection_reason_counts, report_issues))
    exit_recommendations = tuple(_exit_recommendation_records(report_input.exits, rejection_reason_counts, report_issues))

    tickets_drafted = sum(1 for ticket in report_input.trade_tickets if ticket.status.upper() == "DRAFT")
    open_position_records = [position for position in report_input.positions if position.status.upper() == OPEN_STATUS]
    open_positions = len(open_position_records)
    position_details, open_max_loss, risk_by_expiration, risk_by_underlying = _position_risk_summary(
        open_position_records,
        report_input.fills,
    )

    latest_risk_snapshot = _latest_risk_snapshot(report_input.risk_snapshots)
    risk_details = _risk_details(latest_risk_snapshot, report_issues)
    account_equity = _decimal_or_missing(None if latest_risk_snapshot is None else latest_risk_snapshot.account_equity)
    portfolio_heat = _decimal_or_missing(None if latest_risk_snapshot is None else latest_risk_snapshot.portfolio_heat)
    clean_pilot_fills, violation_observation_fills = _fill_classification_counts(
        report_input.audit_logs,
        report_issues,
    )
    unclassified_fills = max(len(report_input.fills) - clean_pilot_fills - violation_observation_fills, 0)

    return DailyReport(
        report_date=report_input.report_date,
        candidates_scanned=len(report_input.trade_candidates),
        candidate_status_counts=dict(sorted(candidate_status_counts.items())),
        tickets_drafted=tickets_drafted,
        fills_recorded=len(report_input.fills),
        open_positions=open_positions,
        exit_review_counts=dict(sorted(exit_review_counts.items())),
        rejection_reason_counts=dict(sorted(rejection_reason_counts.items())),
        report_issues=tuple(report_issues),
        account_equity=account_equity,
        current_regime=_latest_regime(report_input.regime_states),
        kill_switch_state=_latest_kill_switch_state(report_input.audit_logs),
        open_max_loss=open_max_loss,
        portfolio_heat=portfolio_heat,
        risk_by_expiration=risk_by_expiration,
        risk_by_underlying=risk_by_underlying,
        daily_pnl=_detail_value(risk_details, "daily_pnl"),
        weekly_pnl=_detail_value(risk_details, "weekly_pnl", fallback_key="weekly_realized_pnl"),
        monthly_drawdown=_detail_value(risk_details, "monthly_drawdown"),
        pending_tickets=tuple(_pending_ticket_records(report_input.trade_tickets)),
        rejected_trades=rejected_trades,
        exit_recommendations=exit_recommendations,
        data_quality_warnings=tuple(_data_quality_warnings(report_input.audit_logs, report_issues)),
        open_position_details=tuple(position_details),
        clean_pilot_fills=clean_pilot_fills,
        violation_observation_fills=violation_observation_fills,
        unclassified_fills=unclassified_fills,
    )


def load_daily_report_input(
    connection: sqlite3.Connection,
    report_date: date,
    *,
    as_of: datetime | None = None,
) -> DailyReportInput:
    """Load local database records needed for one daily risk report."""
    if as_of is not None:
        _require_aware_datetime(as_of, "as_of")
    report_date_text = report_date.isoformat()
    as_of_text = None if as_of is None else as_of.isoformat()
    return DailyReportInput(
        report_date=report_date,
        trade_candidates=_load_trade_candidates(connection, report_date_text, as_of_text),
        trade_tickets=_load_trade_tickets(connection, report_date_text, as_of_text),
        fills=_load_fills(connection, report_date_text, as_of_text),
        positions=_load_open_positions(connection, as_of_text),
        exits=_load_exits(connection, report_date_text, as_of_text),
        regime_states=_load_regime_states(connection, report_date_text, as_of_text),
        risk_snapshots=_load_risk_snapshots(connection, report_date_text, as_of_text),
        audit_logs=_load_audit_logs(connection, report_date_text, as_of_text),
    )


def build_daily_report_from_database(
    connection: sqlite3.Connection,
    report_date: date,
    *,
    as_of: datetime | None = None,
) -> DailyReport:
    """Build a daily risk report from local SQLite storage."""
    return build_daily_report(load_daily_report_input(connection, report_date, as_of=as_of))


def write_daily_report_json(report: DailyReport, output_path: Path) -> Path:
    """Write a daily risk report to JSON."""
    return report.write_json(output_path)


def _rejected_trade_records(
    trade_candidates: list[TradeCandidate],
    rejection_reason_counts: Counter[str],
    report_issues: list[str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for candidate in trade_candidates:
        reason_codes = _candidate_reason_codes(candidate, report_issues)
        for code in reason_codes:
            if not code.startswith(MISSING):
                rejection_reason_counts[code] += 1

        if candidate.status.upper() not in WATCHLIST_STATUSES:
            records.append(
                {
                    "id": candidate.id,
                    "symbol": candidate.symbol,
                    "expiration_date": candidate.expiration_date.isoformat(),
                    "short_put_strike": str(candidate.short_put_strike),
                    "long_put_strike": str(candidate.long_put_strike),
                    "status": candidate.status,
                    "reason_codes": reason_codes or [f"{MISSING}: no rejection reason recorded"],
                }
            )
    return records


def _candidate_reason_codes(candidate: TradeCandidate, report_issues: list[str]) -> list[str]:
    payload = _load_json(candidate.reason_json, f"trade_candidate:{candidate.id}", report_issues)
    if payload is None:
        return []

    rejection_reasons = payload.get("rejection_reasons", [])
    if not isinstance(rejection_reasons, list):
        report_issues.append(f"trade_candidate:{candidate.id} rejection_reasons is not a list")
        return []

    reason_codes: list[str] = []
    for reason in rejection_reasons:
        if isinstance(reason, dict) and isinstance(reason.get("code"), str):
            reason_codes.append(reason["code"])
        else:
            report_issues.append(f"trade_candidate:{candidate.id} has malformed rejection reason")
    return reason_codes


def _exit_recommendation_records(
    exits: list[Exit],
    rejection_reason_counts: Counter[str],
    report_issues: list[str],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for exit_review in exits:
        reason_codes = _exit_reason_codes(exit_review, report_issues)
        for code in reason_codes:
            rejection_reason_counts[code] += 1
        records.append(
            {
                "id": exit_review.id,
                "position_id": exit_review.position_id,
                "evaluated_at": exit_review.evaluated_at.isoformat(),
                "action": exit_review.action,
                "reason_codes": reason_codes or [f"{MISSING}: no exit reason recorded"],
            }
        )
    return records


def _exit_reason_codes(exit_review: Exit, report_issues: list[str]) -> list[str]:
    payload = _load_json(exit_review.reason_json, f"exit:{exit_review.id}", report_issues)
    if payload is None:
        return []

    reasons = payload.get("reasons", [])
    if not isinstance(reasons, list):
        report_issues.append(f"exit:{exit_review.id} reasons is not a list")
        return []

    reason_codes: list[str] = []
    for reason in reasons:
        if isinstance(reason, dict) and isinstance(reason.get("code"), str):
            reason_codes.append(reason["code"])
        else:
            report_issues.append(f"exit:{exit_review.id} has malformed reason")
    return reason_codes


def _position_risk_summary(
    open_positions: list[Position],
    fills: list[Fill],
) -> tuple[list[dict[str, object]], str, dict[str, str], dict[str, str]]:
    details: list[dict[str, object]] = []
    risk_by_expiration: dict[str, Decimal] = {}
    risk_by_underlying: dict[str, Decimal] = {}
    open_max_loss = Decimal("0")
    missing_max_loss = False

    fills_by_position = _fills_by_position(fills)
    for position in open_positions:
        max_loss = _position_max_loss(position, fills_by_position)
        max_loss_text = _decimal_or_missing(max_loss)
        if max_loss is None:
            missing_max_loss = True
        else:
            open_max_loss += max_loss
            expiration_key = position.expiration_date.isoformat()
            underlying_key = position.symbol.strip().upper()
            risk_by_expiration[expiration_key] = risk_by_expiration.get(expiration_key, Decimal("0")) + max_loss
            risk_by_underlying[underlying_key] = risk_by_underlying.get(underlying_key, Decimal("0")) + max_loss

        details.append(
            {
                "id": position.id,
                "symbol": position.symbol,
                "quantity": position.quantity,
                "short_put_strike": str(position.short_put_strike),
                "long_put_strike": str(position.long_put_strike),
                "expiration_date": position.expiration_date.isoformat(),
                "status": position.status,
                "max_loss": max_loss_text,
            }
        )

    open_max_loss_text = MISSING if missing_max_loss and open_positions else _format_decimal(open_max_loss)
    return (
        details,
        open_max_loss_text,
        {key: _format_decimal(value) for key, value in sorted(risk_by_expiration.items())},
        {key: _format_decimal(value) for key, value in sorted(risk_by_underlying.items())},
    )


def _fills_by_position(fills: list[Fill]) -> dict[int, list[Fill]]:
    grouped: dict[int, list[Fill]] = {}
    for fill in fills:
        if fill.position_id is not None:
            grouped.setdefault(fill.position_id, []).append(fill)
    return grouped


def _position_max_loss(position: Position, fills_by_position: dict[int, list[Fill]]) -> Decimal | None:
    if position.id is None:
        return None
    position_fills = fills_by_position.get(position.id)
    if not position_fills:
        return None

    entry_credit = position_fills[0].price
    width = position.short_put_strike - position.long_put_strike
    max_loss_per_spread = width - entry_credit
    if width <= Decimal("0") or max_loss_per_spread <= Decimal("0"):
        return None
    return max_loss_per_spread * Decimal(position.quantity) * CONTRACT_MULTIPLIER


def _pending_ticket_records(trade_tickets: list[TradeTicket]) -> list[dict[str, object]]:
    return [
        {
            "id": ticket.id,
            "candidate_id": ticket.candidate_id,
            "symbol": ticket.symbol,
            "order_type": ticket.order_type,
            "limit_price": str(ticket.limit_price),
            "status": ticket.status,
            "created_at": ticket.created_at.isoformat(),
        }
        for ticket in trade_tickets
        if ticket.status.upper() in PENDING_TICKET_STATUSES
    ]


def _latest_risk_snapshot(risk_snapshots: list[RiskSnapshot]) -> RiskSnapshot | None:
    if not risk_snapshots:
        return None
    return max(risk_snapshots, key=lambda snapshot: (snapshot.as_of, snapshot.id or 0))


def _risk_details(risk_snapshot: RiskSnapshot | None, report_issues: list[str]) -> dict[str, object]:
    if risk_snapshot is None:
        return {}
    return _load_json(risk_snapshot.details_json, f"risk_snapshot:{risk_snapshot.id}", report_issues) or {}


def _latest_regime(regime_states: list[RegimeState]) -> str:
    if not regime_states:
        return MISSING
    latest = max(regime_states, key=lambda state: (state.as_of, state.id or 0))
    return latest.regime


def _latest_kill_switch_state(audit_logs: list[AuditLog]) -> str:
    kill_switch_logs = [
        audit_log
        for audit_log in audit_logs
        if audit_log.entity_type == "kill_switch" and audit_log.event_type.startswith("KILL_SWITCH_")
    ]
    if not kill_switch_logs:
        return MISSING

    latest = max(kill_switch_logs, key=lambda audit_log: (audit_log.created_at, audit_log.id or 0))
    payload = _safe_audit_metadata(latest)
    state = payload.get("state")
    return state if isinstance(state, str) and state else MISSING


def _data_quality_warnings(audit_logs: list[AuditLog], report_issues: list[str]) -> list[str]:
    warnings: list[str] = []
    data_quality_seen = False
    for audit_log in audit_logs:
        if audit_log.entity_type != "data_quality" and not audit_log.event_type.startswith("DATA_QUALITY"):
            continue
        data_quality_seen = True
        metadata = _safe_audit_metadata(audit_log)
        if not metadata:
            report_issues.append(f"audit_log:{audit_log.id} data quality metadata is missing or malformed")
            continue
        severity = str(metadata.get("severity", MISSING))
        passed = metadata.get("passed")
        if severity in {"WARNING", "ERROR", "CRITICAL"} or passed is False:
            reason_code = str(metadata.get("reason_code", MISSING))
            message = str(metadata.get("message", MISSING))
            warnings.append(f"{severity}: {reason_code} - {message}")

    if not data_quality_seen:
        return [f"{MISSING}: no data quality audit entries found"]
    return warnings


def _fill_classification_counts(audit_logs: list[AuditLog], report_issues: list[str]) -> tuple[int, int]:
    clean_count = 0
    violation_count = 0
    for audit_log in audit_logs:
        if audit_log.event_type != LIVE_FILL_CLASSIFICATION_EVENT:
            continue
        metadata = _safe_audit_metadata(audit_log)
        if not metadata:
            report_issues.append(f"audit_log:{audit_log.id} live fill classification metadata is missing or malformed")
            continue
        classification = metadata.get("classification")
        if classification == "CLEAN_PILOT_FILL" or metadata.get("valid_for_pilot") is True:
            clean_count += 1
        elif classification == "VIOLATION_OBSERVATION_FILL" or metadata.get("valid_for_pilot") is False:
            violation_count += 1
        else:
            report_issues.append(f"audit_log:{audit_log.id} live fill classification is missing or unknown")
    return clean_count, violation_count


def _safe_audit_metadata(audit_log: AuditLog) -> dict[str, object]:
    try:
        payload = json.loads(audit_log.payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("metadata", payload)
    return metadata if isinstance(metadata, dict) else {}


def _detail_value(details: dict[str, object], key: str, fallback_key: str | None = None) -> str:
    value = details.get(key)
    if value is None and fallback_key is not None:
        value = details.get(fallback_key)
    if value is None:
        return MISSING
    return str(value)


def _load_json(raw_json: str, label: str, report_issues: list[str]) -> dict[str, object] | None:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        report_issues.append(f"{label} contains malformed JSON")
        return None

    if not isinstance(payload, dict):
        report_issues.append(f"{label} JSON payload is not an object")
        return None
    return payload


def _format_counts(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["- None"]
    return [f"- {key}: {value}" for key, value in counts.items()]


def _format_mapping(values: dict[str, str]) -> list[str]:
    if not values:
        return ["- None"]
    return [f"- {key}: {value}" for key, value in values.items()]


def _format_records(records: tuple[dict[str, object], ...] | list[dict[str, object]]) -> list[str]:
    if not records:
        return ["- None"]
    return [f"- {json.dumps(record, sort_keys=True)}" for record in records]


def _format_lines(values: tuple[str, ...] | list[str]) -> list[str]:
    if not values:
        return ["- None"]
    return [f"- {value}" for value in values]


def _decimal_or_missing(value: Decimal | None) -> str:
    return MISSING if value is None else _format_decimal(value)


def _format_decimal(value: Decimal) -> str:
    return str(value)


def _load_trade_candidates(
    connection: sqlite3.Connection,
    report_date_text: str,
    as_of_text: str | None,
) -> list[TradeCandidate]:
    rows = connection.execute(
        """
        SELECT
            id,
            symbol,
            expiration_date,
            short_put_strike,
            long_put_strike,
            max_loss,
            status,
            reason_json,
            config_version,
            created_at
        FROM trade_candidates
        WHERE substr(created_at, 1, 10) = ?
          AND (? IS NULL OR created_at <= ?)
        ORDER BY id
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        TradeCandidate(
            id=row[0],
            symbol=row[1],
            expiration_date=_parse_date(row[2]),
            short_put_strike=Decimal(row[3]),
            long_put_strike=Decimal(row[4]),
            max_loss=Decimal(row[5]),
            status=row[6],
            reason_json=row[7],
            config_version=row[8],
            created_at=_parse_datetime(row[9]),
        )
        for row in rows
    ]


def _load_trade_tickets(
    connection: sqlite3.Connection,
    report_date_text: str,
    as_of_text: str | None,
) -> list[TradeTicket]:
    rows = connection.execute(
        """
        SELECT
            id,
            candidate_id,
            symbol,
            order_type,
            limit_price,
            status,
            notes,
            config_version,
            created_at
        FROM trade_tickets
        WHERE substr(created_at, 1, 10) = ?
          AND (? IS NULL OR created_at <= ?)
        ORDER BY id
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        TradeTicket(
            id=row[0],
            candidate_id=row[1],
            symbol=row[2],
            order_type=row[3],
            limit_price=Decimal(row[4]),
            status=row[5],
            notes=row[6],
            config_version=row[7],
            created_at=_parse_datetime(row[8]),
        )
        for row in rows
    ]


def _load_fills(connection: sqlite3.Connection, report_date_text: str, as_of_text: str | None) -> list[Fill]:
    rows = connection.execute(
        """
        SELECT
            id,
            ticket_id,
            position_id,
            filled_at,
            quantity,
            price,
            source,
            config_version,
            created_at
        FROM fills
        WHERE substr(filled_at, 1, 10) = ?
          AND (? IS NULL OR filled_at <= ?)
        ORDER BY id
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        Fill(
            id=row[0],
            ticket_id=row[1],
            position_id=row[2],
            filled_at=_parse_datetime(row[3]),
            quantity=row[4],
            price=Decimal(row[5]),
            source=row[6],
            config_version=row[7],
            created_at=_parse_datetime(row[8]),
        )
        for row in rows
    ]


def _load_open_positions(connection: sqlite3.Connection, as_of_text: str | None) -> list[Position]:
    rows = connection.execute(
        """
        SELECT
            id,
            symbol,
            opened_at,
            closed_at,
            quantity,
            short_put_strike,
            long_put_strike,
            expiration_date,
            status,
            config_version,
            created_at
        FROM positions
        WHERE upper(status) = 'OPEN'
          AND (? IS NULL OR opened_at <= ?)
        ORDER BY id
        """,
        (as_of_text, as_of_text),
    ).fetchall()
    return [
        Position(
            id=row[0],
            symbol=row[1],
            opened_at=_parse_datetime(row[2]),
            closed_at=None if row[3] is None else _parse_datetime(row[3]),
            quantity=row[4],
            short_put_strike=Decimal(row[5]),
            long_put_strike=Decimal(row[6]),
            expiration_date=_parse_date(row[7]),
            status=row[8],
            config_version=row[9],
            created_at=_parse_datetime(row[10]),
        )
        for row in rows
    ]


def _load_exits(connection: sqlite3.Connection, report_date_text: str, as_of_text: str | None) -> list[Exit]:
    rows = connection.execute(
        """
        SELECT
            id,
            position_id,
            evaluated_at,
            action,
            reason_json,
            config_version,
            created_at
        FROM exits
        WHERE substr(evaluated_at, 1, 10) = ?
          AND (? IS NULL OR evaluated_at <= ?)
        ORDER BY id
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        Exit(
            id=row[0],
            position_id=row[1],
            evaluated_at=_parse_datetime(row[2]),
            action=row[3],
            reason_json=row[4],
            config_version=row[5],
            created_at=_parse_datetime(row[6]),
        )
        for row in rows
    ]


def _load_regime_states(connection: sqlite3.Connection, report_date_text: str, as_of_text: str | None) -> list[RegimeState]:
    rows = connection.execute(
        """
        SELECT id, symbol, as_of, regime, details_json, config_version, created_at
        FROM regime_states
        WHERE substr(as_of, 1, 10) <= ?
          AND (? IS NULL OR as_of <= ?)
        ORDER BY as_of ASC, id ASC
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        RegimeState(
            id=row[0],
            symbol=row[1],
            as_of=_parse_datetime(row[2]),
            regime=row[3],
            details_json=row[4],
            config_version=row[5],
            created_at=_parse_datetime(row[6]),
        )
        for row in rows
    ]


def _load_risk_snapshots(connection: sqlite3.Connection, report_date_text: str, as_of_text: str | None) -> list[RiskSnapshot]:
    rows = connection.execute(
        """
        SELECT id, as_of, account_equity, portfolio_heat, details_json, config_version, created_at
        FROM risk_snapshots
        WHERE substr(as_of, 1, 10) <= ?
          AND (? IS NULL OR as_of <= ?)
        ORDER BY as_of ASC, id ASC
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        RiskSnapshot(
            id=row[0],
            as_of=_parse_datetime(row[1]),
            account_equity=Decimal(row[2]),
            portfolio_heat=Decimal(row[3]),
            details_json=row[4],
            config_version=row[5],
            created_at=_parse_datetime(row[6]),
        )
        for row in rows
    ]


def _load_audit_logs(connection: sqlite3.Connection, report_date_text: str, as_of_text: str | None) -> list[AuditLog]:
    rows = connection.execute(
        """
        SELECT id, event_type, entity_type, message, payload_json, config_version, created_at
        FROM audit_log
        WHERE substr(created_at, 1, 10) = ?
          AND (? IS NULL OR created_at <= ?)
        ORDER BY created_at ASC, id ASC
        """,
        (report_date_text, as_of_text, as_of_text),
    ).fetchall()
    return [
        AuditLog(
            id=row[0],
            event_type=row[1],
            entity_type=row[2],
            message=row[3],
            payload_json=row[4],
            config_version=row[5],
            created_at=_parse_datetime(row[6]),
        )
        for row in rows
    ]


def _parse_date(raw_value: str) -> date:
    return date.fromisoformat(raw_value)


def _parse_datetime(raw_value: str) -> datetime:
    normalized_value = f"{raw_value[:-1]}+00:00" if raw_value.endswith("Z") else raw_value
    return datetime.fromisoformat(normalized_value)


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
