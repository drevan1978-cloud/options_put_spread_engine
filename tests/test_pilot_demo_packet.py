from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.live import (
    DEMO_PILOT_ID,
    LiveFillEntry,
    LivePilotError,
    PilotDemoPacketRequest,
    build_pilot_demo_packet,
    build_pilot_evidence_packet,
    load_pilot_sessions,
    record_live_fill_for_active_session,
)
from options_engine.storage.database import connect_database


def test_build_pilot_demo_packet_creates_seeded_database_and_artifacts(tmp_path: Path) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "reports"

    result = build_pilot_demo_packet(
        PilotDemoPacketRequest(database_path=database_path, output_dir=output_dir)
    )

    expected_paths = (
        result.readiness_report_path,
        result.daily_report_path,
        result.dashboard_markdown_path,
        result.evidence_packet_path,
        result.operator_checklist_path,
        result.manifest_path,
    )
    assert database_path.exists()
    assert all(path.exists() for path in expected_paths)

    readiness = json.loads(result.readiness_report_path.read_text(encoding="utf-8"))
    daily_report = json.loads(result.daily_report_path.read_text(encoding="utf-8"))
    evidence = json.loads(result.evidence_packet_path.read_text(encoding="utf-8"))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    dashboard_markdown = result.dashboard_markdown_path.read_text(encoding="utf-8")
    checklist_markdown = result.operator_checklist_path.read_text(encoding="utf-8")

    assert readiness["ready"] is True
    assert daily_report["fills_recorded"] == 1
    assert daily_report["account_equity"] == "100000"
    assert evidence["pilot_session"]["pilot_id"] == DEMO_PILOT_ID
    assert evidence["pilot_session"]["trade_count"] == 1
    assert evidence["broker_orders_submitted_by_system"] is False
    assert manifest["broker_orders_submitted_by_system"] is False
    assert "# Live Pilot Risk Dashboard - 2026-06-20" in dashboard_markdown
    assert "Operator Rehearsal Checklist" in checklist_markdown
    assert _json_contains_no_broker_submission(readiness)
    assert _json_contains_no_broker_submission(daily_report)
    assert _json_contains_no_broker_submission(evidence)
    assert _json_contains_no_broker_submission(manifest)


def test_demo_packet_database_replays_session_state_from_audit_log(tmp_path: Path) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "reports"
    result = build_pilot_demo_packet(PilotDemoPacketRequest(database_path=database_path, output_dir=output_dir))

    with connect_database(database_path) as connection:
        sessions = load_pilot_sessions(connection)
        evidence = build_pilot_evidence_packet(
            connection,
            pilot_id=result.pilot_id,
            generated_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
        )
        rows = connection.execute("SELECT order_type, status FROM trade_tickets").fetchall()

    assert len(sessions) == 1
    assert sessions[0].active is True
    assert sessions[0].trade_count == 1
    assert evidence.pilot_session.to_dict() == sessions[0].to_dict()
    assert rows == [("LIMIT", "DRAFT")]


def test_evidence_packet_excludes_future_session_fill_events(tmp_path: Path) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "reports"
    result = build_pilot_demo_packet(PilotDemoPacketRequest(database_path=database_path, output_dir=output_dir))
    generated_at = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)

    with connect_database(database_path) as connection:
        record_live_fill_for_active_session(
            connection,
            LiveFillEntry(
                ticket_id=result.ticket_id,
                position_id=None,
                filled_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
                quantity=1,
                price=Decimal("1.40"),
                expected_credit=Decimal("1.50"),
                source="future_manual_live_entry",
                order_type="LIMIT",
                manual_execution_confirmed=True,
                execution_kill_switch_state="GREEN",
            ),
            config_version="demo-config-v1",
            pilot_id=result.pilot_id,
        )
        current_sessions = load_pilot_sessions(connection)
        evidence = build_pilot_evidence_packet(
            connection,
            pilot_id=result.pilot_id,
            generated_at=generated_at,
        )

    assert current_sessions[0].trade_count == 2
    assert evidence.pilot_session.trade_count == 1
    assert len(evidence.fills) == 1
    assert len(evidence.slippage_events) == 1
    assert all(event["created_at"] <= generated_at.isoformat() for event in evidence.audit_events)
    assert all(fill["filled_at"] <= generated_at.isoformat() for fill in evidence.fills)
    assert all(snapshot["live_fills_today"] == 1 for snapshot in evidence.dashboard_snapshots)


def test_demo_packet_refuses_non_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "reports"
    request = PilotDemoPacketRequest(database_path=database_path, output_dir=output_dir)
    build_pilot_demo_packet(request)

    with pytest.raises(LivePilotError, match="empty"):
        build_pilot_demo_packet(request)


def test_demo_packet_accepts_custom_timestamp_and_report_date(tmp_path: Path) -> None:
    database_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "reports"
    result = build_pilot_demo_packet(
        PilotDemoPacketRequest(
            database_path=database_path,
            output_dir=output_dir,
            run_at=datetime(2026, 6, 21, 14, 30, tzinfo=UTC),
            report_date=date(2026, 6, 21),
            pilot_id="pilot-custom",
            operator="operator",
            config_version="custom-config",
        )
    )
    evidence = json.loads(result.evidence_packet_path.read_text(encoding="utf-8"))

    assert result.pilot_id == "pilot-custom"
    assert evidence["pilot_session"]["pilot_id"] == "pilot-custom"
    assert evidence["pilot_session"]["config_version"] == "custom-config"


def _json_contains_no_broker_submission(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"broker_order_submitted", "broker_orders_submitted_by_system"} and nested is not False:
                return False
            if key in {"market_order_allowed", "auto_execution"} and nested is not False:
                return False
            if not _json_contains_no_broker_submission(nested):
                return False
    elif isinstance(value, list):
        return all(_json_contains_no_broker_submission(item) for item in value)
    return True
