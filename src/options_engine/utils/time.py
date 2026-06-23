"""Timezone-safe time utilities."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def parse_iso_datetime(raw_value: str) -> datetime:
    """Parse an ISO timestamp and require timezone information."""
    parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    return require_timezone_aware(parsed)


def require_timezone_aware(value: datetime, field_name: str = "timestamp") -> datetime:
    """Return a datetime only if it is timezone-aware."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value
