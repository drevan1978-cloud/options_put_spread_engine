from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from options_engine.live import (
    DRY_RUN_FILL_SOURCE,
    LivePilotDryRunRequest,
    ReadinessStatus,
    run_live_pilot_readiness_dry_run,
)
from options_engine.storage.database import connect_database, initialize_database


def test_live_pilot_readiness_dry_run_proves_manual_workflow(tmp_path: Path) -> None:
    database_path = tmp_path / "readiness.sqlite"
    initialize_database(database_path)
    request = LivePilotDryRunRequest(
        report_date=date(2026, 6, 20),
        run_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
        config_version="config-v1",
        operator="operator",
    )

    with connect_database(database_path) as connection:
        report = run_live_pilot_readiness_dry_run(connection, request)
        fill_row = connection.execute(
            "SELECT quantity, price, source FROM fills WHERE id = ?",
            (report.created_fill_id,),
        ).fetchone()
        audit_rows = connection.execute(
            "SELECT event_type, payload_json FROM audit_log ORDER BY id"
        ).fetchall()

    assert report.status == ReadinessStatus.READY
    assert report.ready is True
    assert report.broker_orders_submitted is False
    assert fill_row == (1, "1.45", DRY_RUN_FILL_SOURCE)
    assert report.dashboard_before_violation.emergency_shutdown_active is False
    assert report.dashboard_before_violation.live_fills_today == 1
    assert report.dashboard_before_violation.daily_report.fills_recorded == 1
    assert report.dashboard_after_violation.emergency_shutdown_active is True
    assert report.dashboard_after_violation.rule_violations_today == 1
    assert report.continuation_decision_after_violation.status.value == "STOPPED"
    assert all(check.passed for check in report.checks)

    live_fill_payload = next(
        json.loads(payload)["metadata"]
        for event_type, payload in audit_rows
        if event_type == "LIVE_FILL_RECORDED"
    )
    assert live_fill_payload["dry_run"] is True
    assert live_fill_payload["broker_order_submitted_by_system"] is False
    assert live_fill_payload["market_order_allowed"] is False


def test_live_pilot_readiness_report_writes_json(tmp_path: Path) -> None:
    database_path = tmp_path / "readiness.sqlite"
    output_path = tmp_path / "readiness.json"
    initialize_database(database_path)
    request = LivePilotDryRunRequest(
        report_date=date(2026, 6, 20),
        run_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
        config_version="config-v1",
        operator="operator",
    )

    with connect_database(database_path) as connection:
        report = run_live_pilot_readiness_dry_run(connection, request)
        report.write_json(output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] == "READY"
    assert payload["ready"] is True
    assert payload["broker_orders_submitted"] is False
    assert payload["dashboard_after_violation"]["emergency_shutdown_active"] is True


def test_live_pilot_runbook_exists() -> None:
    runbook_path = Path(__file__).resolve().parents[1] / "docs" / "live_pilot_runbook.md"

    content = runbook_path.read_text(encoding="utf-8")

    assert "Manual Live Pilot Runbook" in content
    assert "No market orders" in content or "NO_MARKET_ORDERS" in content
    assert "readiness-dry-run" in content
