from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from options_engine.live import (
    EmergencyShutdownFlag,
    LivePilotRuleViolation,
    LivePilotRuleViolationCode,
    PilotDemoPacketRequest,
    PilotDemoPacketResult,
    PilotSessionReasonCode,
    ReleaseGateInput,
    ReleaseGateReasonCode,
    ReleaseGateStatus,
    activate_emergency_shutdown,
    build_pilot_demo_packet,
    evaluate_pilot_release_gate,
    record_rule_violation,
    resume_pilot_session,
    stop_pilot_session,
)
from options_engine.storage.database import connect_database, record_audit_event


def test_release_gate_clean_demo_packet_is_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))
    json_path = tmp_path / "release_gate.json"
    markdown_path = tmp_path / "release_gate.md"
    report.write_json(json_path)
    report.write_markdown(markdown_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert report.status == ReleaseGateStatus.GO
    assert report.ready is True
    assert report.blocking_reason_codes == ()
    assert json_path.exists()
    assert markdown_path.exists()
    assert payload["status"] == "GO"
    assert payload["config_version"] == "demo-config-v1"
    assert payload["broker_orders_submitted_by_system"] is False
    assert _check_status(report, "Position Reconciliation") == "PASSED"
    assert "Pilot Release Gate - GO" in markdown_path.read_text(encoding="utf-8")


def test_release_gate_report_creates_auditable_event(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))
    audit_event = report.to_audit_event()

    assert audit_event.event_type == "PILOT_RELEASE_GATE_GO"
    assert audit_event.entity_type == "pilot_release_gate"
    assert audit_event.config_version == "demo-config-v1"
    assert audit_event.metadata["status"] == "GO"
    assert audit_event.metadata["blocking_reason_codes"] == []
    assert audit_event.metadata["broker_orders_submitted_by_system"] is False


def test_release_gate_missing_readiness_report_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    report = evaluate_pilot_release_gate(
        _gate_input(demo.database_path, tmp_path / "missing_readiness.json", demo.evidence_packet_path)
    )

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.READINESS_REPORT_MISSING.value in report.blocking_reason_codes


