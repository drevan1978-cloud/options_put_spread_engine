"""Command line entry point for local audit reporting and manual live observation."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from options_engine.live import (
    DailyPilotSignoff,
    LiveFillEntry,
    LiveOrderType,
    LivePilotDryRunRequest,
    PilotDemoPacketRequest,
    PilotSessionReasonCode,
    PilotSessionStartRequest,
    ReleaseGateInput,
    ReleaseGateReport,
    build_gated_live_risk_dashboard_from_database,
    build_live_risk_dashboard_from_database,
    build_pilot_demo_packet,
    build_pilot_evidence_packet,
    evaluate_pilot_release_gate,
    record_daily_operator_signoff,
    record_live_fill_for_active_session,
    record_pilot_reset_review,
    resume_pilot_session,
    run_live_pilot_readiness_dry_run,
    start_pilot_session,
    stop_pilot_session,
)
from options_engine.reporting import build_daily_report_from_database
from options_engine.storage.database import connect_database, initialize_database, record_audit_event


def main(argv: Sequence[str] | None = None) -> int:
    """Run the read-only application CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "daily-report":
        return _run_daily_report(args)
    if args.command == "risk-dashboard":
        return _run_risk_dashboard(args)
    if args.command == "live-fill":
        return _run_live_fill(args)
    if args.command == "readiness-dry-run":
        return _run_readiness_dry_run(args)
    if args.command == "pilot-start":
        return _run_pilot_start(args)
    if args.command == "pilot-stop":
        return _run_pilot_stop(args)
    if args.command == "pilot-reset-review":
        return _run_pilot_reset_review(args)
    if args.command == "pilot-resume":
        return _run_pilot_resume(args)
    if args.command == "pilot-live-fill":
        return _run_pilot_live_fill(args)
    if args.command == "pilot-dashboard":
        return _run_pilot_dashboard(args)
    if args.command == "pilot-signoff":
        return _run_pilot_signoff(args)
    if args.command == "pilot-evidence":
        return _run_pilot_evidence(args)
    if args.command == "build-pilot-demo":
        return _run_build_pilot_demo(args)
    if args.command == "pilot-release-gate":
        return _run_pilot_release_gate(args)

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="options-engine",
        description="Local audit utilities for the options put spread engine. No broker orders are submitted.",
    )
    subparsers = parser.add_subparsers(dest="command")

    report_parser = subparsers.add_parser("daily-report", help="Print a Markdown daily audit report from SQLite.")
    report_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    report_parser.add_argument("--date", required=True, type=_parse_date, help="Report date in YYYY-MM-DD format.")

    dashboard_parser = subparsers.add_parser(
        "risk-dashboard",
        help="Print the manual live-pilot risk dashboard from SQLite.",
    )
    dashboard_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    dashboard_parser.add_argument("--date", required=True, type=_parse_date, help="Dashboard date in YYYY-MM-DD format.")
    dashboard_parser.add_argument(
        "--pilot-started-at",
        type=_parse_datetime,
        help="Optional pilot start timestamp in ISO-8601 format.",
    )

    fill_parser = subparsers.add_parser(
        "live-fill",
        help="Record an observed manual live fill through the active pilot session gate.",
    )
    fill_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    fill_parser.add_argument("--config-version", required=True, help="Locked config version used for the fill.")
    fill_parser.add_argument("--pilot-id", required=True, help="Expected active pilot session id.")
    fill_parser.add_argument("--ticket-id", type=int, help="Manual ticket id associated with the fill.")
    fill_parser.add_argument("--position-id", type=int, help="Position id associated with the fill.")
    fill_parser.add_argument("--filled-at", required=True, type=_parse_datetime, help="Fill timestamp in ISO-8601 format.")
    fill_parser.add_argument("--quantity", required=True, type=int, help="Observed filled spread quantity.")
    fill_parser.add_argument("--price", required=True, type=_parse_decimal, help="Observed credit fill price.")
    fill_parser.add_argument(
        "--expected-credit",
        required=True,
        type=_parse_decimal,
        help="Expected/manual ticket credit for slippage tracking.",
    )
    fill_parser.add_argument("--source", default="manual_live_entry", help="Manual source label.")
    fill_parser.add_argument(
        "--order-type",
        default=LiveOrderType.LIMIT.value,
        choices=[order_type.value for order_type in LiveOrderType],
        help="Observed manual order type.",
    )
    fill_parser.add_argument(
        "--execution-kill-switch-state",
        default="GREEN",
        choices=["GREEN", "YELLOW", "RED", "BLACK"],
        help="Kill-switch state at time of manual execution.",
    )
    fill_parser.add_argument(
        "--manual-execution-confirmed",
        action="store_true",
        help="Confirm the fill came from manual operator execution.",
    )
    fill_parser.add_argument(
        "--critical-system-error",
        action="store_true",
        help="Mark this fill as associated with a critical system error.",
    )
    fill_parser.add_argument(
        "--risk-rule-violation",
        action="store_true",
        help="Mark this fill as associated with a risk-rule violation.",
    )

    dry_run_parser = subparsers.add_parser(
        "readiness-dry-run",
        help="Run an auditable no-money live-pilot readiness rehearsal.",
    )
    dry_run_parser.add_argument("--database", required=True, type=Path, help="Path to a local SQLite database.")
    dry_run_parser.add_argument("--config-version", required=True, help="Locked config version to rehearse.")
    dry_run_parser.add_argument("--date", required=True, type=_parse_date, help="Dry-run report date in YYYY-MM-DD format.")
    dry_run_parser.add_argument("--operator", required=True, help="Operator name or initials for the dry run.")
    dry_run_parser.add_argument(
        "--run-at",
        type=_parse_datetime,
        help="Optional dry-run timestamp in ISO-8601 format. Defaults to current UTC time.",
    )
    dry_run_parser.add_argument(
        "--account-equity",
        default=Decimal("100000"),
        type=_parse_decimal,
        help="Dry-run account equity used for the readiness dashboard.",
    )
    dry_run_parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path to write the readiness report JSON.",
    )

    start_parser = subparsers.add_parser(
        "pilot-start",
        help="Start one immutable manual live-pilot session.",
    )
    start_parser.add_argument("--database", required=True, type=Path, help="Path to a local SQLite database.")
    start_parser.add_argument("--pilot-id", required=True, help="Operator-defined pilot session id.")
    start_parser.add_argument("--operator", required=True, help="Operator name or initials.")
    start_parser.add_argument("--config-version", required=True, help="Locked config version for the pilot.")
    start_parser.add_argument(
        "--started-at",
        type=_parse_datetime,
        help="Optional session start timestamp in ISO-8601 format.",
    )

    stop_parser = subparsers.add_parser(
        "pilot-stop",
        help="Stop a manual live-pilot session with an explicit reason.",
    )
    stop_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    stop_parser.add_argument("--pilot-id", required=True, help="Pilot session id.")
    stop_parser.add_argument("--config-version", required=True, help="Config version for audit.")
    stop_parser.add_argument("--reason", required=True, help="Explicit stop reason.")
    stop_parser.add_argument(
        "--reason-code",
        default=PilotSessionReasonCode.OPERATOR_STOP.value,
        choices=[code.value for code in PilotSessionReasonCode],
        help="Stable stop reason code.",
    )
    stop_parser.add_argument("--stopped-at", type=_parse_datetime, help="Optional stop timestamp.")

    reset_parser = subparsers.add_parser(
        "pilot-reset-review",
        help="Record reset review required before resuming after hard stops.",
    )
    reset_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    reset_parser.add_argument("--pilot-id", required=True, help="Pilot session id.")
    reset_parser.add_argument("--config-version", required=True, help="Config version for audit.")
    reset_parser.add_argument("--review-note", required=True, help="Explicit reset review note.")
    reset_parser.add_argument("--reviewed-at", type=_parse_datetime, help="Optional review timestamp.")

    resume_parser = subparsers.add_parser(
        "pilot-resume",
        help="Resume a stopped pilot session after explicit review.",
    )
    resume_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    resume_parser.add_argument("--pilot-id", required=True, help="Pilot session id.")
    resume_parser.add_argument("--config-version", required=True, help="Config version for audit.")
    resume_parser.add_argument("--review-note", required=True, help="Explicit resume review note.")
    resume_parser.add_argument("--resumed-at", type=_parse_datetime, help="Optional resume timestamp.")

    pilot_fill_parser = subparsers.add_parser(
        "pilot-live-fill",
        help="Record an observed manual live fill through the active pilot session gate.",
    )
    pilot_fill_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    pilot_fill_parser.add_argument("--config-version", required=True, help="Locked config version used for the fill.")
    pilot_fill_parser.add_argument("--pilot-id", help="Expected active pilot session id.")
    pilot_fill_parser.add_argument("--ticket-id", type=int, help="Manual ticket id associated with the fill.")
    pilot_fill_parser.add_argument("--position-id", type=int, help="Position id associated with the fill.")
    pilot_fill_parser.add_argument("--filled-at", required=True, type=_parse_datetime, help="Fill timestamp in ISO-8601 format.")
    pilot_fill_parser.add_argument("--quantity", required=True, type=int, help="Observed filled spread quantity.")
    pilot_fill_parser.add_argument("--price", required=True, type=_parse_decimal, help="Observed credit fill price.")
    pilot_fill_parser.add_argument("--expected-credit", required=True, type=_parse_decimal, help="Expected credit.")
    pilot_fill_parser.add_argument("--source", default="manual_live_entry", help="Manual source label.")
    pilot_fill_parser.add_argument(
        "--order-type",
        default=LiveOrderType.LIMIT.value,
        choices=[order_type.value for order_type in LiveOrderType],
        help="Observed manual order type.",
    )
    pilot_fill_parser.add_argument(
        "--execution-kill-switch-state",
        default="GREEN",
        choices=["GREEN", "YELLOW", "RED", "BLACK"],
        help="Kill-switch state at time of manual execution.",
    )
    pilot_fill_parser.add_argument("--manual-execution-confirmed", action="store_true", help="Confirm manual execution.")
    pilot_fill_parser.add_argument("--critical-system-error", action="store_true", help="Mark critical system error.")
    pilot_fill_parser.add_argument("--risk-rule-violation", action="store_true", help="Mark risk-rule violation.")

    pilot_dashboard_parser = subparsers.add_parser(
        "pilot-dashboard",
        help="Print a session-gated manual live-pilot risk dashboard.",
    )
    pilot_dashboard_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    pilot_dashboard_parser.add_argument("--date", required=True, type=_parse_date, help="Dashboard date.")
    pilot_dashboard_parser.add_argument("--config-version", required=True, help="Runtime config version.")
    pilot_dashboard_parser.add_argument("--pilot-id", help="Expected active pilot session id.")
    pilot_dashboard_parser.add_argument("--generated-at", type=_parse_datetime, help="Optional dashboard timestamp.")

    signoff_parser = subparsers.add_parser(
        "pilot-signoff",
        help="Record required daily operator signoff for the live pilot.",
    )
    signoff_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    signoff_parser.add_argument("--pilot-id", required=True, help="Pilot session id.")
    signoff_parser.add_argument("--config-version", required=True, help="Config version for audit.")
    signoff_parser.add_argument("--date", required=True, type=_parse_date, help="Signoff date.")
    signoff_parser.add_argument("--operator", required=True, help="Operator name or initials.")
    signoff_parser.add_argument("--notes", required=True, help="End-of-day signoff notes.")
    signoff_parser.add_argument("--signed-at", type=_parse_datetime, help="Optional signoff timestamp.")
    signoff_parser.add_argument("--report-reviewed", action="store_true", help="Confirm report review.")
    signoff_parser.add_argument("--positions-reconciled", action="store_true", help="Confirm position reconciliation.")
    signoff_parser.add_argument("--slippage-reviewed", action="store_true", help="Confirm slippage review.")
    signoff_parser.add_argument("--violations-reviewed", action="store_true", help="Confirm violation review.")

    evidence_parser = subparsers.add_parser(
        "pilot-evidence",
        help="Export a JSON evidence packet for one pilot session.",
    )
    evidence_parser.add_argument("--database", required=True, type=Path, help="Path to an existing SQLite database.")
    evidence_parser.add_argument("--pilot-id", required=True, help="Pilot session id.")
    evidence_parser.add_argument("--output-json", required=True, type=Path, help="Evidence packet JSON path.")
    evidence_parser.add_argument("--generated-at", type=_parse_datetime, help="Optional evidence timestamp.")

    demo_parser = subparsers.add_parser(
        "build-pilot-demo",
        help="Build a local first-pilot simulation packet with demo database and artifacts.",
    )
    demo_parser.add_argument("--database", required=True, type=Path, help="Path to create the demo SQLite database.")
    demo_parser.add_argument("--output-dir", required=True, type=Path, help="Directory for demo artifacts.")
    demo_parser.add_argument("--pilot-id", default="pilot-demo-001", help="Demo pilot session id.")
    demo_parser.add_argument("--operator", default="demo-operator", help="Demo operator name or initials.")
    demo_parser.add_argument("--config-version", default="demo-config-v1", help="Demo locked config version.")
    demo_parser.add_argument("--run-at", type=_parse_datetime, help="Optional demo timestamp.")
    demo_parser.add_argument("--date", type=_parse_date, help="Optional demo report date.")

    release_gate_parser = subparsers.add_parser(
        "pilot-release-gate",
        help="Evaluate the final manual live-pilot release gate.",
    )
    release_gate_parser.add_argument("--database", required=True, type=Path, help="Path to the pilot SQLite database.")
    release_gate_parser.add_argument("--readiness", required=True, type=Path, help="Readiness dry-run JSON report.")
    release_gate_parser.add_argument("--evidence", required=True, type=Path, help="Pilot evidence packet JSON.")
    release_gate_parser.add_argument("--output-json", required=True, type=Path, help="Release gate JSON output path.")
    release_gate_parser.add_argument("--output-markdown", type=Path, help="Optional Markdown output path.")
    release_gate_parser.add_argument("--generated-at", type=_parse_datetime, help="Optional gate timestamp.")
    release_gate_parser.add_argument(
        "--full-test-suite-passed",
        action="store_true",
        help="Operator assertion that the full pytest suite passed.",
    )
    release_gate_parser.add_argument(
        "--runbook-acknowledged",
        action="store_true",
        help="Operator acknowledgement of the live pilot runbook.",
    )
    release_gate_parser.add_argument(
        "--account-equity-present",
        action="store_true",
        help="Operator confirmation that account equity is present and current.",
    )
    release_gate_parser.add_argument(
        "--open-positions-verified",
        action="store_true",
        help="Operator confirmation that open positions/open risk are verified.",
    )
    release_gate_parser.add_argument(
        "--max-evidence-age-days",
        default=7,
        type=int,
        help="Maximum allowed evidence packet age in days.",
    )
    return parser


