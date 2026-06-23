"""Shared safety-focused domain enums."""

from __future__ import annotations

from enum import StrEnum


class TradeDecision(StrEnum):
    """Top-level trade decision states."""

    NO_TRADE = "NO_TRADE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ExecutionMode(StrEnum):
    """Supported execution modes for v1."""

    MANUAL_ONLY = "MANUAL_ONLY"


class OrderSafetyRule(StrEnum):
    """Stable order-safety rule names."""

    LIVE_ORDERS_DISABLED = "LIVE_ORDERS_DISABLED"
    MARKET_ORDERS_FORBIDDEN = "MARKET_ORDERS_FORBIDDEN"
    MARTINGALE_FORBIDDEN = "MARTINGALE_FORBIDDEN"
