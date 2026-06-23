"""Deterministic market regime state machine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from options_engine.data.data_quality import DataQualityResult, DataQualitySeverity
from options_engine.data.market_data import PriceBar
from options_engine.storage.models import RegimeState as StoredRegimeState


class RegimeLabel(StrEnum):
    """Supported deterministic regime labels."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    BLACK = "BLACK"

    BULLISH = "GREEN"
    NEUTRAL = "YELLOW"
    BEARISH = "RED"
    UNKNOWN = "BLACK"


class VIXTermStructureStatus(StrEnum):
    """VIX term-structure status inputs."""

    NORMAL = "NORMAL"
    INVERTED = "INVERTED"
    UNKNOWN = "UNKNOWN"


class AccountReconciliationStatus(StrEnum):
    """Broker/account reconciliation status inputs."""

    RECONCILED = "RECONCILED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class RegimeReasonCode(StrEnum):
    """Stable reason codes for regime classifications."""

    ABNORMAL_LOSS_CLUSTER = "ABNORMAL_LOSS_CLUSTER"
    BROKER_ACCOUNT_RECONCILIATION_FAILED = "BROKER_ACCOUNT_RECONCILIATION_FAILED"
    DATA_QUALITY_CRITICAL_FAILURE = "DATA_QUALITY_CRITICAL_FAILURE"
    DATA_QUALITY_FAILED = "DATA_QUALITY_FAILED"
    DATA_QUALITY_PASSED = "DATA_QUALITY_PASSED"
    GREEN_CONDITIONS_MET = "GREEN_CONDITIONS_MET"
    HARD_LOSS_CAP_BREACHED = "HARD_LOSS_CAP_BREACHED"
    INSUFFICIENT_PRICE_HISTORY = "INSUFFICIENT_PRICE_HISTORY"
    IV_ABOVE_RV = "IV_ABOVE_RV"
    MISSING_PRICE_DATA = "MISSING_PRICE_DATA"
    OPEN_RISK_NOT_VERIFIED = "OPEN_RISK_NOT_VERIFIED"
    PRICE_ABOVE_50DMA = "PRICE_ABOVE_50DMA"
    PRICE_BELOW_50DMA = "PRICE_BELOW_50DMA"
    REALIZED_VOL_ABOVE_IV = "REALIZED_VOL_ABOVE_IV"
    TIMEZONE_REQUIRED = "TIMEZONE_REQUIRED"
    TREND_WEAKENING = "TREND_WEAKENING"
    VIX_TERM_STRUCTURE_INVERTED = "VIX_TERM_STRUCTURE_INVERTED"
    VIX_TERM_STRUCTURE_NORMAL = "VIX_TERM_STRUCTURE_NORMAL"
    VIX_TERM_STRUCTURE_UNKNOWN = "VIX_TERM_STRUCTURE_UNKNOWN"
    VOLATILITY_ELEVATED = "VOLATILITY_ELEVATED"


RegimeRejectionCode = RegimeReasonCode


@dataclass(frozen=True, slots=True)
class RegimePolicy:
    """Static thresholds for deterministic regime classification."""

    volatility_elevated_iv_threshold: Decimal = Decimal("0.30")
    trend_weakening_buffer_pct: Decimal = Decimal("0.01")
    lookback_bars: int = 5
    moving_average_bars: int = 3

    def __post_init__(self) -> None:
        if self.volatility_elevated_iv_threshold <= Decimal("0"):
            raise ValueError("volatility_elevated_iv_threshold must be positive")
        if self.trend_weakening_buffer_pct < Decimal("0"):
            raise ValueError("trend_weakening_buffer_pct must be non-negative")
        if self.lookback_bars < 2:
            raise ValueError("lookback_bars must be at least 2")
        if self.moving_average_bars < 2:
            raise ValueError("moving_average_bars must be at least 2")
        if self.moving_average_bars > self.lookback_bars:
            raise ValueError("moving_average_bars must not exceed lookback_bars")