def test_release_gate_emergency_shutdown_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        activate_emergency_shutdown(
            connection,
            reason_code=LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR,
            message="critical system error before release",
            config_version="demo-config-v1",
            activated_at=datetime(2026, 6, 20, 15, 55, tzinfo=UTC),
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.EMERGENCY_SHUTDOWN_ACTIVE.value in report.blocking_reason_codes


def test_release_gate_future_shutdown_clear_does_not_hide_active_shutdown(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        activate_emergency_shutdown(
            connection,
            reason_code=LivePilotRuleViolationCode.CRITICAL_SYSTEM_ERROR,
            message="critical system error before release",
            config_version="demo-config-v1",
            activated_at=datetime(2026, 6, 20, 15, 30, tzinfo=UTC),
        )
        future_clear = EmergencyShutdownFlag(
            active=False,
            reason_code=LivePilotRuleViolationCode.EMERGENCY_SHUTDOWN_CLEAR.value,
            message="future clear should not affect release gate",
            flagged_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
        )
        record_audit_event(connection, future_clear.to_audit_event("demo-config-v1"))

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.EMERGENCY_SHUTDOWN_ACTIVE.value in report.blocking_reason_codes


def test_release_gate_rule_violation_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        record_rule_violation(
            connection,
            LivePilotRuleViolation(
                code=LivePilotRuleViolationCode.RISK_RULE_VIOLATION,
                message="portfolio heat cap breached before release",
                field="portfolio_heat",
                occurred_at=datetime(2026, 6, 20, 15, 55, tzinfo=UTC),
                stop_pilot=False,
            ),
            config_version="demo-config-v1",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.RULE_VIOLATIONS_PRESENT.value in report.blocking_reason_codes


def test_release_gate_future_rule_violation_does_not_block_prior_gate(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        record_rule_violation(
            connection,
            LivePilotRuleViolation(
                code=LivePilotRuleViolationCode.RISK_RULE_VIOLATION,
                message="future violation should not affect prior release gate",
                field="portfolio_heat",
                occurred_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
                stop_pilot=False,
            ),
            config_version="demo-config-v1",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.GO
    assert ReleaseGateReasonCode.RULE_VIOLATIONS_PRESENT.value not in report.blocking_reason_codes


def test_release_gate_future_session_resume_does_not_make_stopped_session_active(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        stop_pilot_session(
            connection,
            pilot_id=demo.pilot_id,
            stopped_at=datetime(2026, 6, 20, 15, 30, tzinfo=UTC),
            reason_code=PilotSessionReasonCode.OPERATOR_STOP,
            reason="operator stopped before release gate",
            config_version="demo-config-v1",
        )
        resume_pilot_session(
            connection,
            pilot_id=demo.pilot_id,
            resumed_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
            review_note="future resume should not affect release gate",
            config_version="demo-config-v1",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.PILOT_SESSION_INVALID.value in report.blocking_reason_codes
    assert _check_message(report, ReleaseGateReasonCode.PILOT_SESSION_INVALID.value) == (
        "Expected exactly one active pilot session, found 0"
    )


def test_release_gate_future_session_stop_does_not_block_active_session(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        stop_pilot_session(
            connection,
            pilot_id=demo.pilot_id,
            stopped_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC),
            reason_code=PilotSessionReasonCode.OPERATOR_STOP,
            reason="future stop should not affect release gate",
            config_version="demo-config-v1",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.GO
    assert ReleaseGateReasonCode.PILOT_SESSION_INVALID.value not in report.blocking_reason_codes


def test_release_gate_missing_position_reconciliation_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        connection.execute("DELETE FROM audit_log WHERE event_type LIKE 'POSITION_RECONCILIATION_%'")
        connection.commit()

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value in report.blocking_reason_codes


def test_release_gate_future_config_lock_does_not_satisfy_gate(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        _mutate_latest_config_lock(
            connection,
            locked_at="2026-06-20T17:00:00+00:00",
            created_at="2026-06-20T17:00:00+00:00",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.CONFIG_LOCK_MISSING.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.CONFIG_LOCK_MISSING.value)
        == "No config lock audit event found at or before release gate timestamp"
    )


def test_release_gate_future_position_reconciliation_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        _mutate_latest_position_reconciliation(
            connection,
            checked_at="2026-06-20T17:00:00+00:00",
            created_at="2026-06-20T17:00:00+00:00",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value)
        == "Position reconciliation is after the release gate timestamp"
    )


def test_release_gate_position_reconciliation_after_evidence_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        _mutate_latest_position_reconciliation(
            connection,
            checked_at="2026-06-20T15:30:00+00:00",
            created_at="2026-06-20T15:30:00+00:00",
        )

    report = evaluate_pilot_release_gate(
        _gate_input(
            demo.database_path,
            demo.readiness_report_path,
            demo.evidence_packet_path,
            generated_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )
    )

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value)
        == "Position reconciliation is after evidence packet generated_at"
    )


def test_release_gate_stale_position_reconciliation_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        _mutate_latest_position_reconciliation(
            connection,
            checked_at="2026-06-01T15:05:00+00:00",
            created_at="2026-06-01T15:05:00+00:00",
        )

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.OPEN_POSITIONS_NOT_VERIFIED.value)
        == "Position reconciliation is older than 7 days"
    )


def test_release_gate_account_equity_config_mismatch_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        connection.execute("UPDATE risk_snapshots SET config_version = ?", ("other-config",))
        connection.commit()

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED.value)
        == "Latest risk snapshot config version does not match evidence packet"
    )


def test_release_gate_future_account_equity_snapshot_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    with connect_database(demo.database_path) as connection:
        connection.execute(
            "UPDATE risk_snapshots SET as_of = ?, created_at = ?",
            ("2026-06-20T17:00:00+00:00", "2026-06-20T17:00:00+00:00"),
        )
        connection.commit()

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.ACCOUNT_EQUITY_NOT_VERIFIED.value)
        == "Latest risk snapshot is after the release gate timestamp"
    )


def test_release_gate_missing_runbook_acknowledgement_requires_review(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    report = evaluate_pilot_release_gate(
        _gate_input(
            demo.database_path,
            demo.readiness_report_path,
            demo.evidence_packet_path,
            runbook_acknowledged=False,
        )
    )

    assert report.status == ReleaseGateStatus.REVIEW_REQUIRED
    assert report.blocking_reason_codes == ()
    assert ReleaseGateReasonCode.RUNBOOK_NOT_ACKNOWLEDGED.value in report.review_reason_codes


def test_release_gate_stale_evidence_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)

    report = evaluate_pilot_release_gate(
        _gate_input(
            demo.database_path,
            demo.readiness_report_path,
            demo.evidence_packet_path,
            generated_at=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        )
    )

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.EVIDENCE_PACKET_STALE.value in report.blocking_reason_codes


def test_release_gate_stale_readiness_report_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)
    readiness = json.loads(demo.readiness_report_path.read_text(encoding="utf-8"))
    readiness["run_at"] = "2026-06-01T15:05:00+00:00"
    stale_readiness_path = tmp_path / "stale_readiness.json"
    stale_readiness_path.write_text(json.dumps(readiness), encoding="utf-8")

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, stale_readiness_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.READINESS_REPORT_STALE.value in report.blocking_reason_codes


