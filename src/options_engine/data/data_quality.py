"""Data quality gates for required market, option-chain, and account inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from options_engine.data.market_data import PriceBar
from options_engine.data.option_chain import OptionChainSnapshot, OptionQuote
from options_engine.storage.models import AuditEvent, Position


class DataQualityDecision(StrEnum):
    """Data quality gate decision states."""

    PASS = "PASS"
    NO_TRADE = "NO_TRADE"


class DataQualitySeverity(StrEnum):
    """Severity levels for data-quality findings."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class DataQualityRejectionCode(StrEnum):
    """Stable rejection codes for data-quality failures."""

    DUPLICATE_QUOTES = "DUPLICATE_QUOTES"
    FUTURE_OPTION_CHAIN_DATA = "FUTURE_OPTION_CHAIN_DATA"
    FUTURE_PRICE_DATA = "FUTURE_PRICE_DATA"
    INVALID_BID_ASK = "INVALID_BID_ASK"
    MISSING_ACCOUNT_EQUITY = "MISSING_ACCOUNT_EQUITY"
    MISSING_GREEKS = "MISSING_GREEKS"
    MISSING_OPEN_POSITIONS = "MISSING_OPEN_POSITIONS"
    MISSING_OPTION_CHAIN_DATA = "MISSING_OPTION_CHAIN_DATA"
    MISSING_PRICE_DATA = "MISSING_PRICE_DATA"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    STALE_OPTION_CHAIN_DATA = "STALE_OPTION_CHAIN_DATA"
    STALE_PRICE_DATA = "STALE_PRICE_DATA"
    TIMESTAMP_TIMEZONE_INCONSISTENT = "TIMESTAMP_TIMEZONE_INCONSISTENT"
    TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"