@dataclass(frozen=True, slots=True)
class RegimeInputs:
    """Inputs to the simple regime state machine."""

    symbol: str
    as_of: datetime
    underlying_close: Decimal
    moving_average_50: Decimal
    implied_volatility: Decimal
    realized_volatility: Decimal
    vix_term_structure: VIXTermStructureStatus
    data_quality: DataQualityResult
    account_reconciliation: AccountReconciliationStatus
    open_risk_verified: bool
    hard_loss_cap_breached: bool = False
    abnormal_loss_cluster: bool = False

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if self.underlying_close <= Decimal("0"):
            raise ValueError("underlying_close must be positive")
        if self.moving_average_50 <= Decimal("0"):
            raise ValueError("moving_average_50 must be positive")
        if self.implied_volatility < Decimal("0"):
            raise ValueError("implied_volatility must be non-negative")
        if self.realized_volatility < Decimal("0"):
            raise ValueError("realized_volatility must be non-negative")
        object.__setattr__(self, "symbol", normalized_symbol)


@dataclass(frozen=True, slots=True)
class RegimeReason:
    """One auditable reason attached to a regime classification."""

    code: RegimeReasonCode
    message: str
    field: str

    def to_dict(self) -> dict[str, str]:
        """Serialize the reason for audit storage."""
        return {
            "code": self.code.value,
            "message": self.message,
            "field": self.field,
        }


RegimeRejectionReason = RegimeReason


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    """Result of a deterministic regime classification."""

    symbol: str
    as_of: datetime
    regime: RegimeLabel
    details: dict[str, Any]
    reasons: tuple[RegimeReason, ...]

    @property
    def rejection_reasons(self) -> tuple[RegimeReason, ...]:
        """Backward-compatible alias for reason codes."""
        return self.reasons

    @property
    def is_unknown(self) -> bool:
        """Return true when the classifier produced the shutdown state."""
        return self.regime == RegimeLabel.BLACK

    def details_json(self) -> str:
        """Serialize details for database storage."""
        payload = {
            **self.details,
            "reason_codes": [reason.code.value for reason in self.reasons],
            "reasons": [reason.to_dict() for reason in self.reasons],
        }
        return json.dumps(payload, sort_keys=True)

    def to_storage_model(self, config_version: str) -> StoredRegimeState:
        """Convert the classification to the persistent storage model."""
        return StoredRegimeState(
            symbol=self.symbol,
            as_of=self.as_of,
            regime=self.regime.value,
            details_json=self.details_json(),
            config_version=config_version,
        )


def classify_regime_state(inputs: RegimeInputs, policy: RegimePolicy | None = None) -> RegimeClassification:
    """Classify regime using a simple deterministic state machine."""
    active_policy = policy or RegimePolicy()

    reasons = _black_reasons(inputs)
    if reasons:
        return _classification(RegimeLabel.BLACK, inputs, active_policy, reasons)

    reasons = _red_reasons(inputs)
    if reasons:
        return _classification(RegimeLabel.RED, inputs, active_policy, reasons)

    reasons = _yellow_reasons(inputs, active_policy)
    if reasons:
        return _classification(RegimeLabel.YELLOW, inputs, active_policy, reasons)

    reasons = [
        _reason(RegimeReasonCode.GREEN_CONDITIONS_MET, "all green regime conditions are satisfied", "regime"),
        _reason(RegimeReasonCode.PRICE_ABOVE_50DMA, "underlying close is above 50-day moving average", "underlying_close"),
        _reason(RegimeReasonCode.IV_ABOVE_RV, "implied volatility is above realized volatility", "implied_volatility"),
        _reason(RegimeReasonCode.VIX_TERM_STRUCTURE_NORMAL, "VIX term structure is normal", "vix_term_structure"),
        _reason(RegimeReasonCode.DATA_QUALITY_PASSED, "data quality gate passed", "data_quality"),
    ]
    return _classification(RegimeLabel.GREEN, inputs, active_policy, reasons)


