"""SQLite database schema creation for the MVP storage layer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from options_engine.storage.models import (
    AuditEvent,
    AuditLog,
    Exit,
    Fill,
    OptionChain,
    Position,
    Price,
    RegimeState,
    TradeCandidate,
    TradeTicket,
)

REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "prices",
    "option_chains",
    "regime_states",
    "trade_candidates",
    "trade_tickets",
    "positions",
    "fills",
    "exits",
    "risk_snapshots",
    "audit_log",
    "config_changes",
)

CREATED_AT_SQL: Final[str] = "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"

SCHEMA_STATEMENTS: Final[dict[str, str]] = {
    "prices": f"""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            open_price TEXT,
            high_price TEXT,
            low_price TEXT,
            close_price TEXT NOT NULL,
            volume INTEGER,
            source TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "option_chains": f"""
        CREATE TABLE IF NOT EXISTS option_chains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            quote_timestamp TEXT NOT NULL,
            chain_json TEXT NOT NULL,
            source TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "regime_states": f"""
        CREATE TABLE IF NOT EXISTS regime_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            as_of TEXT NOT NULL,
            regime TEXT NOT NULL,
            details_json TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "trade_candidates": f"""
        CREATE TABLE IF NOT EXISTS trade_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            short_put_strike TEXT NOT NULL,
            long_put_strike TEXT NOT NULL,
            max_loss TEXT NOT NULL,
            status TEXT NOT NULL,
            reason_json TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "trade_tickets": f"""
        CREATE TABLE IF NOT EXISTS trade_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            symbol TEXT NOT NULL,
            order_type TEXT NOT NULL,
            limit_price TEXT NOT NULL,
            status TEXT NOT NULL,
            notes TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL},
            FOREIGN KEY(candidate_id) REFERENCES trade_candidates(id)
        )
    """,
    "positions": f"""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            quantity INTEGER NOT NULL,
            short_put_strike TEXT NOT NULL,
            long_put_strike TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            status TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "fills": f"""
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            position_id INTEGER,
            filled_at TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price TEXT NOT NULL,
            source TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL},
            FOREIGN KEY(ticket_id) REFERENCES trade_tickets(id),
            FOREIGN KEY(position_id) REFERENCES positions(id)
        )
    """,
    "exits": f"""
        CREATE TABLE IF NOT EXISTS exits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            evaluated_at TEXT NOT NULL,
            action TEXT NOT NULL,
            reason_json TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL},
            FOREIGN KEY(position_id) REFERENCES positions(id)
        )
    """,
    "risk_snapshots": f"""
        CREATE TABLE IF NOT EXISTS risk_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of TEXT NOT NULL,
            account_equity TEXT NOT NULL,
            portfolio_heat TEXT NOT NULL,
            details_json TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "audit_log": f"""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            config_version TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
    "config_changes": f"""
        CREATE TABLE IF NOT EXISTS config_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_version TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            summary TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            created_at {CREATED_AT_SQL}
        )
    """,
}


