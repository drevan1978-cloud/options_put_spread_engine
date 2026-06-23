"""Post-fill position monitoring for manually entered spread fills."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from options_engine.execution.position_recorder import OpenPositionRecord, PositionStatus, record_open_position
from options_engine.regime import RegimeLabel
from options_engine.storage.database import query_fills_for_position, query_open_positions
from options_engine.storage.models import AuditEvent, Fill, Position, TradeCandidate


class PositionMonitorError(ValueError):
    """Raised when position monitoring data is missing or malformed."""


class PositionMonitorReasonCode(StrEnum):
    """Stable reason codes for position monitoring and reconciliation."""

    POSITION_VERIFIED = "POSITION_VERIFIED"
    OPEN_POSITION_MISSING_DATABASE_ID = "OPEN_POSITION_MISSING_DATABASE_ID"
    OPEN_POSITION_FILL_MISSING = "OPEN_POSITION_FILL_MISSING"
    EXPECTED_POSITION_NOT_FOUND = "EXPECTED_POSITION_NOT_FOUND"
    TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"


class PositionReconciliationStatus(StrEnum):
    """Open-position reconciliation result status."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class PositionMarkSnapshot:
    """One auditable mark-to-market snapshot for an open position."""

    position: Position
    fill: Fill
    marked_at: datetime
    mark_price: Decimal
    theoretical_mid: Decimal
    fill_slippage: Decimal
    unrealized_pnl: Decimal
    current_dte: int
    short_delta: Decimal
    regime_state: str
    multiplier: Decimal

    def to_audit_event(self) -> AuditEvent:
        """Convert this mark update to a structured audit event."""
        return AuditEvent(
            event_type="POSITION_MARK_UPDATED",
            entity_type="position",
            message="Open position mark updated from manual/local data",
            metadata={
                "position_id": self.position.id,
                "symbol": self.position.symbol,
                "quantity": self.position.quantity,
                "expiration_date": self.position.expiration_date.isoformat(),
                "fill_price": str(self.fill.price),
                "mark_price": str(self.mark_price),
                "theoretical_mid": str(self.theoretical_mid),
                "fill_slippage": str(self.fill_slippage),
                "unrealized_pnl": str(self.unrealized_pnl),
                "current_dte": self.current_dte,
                "short_delta": str(self.short_delta),
                "regime_state": self.regime_state,
                "multiplier": str(self.multiplier),
                "marked_at": self.marked_at.isoformat(),
                "config_version": self.position.config_version,
            },
            config_version=self.position.config_version,
            created_at=self.marked_at,
        )