def _run_daily_report(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    report_date: date = args.date

    if not database_path.exists():
        raise SystemExit(f"database does not exist: {database_path}")

    with connect_database(database_path) as connection:
        report = build_daily_report_from_database(connection, report_date)

    print(report.to_markdown())
    return 0


def _run_risk_dashboard(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    report_date: date = args.date

    if not database_path.exists():
        raise SystemExit(f"database does not exist: {database_path}")

    with connect_database(database_path) as connection:
        dashboard = build_live_risk_dashboard_from_database(
            connection,
            report_date,
            pilot_started_at=args.pilot_started_at,
        )

    print(dashboard.to_markdown())
    return 0


def _run_live_fill(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    entry = _live_fill_entry_from_args(args)
    with connect_database(database_path) as connection:
        result = record_live_fill_for_active_session(
            connection,
            entry,
            config_version=args.config_version,
            pilot_id=args.pilot_id,
        )

    print(f"Session-gated live fill recorded: fill_id={result.fill_result.fill_id}")
    print(f"Pilot gate: {result.gate_decision.status.value}")
    print(f"Slippage: {result.fill_result.slippage.slippage}")
    print(f"Pilot stopped: {result.stop_audit_id is not None}")
    if result.fill_result.violations:
        print("Rule violations:")
        for violation in result.fill_result.violations:
            print(f"- {violation.code.value}: {violation.message}")
    return 0


def _run_readiness_dry_run(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    if not database_path.exists():
        initialize_database(database_path)

    request = LivePilotDryRunRequest(
        report_date=args.date,
        run_at=args.run_at or datetime.now().astimezone(),
        config_version=args.config_version,
        operator=args.operator,
        account_equity=args.account_equity,
    )
    with connect_database(database_path) as connection:
        report = run_live_pilot_readiness_dry_run(connection, request)

    if args.output_json is not None:
        report.write_json(args.output_json)

    print(report.to_markdown())
    return 0 if report.ready else 2


def _run_pilot_start(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    if not database_path.exists():
        initialize_database(database_path)

    started_at = args.started_at or datetime.now().astimezone()
    with connect_database(database_path) as connection:
        session = start_pilot_session(
            connection,
            PilotSessionStartRequest(
                pilot_id=args.pilot_id,
                operator=args.operator,
                config_version=args.config_version,
                started_at=started_at,
            ),
        )
    print(f"Pilot session started: {session.pilot_id}")
    print(f"Review due at: {session.review_due_at.isoformat()}")
    return 0


def _run_pilot_stop(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    stopped_at = args.stopped_at or datetime.now().astimezone()
    with connect_database(database_path) as connection:
        audit_id = stop_pilot_session(
            connection,
            pilot_id=args.pilot_id,
            stopped_at=stopped_at,
            reason_code=args.reason_code,
            reason=args.reason,
            config_version=args.config_version,
        )
    print(f"Pilot session stopped: {args.pilot_id} audit_id={audit_id}")
    return 0


def _run_pilot_reset_review(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    reviewed_at = args.reviewed_at or datetime.now().astimezone()
    with connect_database(database_path) as connection:
        audit_id = record_pilot_reset_review(
            connection,
            pilot_id=args.pilot_id,
            reviewed_at=reviewed_at,
            review_note=args.review_note,
            config_version=args.config_version,
        )
    print(f"Pilot reset review recorded: {args.pilot_id} audit_id={audit_id}")
    return 0


def _run_pilot_resume(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    resumed_at = args.resumed_at or datetime.now().astimezone()
    with connect_database(database_path) as connection:
        audit_id = resume_pilot_session(
            connection,
            pilot_id=args.pilot_id,
            resumed_at=resumed_at,
            review_note=args.review_note,
            config_version=args.config_version,
        )
    print(f"Pilot session resumed: {args.pilot_id} audit_id={audit_id}")
    return 0


def _run_pilot_live_fill(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    entry = _live_fill_entry_from_args(args)
    with connect_database(database_path) as connection:
        result = record_live_fill_for_active_session(
            connection,
            entry,
            config_version=args.config_version,
            pilot_id=args.pilot_id,
        )
    print(f"Session-gated live fill recorded: fill_id={result.fill_result.fill_id}")
    print(f"Pilot gate: {result.gate_decision.status.value}")
    print(f"Slippage: {result.fill_result.slippage.slippage}")
    print(f"Pilot stopped: {result.stop_audit_id is not None}")
    return 0


def _run_pilot_dashboard(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    generated_at = args.generated_at or datetime.now().astimezone()
    with connect_database(database_path) as connection:
        dashboard = build_gated_live_risk_dashboard_from_database(
            connection,
            args.date,
            generated_at=generated_at,
            config_version=args.config_version,
            pilot_id=args.pilot_id,
        )
    print(dashboard.to_markdown())
    return 0


def _run_pilot_signoff(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    signed_at = args.signed_at or datetime.now().astimezone()
    signoff = DailyPilotSignoff(
        pilot_id=args.pilot_id,
        signoff_date=args.date,
        operator=args.operator,
        signed_at=signed_at,
        report_reviewed=args.report_reviewed,
        positions_reconciled=args.positions_reconciled,
        slippage_reviewed=args.slippage_reviewed,
        violations_reviewed=args.violations_reviewed,
        notes=args.notes,
    )
    with connect_database(database_path) as connection:
        result = record_daily_operator_signoff(connection, signoff, config_version=args.config_version)
    print(f"Pilot daily signoff: {result.status.value}")
    if result.reason_codes:
        print(f"Reason codes: {', '.join(result.reason_codes)}")
    return 0 if result.passed else 2


def _run_pilot_evidence(args: argparse.Namespace) -> int:
    database_path: Path = args.database
    _require_database_exists(database_path)
    generated_at = args.generated_at or datetime.now().astimezone()
    with connect_database(database_path) as connection:
        packet = build_pilot_evidence_packet(connection, pilot_id=args.pilot_id, generated_at=generated_at)
    packet.write_json(args.output_json)
    print(f"Pilot evidence packet written: {args.output_json}")
    return 0


def _run_build_pilot_demo(args: argparse.Namespace) -> int:
    request = PilotDemoPacketRequest(
        database_path=args.database,
        output_dir=args.output_dir,
        run_at=args.run_at or datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
        report_date=args.date or date(2026, 6, 20),
        pilot_id=args.pilot_id,
        operator=args.operator,
        config_version=args.config_version,
    )
    result = build_pilot_demo_packet(request)
    print(f"Pilot demo packet built: {result.output_dir}")
    print(f"Demo database: {result.database_path}")
    print(f"Evidence packet: {result.evidence_packet_path}")
    return 0


def _run_pilot_release_gate(args: argparse.Namespace) -> int:
    generated_at = args.generated_at or datetime.now(UTC)
    report = evaluate_pilot_release_gate(
        ReleaseGateInput(
            database_path=args.database,
            readiness_report_path=args.readiness,
            evidence_packet_path=args.evidence,
            generated_at=generated_at,
            full_test_suite_passed=args.full_test_suite_passed,
            runbook_acknowledged=args.runbook_acknowledged,
            account_equity_present=args.account_equity_present,
            open_positions_verified=args.open_positions_verified,
            max_evidence_age_days=args.max_evidence_age_days,
        )
    )
    report.write_json(args.output_json)
    if args.output_markdown is not None:
        report.write_markdown(args.output_markdown)
    release_gate_audit_id, release_gate_audit_error = _record_release_gate_audit_event(args.database, report)

    print(f"Pilot release gate: {report.status.value}")
    if release_gate_audit_id is not None:
        print(f"Release gate audit_id: {release_gate_audit_id}")
    else:
        print(f"Release gate audit event not recorded: {release_gate_audit_error}")
    if report.blocking_reason_codes:
        print(f"Blocking reasons: {', '.join(report.blocking_reason_codes)}")
    if report.review_reason_codes:
        print(f"Review reasons: {', '.join(report.review_reason_codes)}")
    return 0 if report.ready else 2


def _record_release_gate_audit_event(database_path: Path, report: ReleaseGateReport) -> tuple[int | None, str | None]:
    if not database_path.exists():
        return None, f"database does not exist: {database_path}"
    try:
        with connect_database(database_path) as connection:
            return record_audit_event(connection, report.to_audit_event()), None
    except sqlite3.Error as exc:
        return None, str(exc)


def _live_fill_entry_from_args(args: argparse.Namespace) -> LiveFillEntry:
    return LiveFillEntry(
        ticket_id=args.ticket_id,
        position_id=args.position_id,
        filled_at=args.filled_at,
        quantity=args.quantity,
        price=args.price,
        expected_credit=args.expected_credit,
        source=args.source,
        order_type=args.order_type,
        manual_execution_confirmed=args.manual_execution_confirmed,
        execution_kill_switch_state=args.execution_kill_switch_state,
        critical_system_error=args.critical_system_error,
        risk_rule_violation=args.risk_rule_violation,
    )


def _parse_date(raw_value: str) -> date:
    try:
        return date.fromisoformat(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from exc


def _parse_datetime(raw_value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return parsed


def _parse_decimal(raw_value: str) -> Decimal:
    try:
        return Decimal(raw_value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be a decimal number") from exc


def _require_database_exists(database_path: Path) -> None:
    if not database_path.exists():
        raise SystemExit(f"database does not exist: {database_path}")


if __name__ == "__main__":
    raise SystemExit(main())
