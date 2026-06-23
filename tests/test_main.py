from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.live import LivePilotError
from options_engine.main import main
from options_engine.storage.database import connect_database, initialize_database, insert_trade_candidate, insert_trade_ticket
from options_engine.storage.models import TradeCandidate, TradeTicket


def test_daily_report_cli_prints_markdown_from_database(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        insert_trade_candidate(
            connection,
            TradeCandidate(
                symbol="SPY",
                expiration_date=date(2026, 7, 24),
                short_put_strike=Decimal("540"),
                long_put_strike=Decimal("535"),
                max_loss=Decimal("3.40"),
                status="REJECTED",
                reason_json=json.dumps(
                    {
                        "rejection_reasons": [
                            {"code": "BID_ASK_WIDTH_TOO_WIDE", "message": "wide", "field": "bid_ask_width"}
                        ]
                    },
                    sort_keys=True,
                ),
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
            ),
        )

    exit_code = main(["daily-report", "--database", str(database_path), "--date", "2026-06-20"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "# Daily Report - 2026-06-20" in captured.out
    assert "- Candidates scanned: 1" in captured.out
    assert "- BID_ASK_WIDTH_TOO_WIDE: 1" in captured.out


def test_live_fill_cli_records_manual_fill(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    start_exit = main(
        [
            "pilot-start",
            "--database",
            str(database_path),
            "--pilot-id",
            "pilot-001",
            "--config-version",
            "test-config",
            "--operator",
            "operator",
            "--started-at",
            "2026-06-20T13:00:00+00:00",
        ]
    )

    with connect_database(database_path) as connection:
        ticket_id = insert_trade_ticket(
            connection,
            TradeTicket(
                candidate_id=None,
                symbol="SPY",
                order_type="LIMIT",
                limit_price=Decimal("1.50"),
                status="DRAFT",
                notes="MANUAL_EXECUTION_REQUIRED; NO_MARKET_ORDERS",
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 12, 30, tzinfo=UTC),
            ),
        )

    exit_code = main(
        [
            "live-fill",
            "--database",
            str(database_path),
            "--config-version",
            "test-config",
            "--pilot-id",
            "pilot-001",
            "--ticket-id",
            str(ticket_id),
            "--filled-at",
            "2026-06-20T15:05:00+00:00",
            "--quantity",
            "1",
            "--price",
            "1.45",
            "--expected-credit",
            "1.50",
            "--manual-execution-confirmed",
        ]
    )
    captured = capsys.readouterr()

    with connect_database(database_path) as connection:
        row = connection.execute("SELECT ticket_id, quantity, price, source FROM fills").fetchone()
        session_fill_count = connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type = 'LIVE_PILOT_SESSION_FILL_RECORDED'"
        ).fetchone()[0]

    assert start_exit == 0
    assert exit_code == 0
    assert row == (ticket_id, 1, "1.45", "manual_live_entry")
    assert session_fill_count == 1
    assert "Session-gated live fill recorded" in captured.out
    assert "Slippage: 0.05" in captured.out


def test_live_fill_cli_blocks_without_active_pilot_session(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        ticket_id = insert_trade_ticket(
            connection,
            TradeTicket(
                candidate_id=None,
                symbol="SPY",
                order_type="LIMIT",
                limit_price=Decimal("1.50"),
                status="DRAFT",
                notes="MANUAL_EXECUTION_REQUIRED; NO_MARKET_ORDERS",
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 12, 30, tzinfo=UTC),
            ),
        )

    with pytest.raises(LivePilotError, match="pilot session gate blocked live fill"):
        main(
            [
                "live-fill",
                "--database",
                str(database_path),
                "--config-version",
                "test-config",
                "--pilot-id",
                "pilot-001",
                "--ticket-id",
                str(ticket_id),
                "--filled-at",
                "2026-06-20T15:05:00+00:00",
                "--quantity",
                "1",
                "--price",
                "1.45",
                "--expected-credit",
                "1.50",
                "--manual-execution-confirmed",
            ]
        )

    with connect_database(database_path) as connection:
        fill_count = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        gate_count = connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type = 'LIVE_PILOT_SESSION_GATE_BLOCKED'"
        ).fetchone()[0]

    assert fill_count == 0
    assert gate_count == 1


def test_risk_dashboard_cli_prints_live_dashboard(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    exit_code = main(["risk-dashboard", "--database", str(database_path), "--date", "2026-06-20"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "# Live Pilot Risk Dashboard - 2026-06-20" in captured.out
    assert "- Emergency shutdown: CLEAR" in captured.out


def test_readiness_dry_run_cli_prints_ready_report_and_writes_json(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "readiness.sqlite"
    output_path = tmp_path / "readiness.json"

    exit_code = main(
        [
            "readiness-dry-run",
            "--database",
            str(database_path),
            "--config-version",
            "test-config",
            "--date",
            "2026-06-20",
            "--operator",
            "operator",
            "--run-at",
            "2026-06-20T15:05:00+00:00",
            "--output-json",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "# Live Pilot Readiness Dry Run - 2026-06-20" in captured.out
    assert "- Status: READY" in captured.out
    assert payload["ready"] is True
    assert payload["broker_orders_submitted"] is False


def test_pilot_session_cli_flow_records_fill_signoff_and_evidence(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "pilot.sqlite"
    evidence_path = tmp_path / "pilot-evidence.json"

    start_exit = main(
        [
            "pilot-start",
            "--database",
            str(database_path),
            "--pilot-id",
            "pilot-001",
            "--config-version",
            "test-config",
            "--operator",
            "operator",
            "--started-at",
            "2026-06-20T13:00:00+00:00",
        ]
    )
    with connect_database(database_path) as connection:
        ticket_id = insert_trade_ticket(
            connection,
            TradeTicket(
                candidate_id=None,
                symbol="SPY",
                order_type="LIMIT",
                limit_price=Decimal("1.50"),
                status="DRAFT",
                notes="MANUAL_EXECUTION_REQUIRED; NO_MARKET_ORDERS",
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 13, 30, tzinfo=UTC),
            ),
        )

    fill_exit = main(
        [
            "pilot-live-fill",
            "--database",
            str(database_path),
            "--pilot-id",
            "pilot-001",
            "--config-version",
            "test-config",
            "--ticket-id",
            str(ticket_id),
            "--filled-at",
            "2026-06-20T15:05:00+00:00",
            "--quantity",
            "1",
            "--price",
            "1.45",
            "--expected-credit",
            "1.50",
            "--manual-execution-confirmed",
        ]
    )
    signoff_exit = main(
        [
            "pilot-signoff",
            "--database",
            str(database_path),
            "--pilot-id",
            "pilot-001",
            "--config-version",
            "test-config",
            "--date",
            "2026-06-20",
            "--operator",
            "operator",
            "--notes",
            "daily closeout complete",
            "--signed-at",
            "2026-06-20T20:00:00+00:00",
            "--report-reviewed",
            "--positions-reconciled",
            "--slippage-reviewed",
            "--violations-reviewed",
        ]
    )
    evidence_exit = main(
        [
            "pilot-evidence",
            "--database",
            str(database_path),
            "--pilot-id",
            "pilot-001",
            "--output-json",
            str(evidence_path),
            "--generated-at",
            "2026-06-20T21:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert start_exit == 0
    assert fill_exit == 0
    assert signoff_exit == 0
    assert evidence_exit == 0
    assert "Pilot session started: pilot-001" in captured.out
    assert "Session-gated live fill recorded" in captured.out
    assert evidence["pilot_session"]["trade_count"] == 1
    assert evidence["broker_orders_submitted_by_system"] is False


def test_build_pilot_demo_cli_creates_demo_packet(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "demo-reports"

    exit_code = main(
        [
            "build-pilot-demo",
            "--database",
            str(database_path),
            "--output-dir",
            str(output_dir),
            "--run-at",
            "2026-06-20T15:05:00+00:00",
            "--date",
            "2026-06-20",
        ]
    )
    captured = capsys.readouterr()

    evidence_path = output_dir / "evidence_packet.json"
    readiness_path = output_dir / "readiness_report.json"
    dashboard_path = output_dir / "pilot_dashboard.md"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert database_path.exists()
    assert evidence_path.exists()
    assert readiness_path.exists()
    assert dashboard_path.exists()
    assert "Pilot demo packet built" in captured.out
    assert evidence["pilot_session"]["trade_count"] == 1
    assert evidence["broker_orders_submitted_by_system"] is False


def test_pilot_release_gate_cli_writes_go_report(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "demo-reports"
    release_json = output_dir / "release_gate.json"
    release_markdown = output_dir / "release_gate.md"

    build_exit = main(
        [
            "build-pilot-demo",
            "--database",
            str(database_path),
            "--output-dir",
            str(output_dir),
            "--run-at",
            "2026-06-20T15:05:00+00:00",
            "--date",
            "2026-06-20",
        ]
    )
    release_exit = main(
        [
            "pilot-release-gate",
            "--database",
            str(database_path),
            "--readiness",
            str(output_dir / "readiness_report.json"),
            "--evidence",
            str(output_dir / "evidence_packet.json"),
            "--output-json",
            str(release_json),
            "--output-markdown",
            str(release_markdown),
            "--generated-at",
            "2026-06-20T16:00:00+00:00",
            "--full-test-suite-passed",
            "--runbook-acknowledged",
            "--account-equity-present",
            "--open-positions-verified",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(release_json.read_text(encoding="utf-8"))
    with connect_database(database_path) as connection:
        audit_row = connection.execute(
            """
            SELECT event_type, entity_type, payload_json, config_version
            FROM audit_log
            WHERE entity_type = 'pilot_release_gate'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    audit_payload = json.loads(audit_row[2])["metadata"]

    assert build_exit == 0
    assert release_exit == 0
    assert payload["status"] == "GO"
    assert payload["ready"] is True
    assert release_markdown.exists()
    assert "Pilot release gate: GO" in captured.out
    assert "Release gate audit_id:" in captured.out
    assert audit_row[0] == "PILOT_RELEASE_GATE_GO"
    assert audit_row[1] == "pilot_release_gate"
    assert audit_row[3] == "demo-config-v1"
    assert audit_payload["status"] == "GO"
    assert audit_payload["blocking_reason_codes"] == []
    assert audit_payload["broker_orders_submitted_by_system"] is False


def test_pilot_release_gate_cli_persists_no_go_audit_event(tmp_path: Path, capsys: object) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "demo-reports"
    release_json = output_dir / "release_gate_no_go.json"

    build_exit = main(
        [
            "build-pilot-demo",
            "--database",
            str(database_path),
            "--output-dir",
            str(output_dir),
            "--run-at",
            "2026-06-20T15:05:00+00:00",
            "--date",
            "2026-06-20",
        ]
    )
    release_exit = main(
        [
            "pilot-release-gate",
            "--database",
            str(database_path),
            "--readiness",
            str(output_dir / "missing_readiness_report.json"),
            "--evidence",
            str(output_dir / "evidence_packet.json"),
            "--output-json",
            str(release_json),
            "--generated-at",
            "2026-06-20T16:00:00+00:00",
            "--full-test-suite-passed",
            "--runbook-acknowledged",
            "--account-equity-present",
            "--open-positions-verified",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(release_json.read_text(encoding="utf-8"))
    with connect_database(database_path) as connection:
        audit_row = connection.execute(
            """
            SELECT event_type, entity_type, payload_json, config_version
            FROM audit_log
            WHERE entity_type = 'pilot_release_gate'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    audit_payload = json.loads(audit_row[2])["metadata"]

    assert build_exit == 0
    assert release_exit == 2
    assert payload["status"] == "NO_GO"
    assert "READINESS_REPORT_MISSING" in payload["blocking_reason_codes"]
    assert "Pilot release gate: NO_GO" in captured.out
    assert audit_row[0] == "PILOT_RELEASE_GATE_NO_GO"
    assert audit_row[1] == "pilot_release_gate"
    assert audit_row[3] == "demo-config-v1"
    assert audit_payload["status"] == "NO_GO"
    assert "READINESS_REPORT_MISSING" in audit_payload["blocking_reason_codes"]
    assert audit_payload["broker_orders_submitted_by_system"] is False