def classify_regime(
    symbol: str,
    price_bars: list[PriceBar],
    policy: RegimePolicy | None = None,
) -> RegimeClassification:
    """Backward-compatible price-only classifier that returns state-machine labels."""
    active_policy = policy or RegimePolicy()
    requested_symbol = symbol.strip().upper()
    symbol_bars = sorted([bar for bar in price_bars if bar.symbol == requested_symbol], key=lambda bar: bar.timestamp)

    if not symbol_bars:
        as_of = datetime.min.replace(tzinfo=UTC)
        return RegimeClassification(
            symbol=requested_symbol,
            as_of=as_of,
            regime=RegimeLabel.BLACK,
            details={"classifier": "deterministic_regime_state_machine_v1", "legacy_price_only": True},
            reasons=(_reason(RegimeReasonCode.MISSING_PRICE_DATA, "required price data is missing", "price_bars"),),
        )

    if any(bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None for bar in symbol_bars):
        return RegimeClassification(
            symbol=requested_symbol,
            as_of=symbol_bars[-1].timestamp,
            regime=RegimeLabel.BLACK,
            details={"classifier": "deterministic_regime_state_machine_v1", "legacy_price_only": True},
            reasons=(_reason(RegimeReasonCode.TIMEZONE_REQUIRED, "price bar timestamps must be timezone-aware", "price_bars"),),
        )

    if len(symbol_bars) < active_policy.lookback_bars:
        return RegimeClassification(
            symbol=requested_symbol,
            as_of=symbol_bars[-1].timestamp,
            regime=RegimeLabel.BLACK,
            details={
                "classifier": "deterministic_regime_state_machine_v1",
                "legacy_price_only": True,
                "available_bars": len(symbol_bars),
            },
            reasons=(
                _reason(
                    RegimeReasonCode.INSUFFICIENT_PRICE_HISTORY,
                    "not enough price bars for deterministic regime classification",
                    "price_bars",
                ),
            ),
        )

    window = symbol_bars[-active_policy.lookback_bars :]
    latest_close = window[-1].close
    moving_average = _average_close(window[-active_policy.moving_average_bars :])
    label = RegimeLabel.GREEN if latest_close > moving_average else RegimeLabel.RED
    code = RegimeReasonCode.PRICE_ABOVE_50DMA if label == RegimeLabel.GREEN else RegimeReasonCode.PRICE_BELOW_50DMA
    message = (
        "underlying close is above moving average"
        if label == RegimeLabel.GREEN
        else "underlying close is below moving average"
    )
    return RegimeClassification(
        symbol=requested_symbol,
        as_of=window[-1].timestamp,
        regime=label,
        details={
            "classifier": "deterministic_regime_state_machine_v1",
            "legacy_price_only": True,
            "used_bars": len(window),
            "latest_close": str(latest_close),
            "moving_average": str(moving_average),
        },
        reasons=(_reason(code, message, "underlying_close"),),
    )


def _black_reasons(inputs: RegimeInputs) -> list[RegimeReason]:
    reasons: list[RegimeReason] = []
    if inputs.data_quality.severity == DataQualitySeverity.CRITICAL:
        reasons.append(
            _reason(
                RegimeReasonCode.DATA_QUALITY_CRITICAL_FAILURE,
                "data quality has a critical failure",
                "data_quality",
            )
        )
    if inputs.account_reconciliation != AccountReconciliationStatus.RECONCILED:
        reasons.append(
            _reason(
                RegimeReasonCode.BROKER_ACCOUNT_RECONCILIATION_FAILED,
                "broker/account reconciliation failed or is unknown",
                "account_reconciliation",
            )
        )
    if not inputs.open_risk_verified:
        reasons.append(
            _reason(
                RegimeReasonCode.OPEN_RISK_NOT_VERIFIED,
                "open risk cannot be verified",
                "open_risk_verified",
            )
        )
    if inputs.hard_loss_cap_breached:
        reasons.append(
            _reason(
                RegimeReasonCode.HARD_LOSS_CAP_BREACHED,
                "hard loss cap has been breached",
                "hard_loss_cap_breached",
            )
        )
    return reasons


