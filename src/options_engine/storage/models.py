"""Typed storage models for the MVP database schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for model defaults."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Price:
    """Stored underlying price observation."""

    symbol: str
    observed_at: datetime
    close_price: Decimal
    source: str
    config_version: str
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    volume: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class OptionChain:
    """Stored option chain snapshot."""

    symbol: str
    expiration_date: date
    quote_timestamp: datetime
    chain_json: str
    source: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class RegimeState:
    """Stored market regime state."""

    symbol: str
    as_of: datetime
    regime: str
    details_json: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class TradeCandidate:
    """Stored put spread candidate for manual review."""

    symbol: str
    expiration_date: date
    short_put_strike: Decimal
    long_put_strike: Decimal
    max_loss: Decimal
    status: str
    reason_json: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class TradeTicket:
    """Stored manual trade ticket."""

    candidate_id: int | None
    symbol: str
    order_type: str
    limit_price: Decimal
    status: str
    notes: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """Stored position record from external/manual records."""

    symbol: str
    opened_at: datetime
    quantity: int
    short_put_strike: Decimal
    long_put_strike: Decimal
    expiration_date: date
    status: str
    config_version: str
    closed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """Stored fill record from external/manual records."""

    ticket_id: int | None
    position_id: int | None
    filled_at: datetime
    quantity: int
    price: Decimal
    source: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Exit:
    """Stored exit review record."""

    position_id: int
    evaluated_at: datetime
    action: str
    reason_json: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """Stored risk state snapshot."""

    as_of: datetime
    account_equity: Decimal
    portfolio_heat: Decimal
    details_json: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class AuditLog:
    """Stored audit event."""

    event_type: str
    entity_type: str
    message: str
    payload_json: str
    config_version: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Structured audit event before durable storage."""

    event_type: str
    entity_type: str
    message: str
    config_version: str
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """Stored configuration change event."""

    config_version: str
    changed_at: datetime
    changed_by: str
    summary: str
    before_json: str
    after_json: str
    created_at: datetime = field(default_factory=utc_now)
    id: int | None = None


StorageModel = (
    Price
    | OptionChain
    | RegimeState
    | TradeCandidate
    | TradeTicket
    | Position
    | Fill
    | Exit
    | RiskSnapshot
    | AuditLog
    | ConfigChange
)


def model_table_names() -> dict[type[Any], str]:
    """Return the table name for each typed storage model."""
    return {
        Price: "prices",
        OptionChain: "option_chains",
        RegimeState: "regime_states",
        TradeCandidate: "trade_candidates",
        TradeTicket: "trade_tickets",
        Position: "positions",
        Fill: "fills",
        Exit: "exits",
        RiskSnapshot: "risk_snapshots",
        AuditLog: "audit_log",
        ConfigChange: "config_changes",
    }
