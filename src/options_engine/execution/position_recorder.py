"""Local position recording from validated manual fills."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from options_engine.storage.models import AuditEvent, Fill, Position, TradeCandidate
from options_engine.strategy.spread_scanner import CandidateScanStatus


class PositionRecordError(ValueError):
    """Raised when a local position cannot be recorded safely."""


class PositionStatus(StrEnum):
    """Local position lifecycle states."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class OpenPositionRecord:
    """Auditable local open-position record produced from a manual fill."""

    position: Position
    source_ticket_id: int
    source_candidate_status: str

    def to_audit_event(self) -> AuditEvent:
        """Convert this open-position record to a structured audit event."""
        return AuditEvent(
            event_type="POSITION_RECORDED",
            entity_type="position",
            message="Local open position recorded from manual fill",
            metadata={
                "source_ticket_id": self.source_ticket_id,
                "source_candidate_status": self.source_candidate_status,
                "symbol": self.position.symbol,
                "opened_at": self.position.opened_at.isoformat(),
                "quantity": self.position.quantity,
                "short_put_strike": str(self.position.short_put_strike),
                "long_put_strike": str(self.position.long_put_strike),
                "expiration_date": self.position.expiration_date.isoformat(),
                "status": self.position.status,
                "config_version": self.position.config_version,
            },
            config_version=self.position.config_version,
            created_at=self.position.created_at,
        )


def record_open_position(
    fill: Fill,
    trade_candidate: TradeCandidate,
    config_version: str,
) -> OpenPositionRecord:
    """Create a local open position from a ticket-backed manual fill."""
    if fill.ticket_id is None:
        raise PositionRecordError("fill must reference a ticket_id to create a new position")
    if fill.position_id is not None:
        raise PositionRecordError("fill already references a position_id")
    if fill.filled_at.tzinfo is None or fill.filled_at.utcoffset() is None:
        raise PositionRecordError("fill.filled_at must be timezone-aware")
    if fill.quantity <= 0:
        raise PositionRecordError("fill quantity must be positive")
    if trade_candidate.status != CandidateScanStatus.ELIGIBLE_FOR_REVIEW.value:
        raise PositionRecordError("positions can only be recorded from eligible trade candidates")
    if trade_candidate.short_put_strike <= trade_candidate.long_put_strike:
        raise PositionRecordError("trade candidate strike order is invalid")
    if trade_candidate.expiration_date <= fill.filled_at.date():
        raise PositionRecordError("trade candidate expiration must be after fill date")
    if not config_version:
        raise PositionRecordError("config_version is required")

    position = Position(
        symbol=trade_candidate.symbol.strip().upper(),
        opened_at=fill.filled_at,
        quantity=fill.quantity,
        short_put_strike=trade_candidate.short_put_strike,
        long_put_strike=trade_candidate.long_put_strike,
        expiration_date=trade_candidate.expiration_date,
        status=PositionStatus.OPEN.value,
        config_version=config_version,
    )
    return OpenPositionRecord(
        position=position,
        source_ticket_id=fill.ticket_id,
        source_candidate_status=trade_candidate.status,
    )
