from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from options_engine.storage.database import (
    connect_database,
    initialize_database,
    query_recent_audit_logs,
    record_audit_event,
)
from options_engine.storage.models import AuditEvent


def test_record_audit_event_persists_structured_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        event_id = record_audit_event(
            connection,
            AuditEvent(
                event_type="DECISION_REJECTED",
                entity_type="trade_candidate",
                message="Candidate rejected by hard rule",
                metadata={"candidate_id": 42, "reason_codes": ["MAX_PORTFOLIO_HEAT_EXCEEDED"]},
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
            ),
        )
        row = connection.execute(
            """
            SELECT event_type, entity_type, message, payload_json, config_version
            FROM audit_log
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == "DECISION_REJECTED"
    assert row[1] == "trade_candidate"
    assert row[2] == "Candidate rejected by hard rule"
    assert row[4] == "test-config"
    assert json.loads(row[3]) == {
        "metadata": {"candidate_id": 42, "reason_codes": ["MAX_PORTFOLIO_HEAT_EXCEEDED"]}
    }


def test_query_recent_audit_logs_returns_newest_first(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        older_id = record_audit_event(
            connection,
            AuditEvent(
                event_type="SCAN_STARTED",
                entity_type="spread_scan",
                message="Started local scan",
                metadata={},
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 13, 0, tzinfo=UTC),
            ),
        )
        newer_id = record_audit_event(
            connection,
            AuditEvent(
                event_type="SCAN_COMPLETED",
                entity_type="spread_scan",
                message="Completed local scan",
                metadata={"candidates_scanned": 12},
                config_version="test-config",
                created_at=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
            ),
        )

        recent_events = query_recent_audit_logs(connection, limit=2)

    assert [event.id for event in recent_events] == [newer_id, older_id]
    assert recent_events[0].event_type == "SCAN_COMPLETED"
    assert recent_events[0].entity_type == "spread_scan"
    assert json.loads(recent_events[0].payload_json) == {"metadata": {"candidates_scanned": 12}}


def test_record_audit_event_rejects_non_json_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        with pytest.raises(ValueError, match="JSON-serializable"):
            record_audit_event(
                connection,
                AuditEvent(
                    event_type="BAD_EVENT",
                    entity_type="audit_test",
                    message="Bad metadata",
                    metadata={"bad": object()},
                    config_version="test-config",
                ),
            )

        count = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    assert count == 0


def test_record_audit_event_requires_entity_type(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with connect_database(database_path) as connection:
        with pytest.raises(ValueError, match="entity_type"):
            record_audit_event(
                connection,
                AuditEvent(
                    event_type="MISSING_ENTITY",
                    entity_type=" ",
                    message="Missing entity type",
                    metadata={},
                    config_version="test-config",
                ),
            )