SEVERITY_RANK: dict[DataQualitySeverity, int] = {
    DataQualitySeverity.INFO: 0,
    DataQualitySeverity.WARNING: 1,
    DataQualitySeverity.ERROR: 2,
    DataQualitySeverity.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class DataQualityPolicy:
    """Freshness thresholds for required inputs."""

    max_price_age: timedelta = timedelta(minutes=15)
    max_option_chain_age: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.max_price_age <= timedelta(0):
            raise ValueError("max_price_age must be positive")
        if self.max_option_chain_age <= timedelta(0):
            raise ValueError("max_option_chain_age must be positive")


@dataclass(frozen=True, slots=True)
class DataQualityRejectionReason:
    """One auditable data-quality rejection reason."""

    code: DataQualityRejectionCode
    message: str
    field: str
    severity: DataQualitySeverity = DataQualitySeverity.ERROR

    def to_dict(self) -> dict[str, str]:
        """Serialize the rejection reason for audit metadata."""
        return {
            "code": self.code.value,
            "message": self.message,
            "field": self.field,
            "severity": self.severity.value,
        }


@dataclass(frozen=True, slots=True)
class DataQualityResult:
    """Result of required data-quality checks."""

    decision: DataQualityDecision
    rejection_reasons: tuple[DataQualityRejectionReason, ...]
    checked_at: datetime
    severity: DataQualitySeverity
    reason_code: str
    message: str

    @property
    def passed(self) -> bool:
        """Return true when all required data-quality checks pass."""
        return self.decision == DataQualityDecision.PASS

    @classmethod
    def pass_result(cls, checked_at: datetime) -> DataQualityResult:
        """Create a passing data-quality result."""
        return cls(
            decision=DataQualityDecision.PASS,
            rejection_reasons=(),
            checked_at=checked_at,
            severity=DataQualitySeverity.INFO,
            reason_code="PASS",
            message="Data quality checks passed",
        )

    @classmethod
    def from_rejections(
        cls,
        rejection_reasons: list[DataQualityRejectionReason],
        checked_at: datetime,
    ) -> DataQualityResult:
        """Create a data-quality result from rejection reasons."""
        if not rejection_reasons:
            return cls.pass_result(checked_at)

        primary_reason = max(rejection_reasons, key=lambda reason: SEVERITY_RANK[reason.severity])
        decision = (
            DataQualityDecision.NO_TRADE
            if primary_reason.severity in {DataQualitySeverity.ERROR, DataQualitySeverity.CRITICAL}
            else DataQualityDecision.PASS
        )
        return cls(
            decision=decision,
            rejection_reasons=tuple(rejection_reasons),
            checked_at=checked_at,
            severity=primary_reason.severity,
            reason_code=primary_reason.code.value,
            message=primary_reason.message,
        )

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this data-quality result to a structured audit event."""
        if not config_version:
            raise ValueError("config_version is required")
        return AuditEvent(
            event_type="DATA_QUALITY_PASSED" if self.passed else "DATA_QUALITY_FAILED",
            entity_type="data_quality",
            message=self.message,
            metadata={
                "passed": self.passed,
                "decision": self.decision.value,
                "severity": self.severity.value,
                "reason_code": self.reason_code,
                "message": self.message,
                "checked_at": self.checked_at.isoformat(),
                "rejection_reasons": [reason.to_dict() for reason in self.rejection_reasons],
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.checked_at,
        )


def evaluate_required_data_quality(
    symbol: str,
    now: datetime,
    price_bars: list[PriceBar],
    option_chains: list[OptionChainSnapshot],
    policy: DataQualityPolicy | None = None,
    *,
    account_equity: Decimal | None = None,
    open_positions: list[Position] | None = None,
) -> DataQualityResult:
    """Validate that required data is present, fresh, complete, and internally consistent."""
    active_policy = policy or DataQualityPolicy()
    rejection_reasons: list[DataQualityRejectionReason] = []
    requested_symbol = symbol.strip().upper()
    checked_at = now if not _is_naive(now) else datetime.now(UTC)

    if not requested_symbol:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_REQUIRED_FIELD,
                "symbol is required",
                "symbol",
                DataQualitySeverity.CRITICAL,
            )
        )

    if _is_naive(now):
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.TIMEZONE_REQUIRED,
                "now must be timezone-aware",
                "now",
                DataQualitySeverity.CRITICAL,
            )
        )
        return DataQualityResult.from_rejections(rejection_reasons, checked_at)

    _validate_account_state(account_equity, open_positions, rejection_reasons)
    _validate_price_bars(price_bars, rejection_reasons)
    _validate_option_chains(option_chains, rejection_reasons)

    symbol_price_bars = [bar for bar in price_bars if _safe_symbol(bar) == requested_symbol]
    latest_price_bar = _latest_price_bar(symbol_price_bars)
    if latest_price_bar is None:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_PRICE_DATA,
                "required price data is missing",
                "price_bars",
            )
        )
    else:
        _validate_observation_timestamp(
            observed_at=latest_price_bar.timestamp,
            now=now,
            max_age=active_policy.max_price_age,
            future_code=DataQualityRejectionCode.FUTURE_PRICE_DATA,
            stale_code=DataQualityRejectionCode.STALE_PRICE_DATA,
            field="price_bars",
            label="price data",
            rejection_reasons=rejection_reasons,
        )

    symbol_option_chains = [snapshot for snapshot in option_chains if _safe_symbol(snapshot) == requested_symbol]
    latest_option_chain = _latest_option_chain(symbol_option_chains)
    if latest_option_chain is None:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_OPTION_CHAIN_DATA,
                "required option chain data is missing",
                "option_chains",
            )
        )
    else:
        _validate_observation_timestamp(
            observed_at=latest_option_chain.quote_timestamp,
            now=now,
            max_age=active_policy.max_option_chain_age,
            future_code=DataQualityRejectionCode.FUTURE_OPTION_CHAIN_DATA,
            stale_code=DataQualityRejectionCode.STALE_OPTION_CHAIN_DATA,
            field="option_chains",
            label="option chain data",
            rejection_reasons=rejection_reasons,
        )

    return DataQualityResult.from_rejections(rejection_reasons, checked_at)


def _validate_account_state(
    account_equity: Decimal | None,
    open_positions: list[Position] | None,
    rejection_reasons: list[DataQualityRejectionReason],
) -> None:
    if account_equity is None:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_ACCOUNT_EQUITY,
                "account equity is required",
                "account_equity",
                DataQualitySeverity.CRITICAL,
            )
        )
    elif account_equity <= Decimal("0"):
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_ACCOUNT_EQUITY,
                "account equity must be positive",
                "account_equity",
                DataQualitySeverity.CRITICAL,
            )
        )

    if open_positions is None:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_OPEN_POSITIONS,
                "open positions snapshot is required",
                "open_positions",
            )
        )


def _validate_price_bars(
    price_bars: list[PriceBar],
    rejection_reasons: list[DataQualityRejectionReason],
) -> None:
    required_fields = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
    price_fields = ("open", "high", "low", "close")
    for index, bar in enumerate(price_bars):
        label = f"price_bars[{index}]"
        for field_name in required_fields:
            if _missing(getattr(bar, field_name, None)):
                rejection_reasons.append(
                    _reject(
                        DataQualityRejectionCode.MISSING_REQUIRED_FIELD,
                        f"{label}.{field_name} is required",
                        f"{label}.{field_name}",
                    )
                )

        timestamp = getattr(bar, "timestamp", None)
        if isinstance(timestamp, datetime) and _is_naive(timestamp):
            rejection_reasons.append(
                _reject(
                    DataQualityRejectionCode.TIMEZONE_REQUIRED,
                    f"{label}.timestamp must be timezone-aware",
                    f"{label}.timestamp",
                )
            )

        for field_name in price_fields:
            value = getattr(bar, field_name, None)
            if value is not None and value < Decimal("0"):
                rejection_reasons.append(
                    _reject(
                        DataQualityRejectionCode.NEGATIVE_PRICE,
                        f"{label}.{field_name} must be non-negative",
                        f"{label}.{field_name}",
                    )
                )


def _validate_option_chains(
    option_chains: list[OptionChainSnapshot],
    rejection_reasons: list[DataQualityRejectionReason],
) -> None:
    seen_quote_keys: set[tuple[str, datetime, date, str, Decimal]] = set()
    for chain_index, snapshot in enumerate(option_chains):
        snapshot_label = f"option_chains[{chain_index}]"
        _validate_snapshot_fields(snapshot, snapshot_label, rejection_reasons)

        for quote_index, quote in enumerate(getattr(snapshot, "quotes", ()) or ()):
            quote_label = f"{snapshot_label}.quotes[{quote_index}]"
            _validate_option_quote_fields(snapshot, quote, quote_label, rejection_reasons)
            key = _quote_key(quote)
            if key is not None:
                if key in seen_quote_keys:
                    rejection_reasons.append(
                        _reject(
                            DataQualityRejectionCode.DUPLICATE_QUOTES,
                            "duplicate option quote detected",
                            quote_label,
                        )
                    )
                seen_quote_keys.add(key)


def _validate_snapshot_fields(
    snapshot: OptionChainSnapshot,
    snapshot_label: str,
    rejection_reasons: list[DataQualityRejectionReason],
) -> None:
    for field_name in ("symbol", "quote_timestamp", "expiration_date", "quotes"):
        if _missing(getattr(snapshot, field_name, None)):
            rejection_reasons.append(
                _reject(
                    DataQualityRejectionCode.MISSING_REQUIRED_FIELD,
                    f"{snapshot_label}.{field_name} is required",
                    f"{snapshot_label}.{field_name}",
                )
            )

    quote_timestamp = getattr(snapshot, "quote_timestamp", None)
    if isinstance(quote_timestamp, datetime) and _is_naive(quote_timestamp):
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.TIMEZONE_REQUIRED,
                f"{snapshot_label}.quote_timestamp must be timezone-aware",
                f"{snapshot_label}.quote_timestamp",
            )
        )


def _validate_option_quote_fields(
    snapshot: OptionChainSnapshot,
    quote: OptionQuote,
    quote_label: str,
    rejection_reasons: list[DataQualityRejectionReason],
) -> None:
    base_fields = (
        "symbol",
        "quote_timestamp",
        "expiration_date",
        "dte",
        "option_type",
        "strike",
        "bid",
        "ask",
        "mid",
        "open_interest",
        "volume",
    )
    greek_fields = ("iv", "delta", "gamma", "theta", "vega")
    for field_name in base_fields:
        if _missing(getattr(quote, field_name, None)):
            rejection_reasons.append(
                _reject(
                    DataQualityRejectionCode.MISSING_REQUIRED_FIELD,
                    f"{quote_label}.{field_name} is required",
                    f"{quote_label}.{field_name}",
                )
            )

    missing_greeks = [field_name for field_name in greek_fields if _missing(getattr(quote, field_name, None))]
    if missing_greeks:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_GREEKS,
                f"{quote_label} is missing required Greeks: {', '.join(missing_greeks)}",
                quote_label,
            )
        )

    quote_timestamp = getattr(quote, "quote_timestamp", None)
    if isinstance(quote_timestamp, datetime) and _is_naive(quote_timestamp):
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.TIMEZONE_REQUIRED,
                f"{quote_label}.quote_timestamp must be timezone-aware",
                f"{quote_label}.quote_timestamp",
            )
        )

    if quote_timestamp != snapshot.quote_timestamp:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.TIMESTAMP_TIMEZONE_INCONSISTENT,
                f"{quote_label}.quote_timestamp must match snapshot quote_timestamp",
                f"{quote_label}.quote_timestamp",
            )
        )

    bid = getattr(quote, "bid", None)
    ask = getattr(quote, "ask", None)
    mid = getattr(quote, "mid", None)
    if bid is not None and ask is not None and bid > ask:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.INVALID_BID_ASK,
                f"{quote_label}.bid must be less than or equal to ask",
                quote_label,
            )
        )

    for field_name in ("strike", "bid", "ask", "mid"):
        value = getattr(quote, field_name, None)
        if value is not None and value < Decimal("0"):
            rejection_reasons.append(
                _reject(
                    DataQualityRejectionCode.NEGATIVE_PRICE,
                    f"{quote_label}.{field_name} must be non-negative",
                    f"{quote_label}.{field_name}",
                )
            )

    if bid is not None and ask is not None and mid is not None and (mid < bid or mid > ask):
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.INVALID_BID_ASK,
                f"{quote_label}.mid must be within bid/ask range",
                f"{quote_label}.mid",
            )
        )

    dte = getattr(quote, "dte", None)
    if dte is not None and dte < 0:
        rejection_reasons.append(
            _reject(
                DataQualityRejectionCode.MISSING_REQUIRED_FIELD,
                f"{quote_label}.dte must be non-negative",
                f"{quote_label}.dte",
            )
        )


def _validate_observation_timestamp(
    observed_at: datetime,
    now: datetime,
    max_age: timedelta,
    future_code: DataQualityRejectionCode,
    stale_code: DataQualityRejectionCode,
    field: str,
    label: str,
    rejection_reasons: list[DataQualityRejectionReason],
) -> None:
    if _is_naive(observed_at):
        return

    if observed_at > now:
        rejection_reasons.append(
            _reject(
                future_code,
                f"{label} timestamp is in the future",
                field,
            )
        )
        return

    if now - observed_at > max_age:
        rejection_reasons.append(
            _reject(
                stale_code,
                f"{label} is stale",
                field,
            )
        )


def _latest_price_bar(price_bars: list[PriceBar]) -> PriceBar | None:
    valid_bars = [bar for bar in price_bars if isinstance(getattr(bar, "timestamp", None), datetime)]
    return max(valid_bars, key=lambda bar: bar.timestamp, default=None)


def _latest_option_chain(option_chains: list[OptionChainSnapshot]) -> OptionChainSnapshot | None:
    valid_chains = [chain for chain in option_chains if isinstance(getattr(chain, "quote_timestamp", None), datetime)]
    return max(valid_chains, key=lambda chain: chain.quote_timestamp, default=None)


def _quote_key(quote: OptionQuote) -> tuple[str, datetime, date, str, Decimal] | None:
    quote_timestamp = getattr(quote, "quote_timestamp", None)
    expiration_date = getattr(quote, "expiration_date", None)
    strike = getattr(quote, "strike", None)
    option_type = getattr(quote, "option_type", None)
    symbol = _safe_symbol(quote)
    if (
        not symbol
        or not isinstance(quote_timestamp, datetime)
        or not isinstance(expiration_date, date)
        or strike is None
        or option_type is None
    ):
        return None
    option_type_value = option_type.value if hasattr(option_type, "value") else str(option_type)
    return (symbol, quote_timestamp, expiration_date, option_type_value, strike)


def _safe_symbol(value: Any) -> str:
    symbol = getattr(value, "symbol", "")
    return "" if symbol is None else str(symbol).strip().upper()


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_naive(value: datetime) -> bool:
    return value.tzinfo is None or value.utcoffset() is None


def _reject(
    code: DataQualityRejectionCode,
    message: str,
    field: str,
    severity: DataQualitySeverity = DataQualitySeverity.ERROR,
) -> DataQualityRejectionReason:
    return DataQualityRejectionReason(code=code, message=message, field=field, severity=severity)
