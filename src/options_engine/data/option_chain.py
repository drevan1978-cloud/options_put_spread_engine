"""Option chain interfaces and CSV ingestion for local option quote data."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

REQUIRED_OPTION_CHAIN_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "symbol",
        "timestamp",
        "expiration",
        "dte",
        "option_type",
        "strike",
        "bid",
        "ask",
        "iv",
        "delta",
        "gamma",
        "theta",
        "vega",
        "open_interest",
        "volume",
    }
)

COLUMN_ALIASES: Final[dict[str, str]] = {
    "quote_timestamp": "timestamp",
    "expiration_date": "expiration",
}


class OptionChainError(ValueError):
    """Raised when option chain data is missing or malformed."""


class OptionType(StrEnum):
    """Supported listed option contract types."""

    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """Validated option quote row from a chain snapshot."""

    symbol: str
    quote_timestamp: datetime
    expiration_date: date
    dte: int
    option_type: OptionType
    strike: Decimal
    bid: Decimal
    ask: Decimal
    mid: Decimal
    iv: Decimal
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal
    volume: int
    open_interest: int

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise OptionChainError("symbol is required")

        if self.quote_timestamp.tzinfo is None or self.quote_timestamp.utcoffset() is None:
            raise OptionChainError("quote_timestamp must be timezone-aware")

        if self.strike <= Decimal("0"):
            raise OptionChainError("strike must be positive")
        if self.bid < Decimal("0"):
            raise OptionChainError("bid must be non-negative")
        if self.ask < Decimal("0"):
            raise OptionChainError("ask must be non-negative")
        if self.ask < self.bid:
            raise OptionChainError("ask must be greater than or equal to bid")
        if self.mid < Decimal("0"):
            raise OptionChainError("mid must be non-negative")
        if self.mid < self.bid or self.mid > self.ask:
            raise OptionChainError("mid must be within bid/ask range")
        if self.dte < 0:
            raise OptionChainError("dte must be non-negative")
        if self.iv < Decimal("0"):
            raise OptionChainError("iv must be non-negative")
        if self.delta < Decimal("-1") or self.delta > Decimal("1"):
            raise OptionChainError("delta must be between -1 and 1")
        if self.gamma < Decimal("0"):
            raise OptionChainError("gamma must be non-negative")
        if self.vega < Decimal("0"):
            raise OptionChainError("vega must be non-negative")
        if self.volume < 0:
            raise OptionChainError("volume must be non-negative")
        if self.open_interest < 0:
            raise OptionChainError("open_interest must be non-negative")

        object.__setattr__(self, "symbol", normalized_symbol)

    def to_dict(self) -> dict[str, str | int]:
        """Serialize the quote to JSON-safe primitives."""
        return {
            "symbol": self.symbol,
            "timestamp": self.quote_timestamp.isoformat(),
            "expiration": self.expiration_date.isoformat(),
            "dte": self.dte,
            "option_type": self.option_type.value,
            "strike": str(self.strike),
            "bid": str(self.bid),
            "ask": str(self.ask),
            "mid": str(self.mid),
            "iv": str(self.iv),
            "delta": str(self.delta),
            "gamma": str(self.gamma),
            "theta": str(self.theta),
            "vega": str(self.vega),
            "volume": self.volume,
            "open_interest": self.open_interest,
        }


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """Validated option chain snapshot for one symbol, expiration, and quote time."""

    symbol: str
    quote_timestamp: datetime
    expiration_date: date
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise OptionChainError("symbol is required")
        if not self.quotes:
            raise OptionChainError("option chain snapshot must contain at least one quote")

        for quote in self.quotes:
            if quote.symbol != normalized_symbol:
                raise OptionChainError("all quotes in a snapshot must share the same symbol")
            if quote.quote_timestamp != self.quote_timestamp:
                raise OptionChainError("all quotes in a snapshot must share the same quote_timestamp")
            if quote.expiration_date != self.expiration_date:
                raise OptionChainError("all quotes in a snapshot must share the same expiration_date")

        object.__setattr__(self, "symbol", normalized_symbol)

    def to_chain_json(self) -> str:
        """Serialize the snapshot quotes for storage."""
        payload = {
            "symbol": self.symbol,
            "quote_timestamp": self.quote_timestamp.isoformat(),
            "expiration_date": self.expiration_date.isoformat(),
            "quotes": [quote.to_dict() for quote in self.quotes],
        }
        return json.dumps(payload, sort_keys=True)


class OptionChainProvider(Protocol):
    """Interface for option chain providers."""

    def load_option_chain(self, symbol: str) -> list[OptionChainSnapshot]:
        """Load option chain snapshots for a symbol."""


class CSVOptionChainProvider:
    """Option chain provider backed by a local CSV file."""

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = csv_path

    def load_option_chain(self, symbol: str) -> list[OptionChainSnapshot]:
        """Load and filter option chain snapshots from the configured CSV file."""
        requested_symbol = symbol.strip().upper()
        if not requested_symbol:
            raise OptionChainError("symbol is required")

        snapshots = load_option_chain_csv(self.csv_path)
        return [snapshot for snapshot in snapshots if snapshot.symbol == requested_symbol]


def load_option_chain_csv(csv_path: Path) -> list[OptionChainSnapshot]:
    """Load validated option chain snapshots from a CSV file."""
    if not csv_path.exists():
        raise OptionChainError(f"CSV file does not exist: {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        _validate_columns(reader.fieldnames, csv_path)
        quotes = [_parse_option_quote(_normalize_row(row), row_number=index + 2) for index, row in enumerate(reader)]

    if not quotes:
        raise OptionChainError(f"CSV file contains no option quote rows: {csv_path}")

    grouped_quotes: dict[tuple[str, datetime, date], list[OptionQuote]] = {}
    for quote in quotes:
        key = (quote.symbol, quote.quote_timestamp, quote.expiration_date)
        grouped_quotes.setdefault(key, []).append(quote)

    return [
        OptionChainSnapshot(
            symbol=symbol,
            quote_timestamp=quote_timestamp,
            expiration_date=expiration_date,
            quotes=tuple(grouped),
        )
        for (symbol, quote_timestamp, expiration_date), grouped in sorted(
            grouped_quotes.items(),
            key=lambda item: (item[0][0], item[0][1], item[0][2]),
        )
    ]


def _validate_columns(fieldnames: list[str] | None, csv_path: Path) -> None:
    if fieldnames is None:
        raise OptionChainError(f"CSV file is empty: {csv_path}")

    normalized = {_normalize_column_name(field) for field in fieldnames}
    missing = REQUIRED_OPTION_CHAIN_COLUMNS.difference(normalized)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise OptionChainError(f"CSV missing required columns: {missing_columns}")


def _parse_option_quote(row: dict[str, str], row_number: int) -> OptionQuote:
    bid = _parse_decimal(row, "bid", row_number)
    ask = _parse_decimal(row, "ask", row_number)
    return OptionQuote(
        symbol=_required_text(row, "symbol", row_number),
        quote_timestamp=_parse_timestamp(_required_text(row, "timestamp", row_number), row_number),
        expiration_date=_parse_date(_required_text(row, "expiration", row_number), row_number),
        dte=_parse_int(row, "dte", row_number),
        option_type=_parse_option_type(_required_text(row, "option_type", row_number), row_number),
        strike=_parse_decimal(row, "strike", row_number),
        bid=bid,
        ask=ask,
        mid=_parse_mid(row, bid, ask, row_number),
        iv=_parse_decimal(row, "iv", row_number),
        delta=_parse_decimal(row, "delta", row_number),
        gamma=_parse_decimal(row, "gamma", row_number),
        theta=_parse_decimal(row, "theta", row_number),
        vega=_parse_decimal(row, "vega", row_number),
        volume=_parse_int(row, "volume", row_number),
        open_interest=_parse_int(row, "open_interest", row_number),
    )


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {_normalize_column_name(column): value for column, value in row.items()}


def _normalize_column_name(column: str) -> str:
    stripped = column.strip()
    return COLUMN_ALIASES.get(stripped, stripped)


def _required_text(row: dict[str, str], column: str, row_number: int) -> str:
    value = row.get(column)
    if value is None or value.strip() == "":
        raise OptionChainError(f"row {row_number}: {column} is required")
    return value.strip()


def _parse_timestamp(value: str, row_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OptionChainError(f"row {row_number}: quote_timestamp is malformed") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OptionChainError(f"row {row_number}: quote_timestamp must be timezone-aware")
    return parsed


def _parse_date(value: str, row_number: int) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise OptionChainError(f"row {row_number}: expiration_date is malformed") from exc


def _parse_option_type(value: str, row_number: int) -> OptionType:
    try:
        return OptionType(value.strip().upper())
    except ValueError as exc:
        raise OptionChainError(f"row {row_number}: option_type must be CALL or PUT") from exc


def _parse_decimal(row: dict[str, str], column: str, row_number: int) -> Decimal:
    value = _required_text(row, column, row_number)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise OptionChainError(f"row {row_number}: {column} is malformed") from exc


def _parse_mid(row: dict[str, str], bid: Decimal, ask: Decimal, row_number: int) -> Decimal:
    value = row.get("mid")
    if value is None or value.strip() == "":
        return (bid + ask) / Decimal("2")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise OptionChainError(f"row {row_number}: mid is malformed") from exc


def _parse_int(row: dict[str, str], column: str, row_number: int) -> int:
    value = _required_text(row, column, row_number)
    try:
        return int(value)
    except ValueError as exc:
        raise OptionChainError(f"row {row_number}: {column} is malformed") from exc


def option_chain_storage_payload(snapshot: OptionChainSnapshot) -> dict[str, Any]:
    """Return storage-ready fields for an option chain snapshot."""
    return {
        "symbol": snapshot.symbol,
        "expiration_date": snapshot.expiration_date,
        "quote_timestamp": snapshot.quote_timestamp,
        "chain_json": snapshot.to_chain_json(),
    }
