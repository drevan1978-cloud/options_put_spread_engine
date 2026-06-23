"""Market data interfaces and CSV ingestion for local OHLCV data."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Protocol

REQUIRED_PRICE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"symbol", "timestamp", "open", "high", "low", "close", "volume"}
)


class MarketDataError(ValueError):
    """Raised when local market data is missing, malformed, or unsafe to ingest."""


@dataclass(frozen=True, slots=True)
class PriceBar:
    """Validated OHLCV price bar."""

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise MarketDataError("symbol is required")

        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise MarketDataError("timestamp must be timezone-aware")

        if self.open <= Decimal("0"):
            raise MarketDataError("open must be positive")
        if self.high <= Decimal("0"):
            raise MarketDataError("high must be positive")
        if self.low <= Decimal("0"):
            raise MarketDataError("low must be positive")
        if self.close <= Decimal("0"):
            raise MarketDataError("close must be positive")
        if self.volume < 0:
            raise MarketDataError("volume must be non-negative")

        if self.high < self.low:
            raise MarketDataError("high must be greater than or equal to low")
        if self.open > self.high or self.open < self.low:
            raise MarketDataError("open must be within high/low range")
        if self.close > self.high or self.close < self.low:
            raise MarketDataError("close must be within high/low range")

        object.__setattr__(self, "symbol", normalized_symbol)


class MarketDataProvider(Protocol):
    """Interface for market data providers."""

    def load_price_bars(self, symbol: str) -> list[PriceBar]:
        """Load OHLCV bars for a symbol."""


class CSVMarketDataProvider:
    """Market data provider backed by a local CSV file."""

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = csv_path

    def load_price_bars(self, symbol: str) -> list[PriceBar]:
        """Load and filter OHLCV bars from the configured CSV file."""
        requested_symbol = symbol.strip().upper()
        if not requested_symbol:
            raise MarketDataError("symbol is required")

        bars = load_ohlcv_csv(self.csv_path)
        return [bar for bar in bars if bar.symbol == requested_symbol]


def load_ohlcv_csv(csv_path: Path) -> list[PriceBar]:
    """Load validated OHLCV bars from a CSV file."""
    if not csv_path.exists():
        raise MarketDataError(f"CSV file does not exist: {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        _validate_columns(reader.fieldnames, csv_path)
        bars = [_parse_price_bar(_normalize_row(row), row_number=index + 2) for index, row in enumerate(reader)]

    if not bars:
        raise MarketDataError(f"CSV file contains no price rows: {csv_path}")
    return bars


def _validate_columns(fieldnames: list[str] | None, csv_path: Path) -> None:
    if fieldnames is None:
        raise MarketDataError(f"CSV file is empty: {csv_path}")

    normalized = {field.strip() for field in fieldnames}
    missing = REQUIRED_PRICE_COLUMNS.difference(normalized)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise MarketDataError(f"CSV missing required columns: {missing_columns}")


def _parse_price_bar(row: dict[str, str], row_number: int) -> PriceBar:
    try:
        return PriceBar(
            symbol=_required_text(row, "symbol", row_number),
            timestamp=_parse_timestamp(_required_text(row, "timestamp", row_number), row_number),
            open=_parse_decimal(row, "open", row_number),
            high=_parse_decimal(row, "high", row_number),
            low=_parse_decimal(row, "low", row_number),
            close=_parse_decimal(row, "close", row_number),
            volume=_parse_int(row, "volume", row_number),
        )
    except MarketDataError:
        raise
    except Exception as exc:
        raise MarketDataError(f"row {row_number}: malformed OHLCV data") from exc


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {column.strip(): value for column, value in row.items()}


def _required_text(row: dict[str, str], column: str, row_number: int) -> str:
    value = row.get(column)
    if value is None or value.strip() == "":
        raise MarketDataError(f"row {row_number}: {column} is required")
    return value.strip()


def _parse_timestamp(value: str, row_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketDataError(f"row {row_number}: timestamp is malformed") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataError(f"row {row_number}: timestamp must be timezone-aware")
    return parsed


def _parse_decimal(row: dict[str, str], column: str, row_number: int) -> Decimal:
    value = _required_text(row, column, row_number)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise MarketDataError(f"row {row_number}: {column} is malformed") from exc
    return parsed


def _parse_int(row: dict[str, str], column: str, row_number: int) -> int:
    value = _required_text(row, column, row_number)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise MarketDataError(f"row {row_number}: {column} is malformed") from exc
    return parsed
