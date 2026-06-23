"""Manual fill tracking from local records only."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from options_engine.storage.models import AuditEvent, Fill

REQUIRED_FILL_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ticket_id", "position_id", "filled_at", "quantity", "price", "source"}
)


class FillTrackingError(ValueError):
    """Raised when a manual fill record is missing, malformed, or unauditable."""


@dataclass(frozen=True, slots=True)
class ManualFillRecord:
    """Validated local/manual fill record."""

    ticket_id: int | None
    position_id: int | None
    filled_at: datetime
    quantity: int
    price: Decimal
    source: str

    def __post_init__(self) -> None:
        if self.ticket_id is None and self.position_id is None:
            raise FillTrackingError("fill must reference a ticket_id or position_id")
        if self.ticket_id is not None and self.ticket_id <= 0:
            raise FillTrackingError("ticket_id must be positive when provided")
        if self.position_id is not None and self.position_id <= 0:
            raise FillTrackingError("position_id must be positive when provided")
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise FillTrackingError("filled_at must be timezone-aware")
        if self.quantity <= 0:
            raise FillTrackingError("quantity must be positive")
        if self.price <= Decimal("0"):
            raise FillTrackingError("price must be positive")

        normalized_source = self.source.strip()
        if not normalized_source:
            raise FillTrackingError("source is required")
        object.__setattr__(self, "source", normalized_source)

    def to_storage_model(self, config_version: str) -> Fill:
        """Convert this manual record to the persistent fill model."""
        if not config_version:
            raise FillTrackingError("config_version is required")
        return Fill(
            ticket_id=self.ticket_id,
            position_id=self.position_id,
            filled_at=self.filled_at,
            quantity=self.quantity,
            price=self.price,
            source=self.source,
            config_version=config_version,
        )

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this manual fill record to a structured audit event."""
        return fill_to_audit_event(self.to_storage_model(config_version))


def track_fills(records: list[ManualFillRecord], config_version: str) -> list[Fill]:
    """Convert validated manual fill records to storage models."""
    return [record.to_storage_model(config_version) for record in records]


def fill_to_audit_event(fill: Fill) -> AuditEvent:
    """Convert one stored fill model to a structured audit event."""
    return AuditEvent(
        event_type="MANUAL_FILL_RECORDED",
        entity_type="fill",
        message="Manual/local fill record captured; no broker synchronization performed",
        metadata={
            "ticket_id": fill.ticket_id,
            "position_id": fill.position_id,
            "filled_at": fill.filled_at.isoformat(),
            "quantity": fill.quantity,
            "price": str(fill.price),
            "source": fill.source,
            "config_version": fill.config_version,
        },
        config_version=fill.config_version,
        created_at=fill.created_at,
    )


def audit_events_for_fills(fills: list[Fill]) -> list[AuditEvent]:
    """Convert stored fill models to structured audit events."""
    return [fill_to_audit_event(fill) for fill in fills]


def load_manual_fills_csv(csv_path: Path) -> list[ManualFillRecord]:
    """Load validated manual fill records from a local CSV file."""
    if not csv_path.exists():
        raise FillTrackingError(f"CSV file does not exist: {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        _validate_columns(reader.fieldnames, csv_path)
        records = [_parse_fill_record(_normalize_row(row), row_number=index + 2) for index, row in enumerate(reader)]

    if not records:
        raise FillTrackingError(f"CSV file contains no fill rows: {csv_path}")
    return records


def _validate_columns(fieldnames: list[str] | None, csv_path: Path) -> None:
    if fieldnames is None:
        raise FillTrackingError(f"CSV file is empty: {csv_path}")

    normalized = {field.strip() for field in fieldnames}
    missing = REQUIRED_FILL_COLUMNS.difference(normalized)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise FillTrackingError(f"CSV missing required columns: {missing_columns}")


def _parse_fill_record(row: dict[str, str], row_number: int) -> ManualFillRecord:
    return ManualFillRecord(
        ticket_id=_parse_optional_int(row, "ticket_id", row_number),
        position_id=_parse_optional_int(row, "position_id", row_number),
        filled_at=_parse_timestamp(_required_text(row, "filled_at", row_number), row_number),
        quantity=_parse_required_int(row, "quantity", row_number),
        price=_parse_decimal(row, "price", row_number),
        source=_required_text(row, "source", row_number),
    )


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {column.strip(): value for column, value in row.items() if column is not None}


def _required_text(row: dict[str, str], column: str, row_number: int) -> str:
    value = row.get(column)
    if value is None or value.strip() == "":
        raise FillTrackingError(f"row {row_number}: {column} is required")
    return value.strip()


def _parse_optional_int(row: dict[str, str], column: str, row_number: int) -> int | None:
    value = row.get(column)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise FillTrackingError(f"row {row_number}: {column} is malformed") from exc


def _parse_required_int(row: dict[str, str], column: str, row_number: int) -> int:
    value = _required_text(row, column, row_number)
    try:
        return int(value)
    except ValueError as exc:
        raise FillTrackingError(f"row {row_number}: {column} is malformed") from exc


def _parse_decimal(row: dict[str, str], column: str, row_number: int) -> Decimal:
    value = _required_text(row, column, row_number)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise FillTrackingError(f"row {row_number}: {column} is malformed") from exc


def _parse_timestamp(value: str, row_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FillTrackingError(f"row {row_number}: filled_at is malformed") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FillTrackingError(f"row {row_number}: filled_at must be timezone-aware")
    return parsed
