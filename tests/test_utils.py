from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from options_engine.utils import (
    ExecutionMode,
    OrderSafetyRule,
    TradeDecision,
    configure_logging,
    parse_iso_datetime,
    require_timezone_aware,
    utc_now,
)


def test_time_helpers_require_timezone_aware_values() -> None:
    parsed = parse_iso_datetime("2026-06-20T14:00:00Z")

    assert parsed == datetime(2026, 6, 20, 14, 0, tzinfo=UTC)
    assert utc_now().tzinfo is not None
    with pytest.raises(ValueError, match="evaluated_at"):
        require_timezone_aware(datetime(2026, 6, 20, 14, 0), field_name="evaluated_at")


def test_shared_safety_enums_are_explicit() -> None:
    assert TradeDecision.NO_TRADE.value == "NO_TRADE"
    assert ExecutionMode.MANUAL_ONLY.value == "MANUAL_ONLY"
    assert OrderSafetyRule.LIVE_ORDERS_DISABLED.value == "LIVE_ORDERS_DISABLED"
    assert OrderSafetyRule.MARKET_ORDERS_FORBIDDEN.value == "MARKET_ORDERS_FORBIDDEN"


def test_configure_logging_emits_structured_json(capsys: Any) -> None:
    configure_logging(level=logging.INFO)
    logger = logging.getLogger("options_engine.test")

    logger.info("structured log line")
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "options_engine.test"
    assert payload["message"] == "structured log line"
    assert "timestamp" in payload