def test_release_gate_readiness_config_mismatch_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)
    readiness = json.loads(demo.readiness_report_path.read_text(encoding="utf-8"))
    readiness["config_version"] = "other-config"
    mismatched_readiness_path = tmp_path / "mismatched_readiness.json"
    mismatched_readiness_path.write_text(json.dumps(readiness), encoding="utf-8")

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, mismatched_readiness_path, demo.evidence_packet_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.READINESS_CONFIG_MISMATCH.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.READINESS_CONFIG_MISMATCH.value)
        == "Readiness report config version does not match evidence packet"
    )


def test_release_gate_readiness_after_evidence_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)
    readiness = json.loads(demo.readiness_report_path.read_text(encoding="utf-8"))
    readiness["run_at"] = "2026-06-20T15:30:00+00:00"
    future_readiness_path = tmp_path / "future_readiness.json"
    future_readiness_path.write_text(json.dumps(readiness), encoding="utf-8")

    report = evaluate_pilot_release_gate(
        _gate_input(
            demo.database_path,
            future_readiness_path,
            demo.evidence_packet_path,
            generated_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        )
    )

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.READINESS_REPORT_INVALID.value in report.blocking_reason_codes
    assert (
        _check_message(report, ReleaseGateReasonCode.READINESS_REPORT_INVALID.value)
        == "Readiness report run_at is after evidence packet generated_at"
    )


def test_release_gate_invalid_evidence_broker_flag_is_no_go(tmp_path: Path) -> None:
    demo = _build_demo(tmp_path)
    evidence = json.loads(demo.evidence_packet_path.read_text(encoding="utf-8"))
    evidence["broker_orders_submitted_by_system"] = True
    invalid_evidence_path = tmp_path / "invalid_evidence.json"
    invalid_evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    report = evaluate_pilot_release_gate(_gate_input(demo.database_path, demo.readiness_report_path, invalid_evidence_path))

    assert report.status == ReleaseGateStatus.NO_GO
    assert ReleaseGateReasonCode.BROKER_ORDER_SUBMISSION_DETECTED.value in report.blocking_reason_codes


def _build_demo(tmp_path: Path) -> PilotDemoPacketResult:
    return build_pilot_demo_packet(
        PilotDemoPacketRequest(
            database_path=tmp_path / "demo.sqlite",
            output_dir=tmp_path / "reports",
            run_at=datetime(2026, 6, 20, 15, 5, tzinfo=UTC),
        )
    )


def _gate_input(
    database_path: Path,
    readiness_path: Path,
    evidence_path: Path,
    *,
    generated_at: datetime | None = None,
    runbook_acknowledged: bool = True,
) -> ReleaseGateInput:
    return ReleaseGateInput(
        database_path=database_path,
        readiness_report_path=readiness_path,
        evidence_packet_path=evidence_path,
        generated_at=generated_at or datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
        full_test_suite_passed=True,
        runbook_acknowledged=runbook_acknowledged,
        account_equity_present=True,
        open_positions_verified=True,
    )


def _check_status(report: object, check_name: str) -> str | None:
    for check in getattr(report, "checks"):
        if check.name == check_name:
            return check.status.value
    return None


def _check_message(report: object, reason_code: str) -> str | None:
    for check in getattr(report, "checks"):
        if check.reason_code == reason_code:
            return check.message
    return None


def _mutate_latest_position_reconciliation(
    connection: object,
    *,
    checked_at: str,
    created_at: str,
) -> None:
    row = connection.execute(
        """
        SELECT id, payload_json
        FROM audit_log
        WHERE event_type = 'POSITION_RECONCILIATION_VERIFIED'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    payload = json.loads(row[1])
    payload["metadata"]["checked_at"] = checked_at
    connection.execute(
        "UPDATE audit_log SET payload_json = ?, created_at = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), created_at, row[0]),
    )
    connection.commit()


def _mutate_latest_config_lock(
    connection: object,
    *,
    locked_at: str,
    created_at: str,
) -> None:
    row = connection.execute(
        """
        SELECT id, payload_json
        FROM audit_log
        WHERE event_type = 'LIVE_PILOT_CONFIG_LOCKED'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    payload = json.loads(row[1])
    payload["metadata"]["locked_at"] = locked_at
    connection.execute(
        "UPDATE audit_log SET payload_json = ?, created_at = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True), created_at, row[0]),
    )
    connection.commit()