def _red_reasons(inputs: RegimeInputs) -> list[RegimeReason]:
    reasons: list[RegimeReason] = []
    if inputs.underlying_close < inputs.moving_average_50:
        reasons.append(
            _reason(
                RegimeReasonCode.PRICE_BELOW_50DMA,
                "underlying close is below 50-day moving average",
                "underlying_close",
            )
        )
    if inputs.vix_term_structure == VIXTermStructureStatus.INVERTED:
        reasons.append(
            _reason(
                RegimeReasonCode.VIX_TERM_STRUCTURE_INVERTED,
                "VIX term structure is inverted",
                "vix_term_structure",
            )
        )
    if inputs.vix_term_structure == VIXTermStructureStatus.UNKNOWN:
        reasons.append(
            _reason(
                RegimeReasonCode.VIX_TERM_STRUCTURE_UNKNOWN,
                "VIX term structure status is unknown",
                "vix_term_structure",
            )
        )
    if inputs.realized_volatility > inputs.implied_volatility:
        reasons.append(
            _reason(
                RegimeReasonCode.REALIZED_VOL_ABOVE_IV,
                "realized volatility is above implied volatility",
                "realized_volatility",
            )
        )
    if inputs.abnormal_loss_cluster:
        reasons.append(
            _reason(RegimeReasonCode.ABNORMAL_LOSS_CLUSTER, "abnormal loss cluster detected", "abnormal_loss_cluster")
        )
    if not inputs.data_quality.passed:
        reasons.append(_reason(RegimeReasonCode.DATA_QUALITY_FAILED, "data quality gate failed", "data_quality"))
    return reasons


def _yellow_reasons(inputs: RegimeInputs, policy: RegimePolicy) -> list[RegimeReason]:
    if inputs.underlying_close <= inputs.moving_average_50:
        return []

    reasons: list[RegimeReason] = []
    price_buffer = (inputs.underlying_close - inputs.moving_average_50) / inputs.moving_average_50
    if price_buffer <= policy.trend_weakening_buffer_pct:
        reasons.append(
            _reason(
                RegimeReasonCode.TREND_WEAKENING,
                "price is above 50DMA but trend buffer is thin",
                "underlying_close",
            )
        )
    if inputs.implied_volatility >= policy.volatility_elevated_iv_threshold:
        reasons.append(
            _reason(
                RegimeReasonCode.VOLATILITY_ELEVATED,
                "implied volatility is elevated",
                "implied_volatility",
            )
        )
    return reasons


def _classification(
    label: RegimeLabel,
    inputs: RegimeInputs,
    policy: RegimePolicy,
    reasons: list[RegimeReason],
) -> RegimeClassification:
    return RegimeClassification(
        symbol=inputs.symbol,
        as_of=inputs.as_of,
        regime=label,
        details={
            "classifier": "deterministic_regime_state_machine_v1",
            "underlying_close": str(inputs.underlying_close),
            "moving_average_50": str(inputs.moving_average_50),
            "implied_volatility": str(inputs.implied_volatility),
            "realized_volatility": str(inputs.realized_volatility),
            "vix_term_structure": inputs.vix_term_structure.value,
            "data_quality_passed": inputs.data_quality.passed,
            "data_quality_severity": inputs.data_quality.severity.value,
            "account_reconciliation": inputs.account_reconciliation.value,
            "open_risk_verified": inputs.open_risk_verified,
            "hard_loss_cap_breached": inputs.hard_loss_cap_breached,
            "abnormal_loss_cluster": inputs.abnormal_loss_cluster,
            "volatility_elevated_iv_threshold": str(policy.volatility_elevated_iv_threshold),
            "trend_weakening_buffer_pct": str(policy.trend_weakening_buffer_pct),
        },
        reasons=tuple(reasons),
    )


def _average_close(price_bars: list[PriceBar]) -> Decimal:
    return sum((bar.close for bar in price_bars), Decimal("0")) / Decimal(len(price_bars))


def _reason(code: RegimeReasonCode, message: str, field: str) -> RegimeReason:
    return RegimeReason(code=code, message=message, field=field)