def connect_database(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    """Create all MVP storage tables if they do not already exist."""
    for table_name in REQUIRED_TABLES:
        connection.execute(SCHEMA_STATEMENTS[table_name])
    _ensure_audit_log_columns(connection)
    connection.commit()


def initialize_database(path: Path) -> None:
    """Initialize a local SQLite database with the MVP schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        create_schema(connection)


def list_table_names(connection: sqlite3.Connection) -> set[str]:
    """Return user table names present in the database."""
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row[0] for row in rows}


def insert_audit_log(connection: sqlite3.Connection, event: AuditLog) -> int:
    """Insert one audit event and return its database id."""
    _validate_audit_text(event.event_type, "event_type")
    _validate_audit_text(event.entity_type, "entity_type")
    _validate_audit_text(event.message, "message")
    payload = json.loads(event.payload_json)
    if not isinstance(payload, dict):
        raise ValueError("audit payload_json must encode a JSON object")
    cursor = connection.execute(
        """
        INSERT INTO audit_log (
            event_type,
            entity_type,
            message,
            payload_json,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_type,
            event.entity_type,
            event.message,
            event.payload_json,
            event.config_version,
            event.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("audit_log insert did not return an id")
    return inserted_id


def record_audit_event(connection: sqlite3.Connection, event: AuditEvent) -> int:
    """Validate and persist a structured audit event."""
    audit_log = AuditLog(
        event_type=event.event_type,
        entity_type=event.entity_type,
        message=event.message,
        payload_json=_build_audit_payload_json(event.metadata),
        config_version=event.config_version,
        created_at=event.created_at,
    )
    return insert_audit_log(connection, audit_log)


def query_recent_audit_logs(connection: sqlite3.Connection, limit: int = 100) -> list[AuditLog]:
    """Return recent audit events, newest first."""
    if limit < 1:
        raise ValueError("audit query limit must be positive")

    rows = connection.execute(
        """
        SELECT
            id,
            event_type,
            entity_type,
            message,
            payload_json,
            config_version,
            created_at
        FROM audit_log
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        AuditLog(
            id=row[0],
            event_type=row[1],
            entity_type=row[2],
            message=row[3],
            payload_json=row[4],
            config_version=row[5],
            created_at=_parse_storage_datetime(row[6]),
        )
        for row in rows
    ]


def insert_price(connection: sqlite3.Connection, price: Price) -> int:
    """Insert one validated price record and return its database id."""
    cursor = connection.execute(
        """
        INSERT INTO prices (
            symbol,
            observed_at,
            open_price,
            high_price,
            low_price,
            close_price,
            volume,
            source,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            price.symbol,
            price.observed_at.isoformat(),
            None if price.open_price is None else str(price.open_price),
            None if price.high_price is None else str(price.high_price),
            None if price.low_price is None else str(price.low_price),
            str(price.close_price),
            price.volume,
            price.source,
            price.config_version,
            price.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("prices insert did not return an id")
    return inserted_id


def insert_prices(connection: sqlite3.Connection, prices: Sequence[Price]) -> list[int]:
    """Insert validated price records and return their database ids."""
    return [insert_price(connection, price) for price in prices]


def insert_option_chain(connection: sqlite3.Connection, option_chain: OptionChain) -> int:
    """Insert one validated option chain snapshot and return its database id."""
    json.loads(option_chain.chain_json)
    cursor = connection.execute(
        """
        INSERT INTO option_chains (
            symbol,
            expiration_date,
            quote_timestamp,
            chain_json,
            source,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            option_chain.symbol,
            option_chain.expiration_date.isoformat(),
            option_chain.quote_timestamp.isoformat(),
            option_chain.chain_json,
            option_chain.source,
            option_chain.config_version,
            option_chain.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("option_chains insert did not return an id")
    return inserted_id


def insert_option_chains(connection: sqlite3.Connection, option_chains: Sequence[OptionChain]) -> list[int]:
    """Insert validated option chain snapshots and return their database ids."""
    return [insert_option_chain(connection, option_chain) for option_chain in option_chains]


def insert_regime_state(connection: sqlite3.Connection, regime_state: RegimeState) -> int:
    """Insert one regime state and return its database id."""
    json.loads(regime_state.details_json)
    cursor = connection.execute(
        """
        INSERT INTO regime_states (
            symbol,
            as_of,
            regime,
            details_json,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            regime_state.symbol,
            regime_state.as_of.isoformat(),
            regime_state.regime,
            regime_state.details_json,
            regime_state.config_version,
            regime_state.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("regime_states insert did not return an id")
    return inserted_id


def insert_regime_states(connection: sqlite3.Connection, regime_states: Sequence[RegimeState]) -> list[int]:
    """Insert regime states and return their database ids."""
    return [insert_regime_state(connection, regime_state) for regime_state in regime_states]


def insert_trade_candidate(connection: sqlite3.Connection, trade_candidate: TradeCandidate) -> int:
    """Insert one scanned trade candidate and return its database id."""
    json.loads(trade_candidate.reason_json)
    cursor = connection.execute(
        """
        INSERT INTO trade_candidates (
            symbol,
            expiration_date,
            short_put_strike,
            long_put_strike,
            max_loss,
            status,
            reason_json,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_candidate.symbol,
            trade_candidate.expiration_date.isoformat(),
            str(trade_candidate.short_put_strike),
            str(trade_candidate.long_put_strike),
            str(trade_candidate.max_loss),
            trade_candidate.status,
            trade_candidate.reason_json,
            trade_candidate.config_version,
            trade_candidate.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("trade_candidates insert did not return an id")
    return inserted_id


def insert_trade_candidates(
    connection: sqlite3.Connection,
    trade_candidates: Sequence[TradeCandidate],
) -> list[int]:
    """Insert scanned trade candidates and return their database ids."""
    return [insert_trade_candidate(connection, trade_candidate) for trade_candidate in trade_candidates]


def insert_trade_ticket(connection: sqlite3.Connection, trade_ticket: TradeTicket) -> int:
    """Insert one manual trade ticket and return its database id."""
    cursor = connection.execute(
        """
        INSERT INTO trade_tickets (
            candidate_id,
            symbol,
            order_type,
            limit_price,
            status,
            notes,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_ticket.candidate_id,
            trade_ticket.symbol,
            trade_ticket.order_type,
            str(trade_ticket.limit_price),
            trade_ticket.status,
            trade_ticket.notes,
            trade_ticket.config_version,
            trade_ticket.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("trade_tickets insert did not return an id")
    return inserted_id


def insert_trade_tickets(connection: sqlite3.Connection, trade_tickets: Sequence[TradeTicket]) -> list[int]:
    """Insert manual trade tickets and return their database ids."""
    return [insert_trade_ticket(connection, trade_ticket) for trade_ticket in trade_tickets]


def insert_fill(connection: sqlite3.Connection, fill: Fill) -> int:
    """Insert one manual fill record and return its database id."""
    cursor = connection.execute(
        """
        INSERT INTO fills (
            ticket_id,
            position_id,
            filled_at,
            quantity,
            price,
            source,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fill.ticket_id,
            fill.position_id,
            fill.filled_at.isoformat(),
            fill.quantity,
            str(fill.price),
            fill.source,
            fill.config_version,
            fill.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("fills insert did not return an id")
    return inserted_id


def insert_fills(connection: sqlite3.Connection, fills: Sequence[Fill]) -> list[int]:
    """Insert manual fill records and return their database ids."""
    return [insert_fill(connection, fill) for fill in fills]


def insert_position(connection: sqlite3.Connection, position: Position) -> int:
    """Insert one local position record and return its database id."""
    cursor = connection.execute(
        """
        INSERT INTO positions (
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
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position.symbol,
            position.opened_at.isoformat(),
            None if position.closed_at is None else position.closed_at.isoformat(),
            position.quantity,
            str(position.short_put_strike),
            str(position.long_put_strike),
            position.expiration_date.isoformat(),
            position.status,
            position.config_version,
            position.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("positions insert did not return an id")
    return inserted_id


def insert_positions(connection: sqlite3.Connection, positions: Sequence[Position]) -> list[int]:
    """Insert local position records and return their database ids."""
    return [insert_position(connection, position) for position in positions]


def query_open_positions(connection: sqlite3.Connection) -> list[Position]:
    """Return all locally stored open positions ordered by open time."""
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
        WHERE status = 'OPEN'
        ORDER BY opened_at ASC, id ASC
        """
    ).fetchall()
    return [
        Position(
            id=row[0],
            symbol=row[1],
            opened_at=_parse_storage_datetime(row[2]),
            closed_at=None if row[3] is None else _parse_storage_datetime(row[3]),
            quantity=row[4],
            short_put_strike=Decimal(row[5]),
            long_put_strike=Decimal(row[6]),
            expiration_date=_parse_storage_date(row[7]),
            status=row[8],
            config_version=row[9],
            created_at=_parse_storage_datetime(row[10]),
        )
        for row in rows
    ]


def query_fills_for_position(connection: sqlite3.Connection, position_id: int) -> list[Fill]:
    """Return manual fills linked to one local position id."""
    if position_id <= 0:
        raise ValueError("position_id must be positive")
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
        WHERE position_id = ?
        ORDER BY filled_at ASC, id ASC
        """,
        (position_id,),
    ).fetchall()
    return [
        Fill(
            id=row[0],
            ticket_id=row[1],
            position_id=row[2],
            filled_at=_parse_storage_datetime(row[3]),
            quantity=row[4],
            price=Decimal(row[5]),
            source=row[6],
            config_version=row[7],
            created_at=_parse_storage_datetime(row[8]),
        )
        for row in rows
    ]


def insert_exit(connection: sqlite3.Connection, exit_review: Exit) -> int:
    """Insert one exit review record and return its database id."""
    json.loads(exit_review.reason_json)
    cursor = connection.execute(
        """
        INSERT INTO exits (
            position_id,
            evaluated_at,
            action,
            reason_json,
            config_version,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            exit_review.position_id,
            exit_review.evaluated_at.isoformat(),
            exit_review.action,
            exit_review.reason_json,
            exit_review.config_version,
            exit_review.created_at.isoformat(),
        ),
    )
    connection.commit()
    inserted_id = cursor.lastrowid
    if inserted_id is None:
        raise RuntimeError("exits insert did not return an id")
    return inserted_id


def insert_exits(connection: sqlite3.Connection, exit_reviews: Sequence[Exit]) -> list[int]:
    """Insert exit review records and return their database ids."""
    return [insert_exit(connection, exit_review) for exit_review in exit_reviews]


def _ensure_audit_log_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_log)")}
    if "entity_type" not in columns:
        connection.execute("ALTER TABLE audit_log ADD COLUMN entity_type TEXT NOT NULL DEFAULT 'unknown'")


def _build_audit_payload_json(metadata: dict[str, object]) -> str:
    try:
        return json.dumps({"metadata": metadata}, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("audit metadata must be JSON-serializable") from exc


def _validate_audit_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"audit {field_name} is required")


def _parse_storage_datetime(raw_value: str) -> datetime:
    normalized_value = f"{raw_value[:-1]}+00:00" if raw_value.endswith("Z") else raw_value
    return datetime.fromisoformat(normalized_value)


def _parse_storage_date(raw_value: str) -> date:
    return date.fromisoformat(raw_value)