@dataclass(frozen=True, slots=True)
class PositionReconciliationResult:
    """Database reconciliation result for open position verification."""

    status: PositionReconciliationStatus
    reason_codes: tuple[str, ...]
    message: str
    checked_at: datetime
    open_positions: tuple[Position, ...]
    missing_position_ids: tuple[int, ...] = ()

    @property
    def open_risk_verified(self) -> bool:
        """Return true when open risk can be verified from local storage."""
        return self.status == PositionReconciliationStatus.VERIFIED

    @property
    def black_state_required(self) -> bool:
        """Return true when the regime layer should later force BLACK."""
        return not self.open_risk_verified

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this reconciliation result to a structured audit event."""
        if not config_version:
            raise PositionMonitorError("config_version is required")
        return AuditEvent(
            event_type=f"POSITION_RECONCILIATION_{self.status.value}",
            entity_type="position_reconciliation",
            message=self.message,
            metadata={
                "status": self.status.value,
                "reason_codes": list(self.reason_codes),
                "open_position_ids": [position.id for position in self.open_positions],
                "missing_position_ids": list(self.missing_position_ids),
                "open_risk_verified": self.open_risk_verified,
                "black_state_required": self.black_state_required,
                "checked_at": self.checked_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.checked_at,
        )


def add_position_from_filled_ticket(
    fill: Fill,
    trade_candidate: TradeCandidate,
    config_version: str,
) -> OpenPositionRecord:
    """Create an open position from a validated manual ticket fill."""
    _validate_fill_price(fill)
    return record_open_position(fill=fill, trade_candidate=trade_candidate, config_version=config_version)


def update_position_mark(
    position: Position,
    fill: Fill,
    *,
    mark_price: Decimal,
    theoretical_mid: Decimal,
    marked_at: datetime,
    short_delta: Decimal,
    current_regime: RegimeLabel | str,
    multiplier: Decimal = Decimal("100"),
) -> PositionMarkSnapshot:
    """Update an open-position mark and calculate PnL, DTE, delta, and regime context."""
    _validate_mark_inputs(position, fill, mark_price, theoretical_mid, marked_at, short_delta, multiplier)
    current_dte = max((position.expiration_date - marked_at.date()).days, 0)
    fill_slippage = fill.price - theoretical_mid
    unrealized_pnl = (fill.price - mark_price) * Decimal(position.quantity) * multiplier
    return PositionMarkSnapshot(
        position=position,
        fill=fill,
        marked_at=marked_at,
        mark_price=mark_price,
        theoretical_mid=theoretical_mid,
        fill_slippage=fill_slippage,
        unrealized_pnl=unrealized_pnl,
        current_dte=current_dte,
        short_delta=short_delta,
        regime_state=_regime_value(current_regime),
        multiplier=multiplier,
    )


def reconcile_open_positions(
    connection: sqlite3.Connection,
    *,
    checked_at: datetime,
    expected_open_position_ids: set[int] | None = None,
    require_position_fills: bool = True,
) -> PositionReconciliationResult:
    """Verify that locally stored open positions can be reconciled."""
    _validate_timezone("checked_at", checked_at)
    open_positions = tuple(query_open_positions(connection))
    open_position_ids = {position.id for position in open_positions if position.id is not None}
    reason_codes: list[str] = []

    expected_ids = expected_open_position_ids or set()
    missing_ids = tuple(sorted(expected_ids.difference(open_position_ids)))
    if missing_ids:
        reason_codes.append(PositionMonitorReasonCode.EXPECTED_POSITION_NOT_FOUND.value)

    if require_position_fills:
        for position in open_positions:
            if position.id is None:
                reason_codes.append(PositionMonitorReasonCode.OPEN_POSITION_MISSING_DATABASE_ID.value)
                continue
            if not query_fills_for_position(connection, position.id):
                reason_codes.append(PositionMonitorReasonCode.OPEN_POSITION_FILL_MISSING.value)

    unique_reason_codes = tuple(dict.fromkeys(reason_codes))
    if unique_reason_codes:
        return PositionReconciliationResult(
            status=PositionReconciliationStatus.UNVERIFIED,
            reason_codes=unique_reason_codes,
            message="Open positions could not be verified from local storage",
            checked_at=checked_at,
            open_positions=open_positions,
            missing_position_ids=missing_ids,
        )

    return PositionReconciliationResult(
        status=PositionReconciliationStatus.VERIFIED,
        reason_codes=(PositionMonitorReasonCode.POSITION_VERIFIED.value,),
        message="Open positions verified from local storage",
        checked_at=checked_at,
        open_positions=open_positions,
    )


def _validate_mark_inputs(
    position: Position,
    fill: Fill,
    mark_price: Decimal,
    theoretical_mid: Decimal,
    marked_at: datetime,
    short_delta: Decimal,
    multiplier: Decimal,
) -> None:
    if position.status != PositionStatus.OPEN.value:
        raise PositionMonitorError("position must be OPEN to update mark")
    if position.quantity <= 0:
        raise PositionMonitorError("position quantity must be positive")
    if position.opened_at.tzinfo is None or position.opened_at.utcoffset() is None:
        raise PositionMonitorError("position.opened_at must be timezone-aware")
    _validate_fill_price(fill)
    _validate_timezone("marked_at", marked_at)
    if fill.position_id is not None and position.id is not None and fill.position_id != position.id:
        raise PositionMonitorError("fill.position_id does not match position.id")
    if mark_price < Decimal("0"):
        raise PositionMonitorError("mark_price must be non-negative")
    if theoretical_mid <= Decimal("0"):
        raise PositionMonitorError("theoretical_mid must be positive")
    if short_delta < Decimal("-1") or short_delta > Decimal("1"):
        raise PositionMonitorError("short_delta must be between -1 and 1")
    if multiplier <= Decimal("0"):
        raise PositionMonitorError("multiplier must be positive")


def _validate_fill_price(fill: Fill) -> None:
    if fill.price <= Decimal("0"):
        raise PositionMonitorError("manual fill must include a positive fill price")
    _validate_timezone("fill.filled_at", fill.filled_at)


def _validate_timezone(field_name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PositionMonitorError(f"{field_name} must be timezone-aware")


def _regime_value(regime: RegimeLabel | str) -> str:
    value = regime.value if isinstance(regime, RegimeLabel) else regime.strip().upper()
    if value not in {label.value for label in RegimeLabel}:
        raise PositionMonitorError("current_regime must be a known regime state")
    return value
