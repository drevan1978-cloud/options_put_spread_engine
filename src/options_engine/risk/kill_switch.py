"""Hard kill-switch risk controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from options_engine.config.loader import RiskLimits
from options_engine.data.data_quality import DataQualityResult, DataQualitySeverity
from options_engine.regime import RegimeLabel
from options_engine.risk.models import (
    RiskCheckResult,
    RiskRejectionCode,
    RiskRejectionReason,
    RiskState,
    reject,
)
from options_engine.risk.sizing import calculate_risk_limit_amounts
from options_engine.storage.models import AuditEvent


class KillSwitchError(ValueError):
    """Raised when kill-switch inputs or resets are invalid."""


class KillSwitchState(StrEnum):
    """Hard kill-switch states."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    BLACK = "BLACK"


class KillSwitchAction(StrEnum):
    """Allowed system actions for each kill-switch state."""

    NORMAL_RECOMMENDATION_ALLOWED = "NORMAL_RECOMMENDATION_ALLOWED"
    REDUCED_SIZE_ONLY = "REDUCED_SIZE_ONLY"
    NO_NEW_TRADES_MANAGE_EXISTING_ONLY = "NO_NEW_TRADES_MANAGE_EXISTING_ONLY"
    RISK_OFF_REVIEW_REQUIRED = "RISK_OFF_REVIEW_REQUIRED"


class KillSwitchReasonCode(StrEnum):
    """Stable kill-switch reason codes."""

    ACCOUNT_EQUITY_MISSING = "ACCOUNT_EQUITY_MISSING"
    BROKER_ACCOUNT_RECONCILIATION_FAILED = "BROKER_ACCOUNT_RECONCILIATION_FAILED"
    DAILY_LOSS_CAP_BREACHED = "DAILY_LOSS_CAP_BREACHED"
    DATA_FEED_CRITICAL_FAILURE = "DATA_FEED_CRITICAL_FAILURE"
    DUPLICATE_ORDER_RISK_DETECTED = "DUPLICATE_ORDER_RISK_DETECTED"
    EXPLICIT_RESET = "EXPLICIT_RESET"
    MONTHLY_LOSS_CAP_BREACHED = "MONTHLY_LOSS_CAP_BREACHED"
    OPEN_POSITIONS_UNVERIFIED = "OPEN_POSITIONS_UNVERIFIED"
    RED_REGIME = "RED_REGIME"
    REDUCED_SIZE_REQUIRED = "REDUCED_SIZE_REQUIRED"
    REGIME_BLACK = "REGIME_BLACK"
    STATE_GREEN = "STATE_GREEN"
    WEEKLY_LOSS_CAP_BREACHED = "WEEKLY_LOSS_CAP_BREACHED"
    WRONG_WAY_ORDER_RISK_DETECTED = "WRONG_WAY_ORDER_RISK_DETECTED"


@dataclass(frozen=True, slots=True)
class KillSwitchReason:
    """One auditable reason attached to a kill-switch state."""

    code: KillSwitchReasonCode
    message: str
    field: str

    def to_dict(self) -> dict[str, str]:
        """Serialize this kill-switch reason for audit metadata."""
        return {
            "code": self.code.value,
            "message": self.message,
            "field": self.field,
        }


@dataclass(frozen=True, slots=True)
class KillSwitchInputs:
    """Inputs for hard kill-switch state evaluation."""

    evaluated_at: datetime
    account_equity: Decimal | None
    data_quality: DataQualityResult | None = None
    data_feed_critical_failure: bool = False
    open_positions_verified: bool = True
    broker_account_reconciled: bool = True
    daily_loss_cap_breached: bool = False
    weekly_loss_cap_breached: bool = False
    monthly_loss_cap_breached: bool = False
    duplicate_order_risk_detected: bool = False
    wrong_way_order_risk_detected: bool = False
    current_regime: RegimeLabel | str = RegimeLabel.GREEN
    reduced_size_required: bool = False

    def __post_init__(self) -> None:
        if _is_naive(self.evaluated_at):
            raise KillSwitchError("evaluated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    """Auditable hard kill-switch decision."""

    state: KillSwitchState
    action: KillSwitchAction
    reasons: tuple[KillSwitchReason, ...]
    evaluated_at: datetime

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Return stable string reason codes."""
        return tuple(reason.code.value for reason in self.reasons)

    @property
    def allow_new_trades(self) -> bool:
        """Return true when the state allows new trade consideration."""
        return self.state in {KillSwitchState.GREEN, KillSwitchState.YELLOW}

    @property
    def allow_ticket_generation(self) -> bool:
        """Return true when manual new-trade ticket generation is allowed."""
        return self.allow_new_trades

    @property
    def reduced_size_only(self) -> bool:
        """Return true when the state only allows reduced sizing."""
        return self.state == KillSwitchState.YELLOW

    @property
    def risk_off_review_required(self) -> bool:
        """Return true when BLACK state requires risk-off review."""
        return self.state == KillSwitchState.BLACK

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this kill-switch decision to a structured audit event."""
        if not config_version:
            raise KillSwitchError("config_version is required")
        return AuditEvent(
            event_type=f"KILL_SWITCH_{self.state.value}",
            entity_type="kill_switch",
            message=f"Kill switch state: {self.state.value}",
            metadata={
                "state": self.state.value,
                "action": self.action.value,
                "reason_codes": list(self.reason_codes),
                "reasons": [reason.to_dict() for reason in self.reasons],
                "allow_new_trades": self.allow_new_trades,
                "allow_ticket_generation": self.allow_ticket_generation,
                "reduced_size_only": self.reduced_size_only,
                "risk_off_review_required": self.risk_off_review_required,
                "evaluated_at": self.evaluated_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.evaluated_at,
        )


@dataclass(frozen=True, slots=True)
class KillSwitchResetResult:
    """Auditable explicit kill-switch reset event."""

    previous_state: KillSwitchState
    new_state: KillSwitchState
    reset_reason: str
    reset_at: datetime
    previous_reason_codes: tuple[str, ...]

    def to_audit_event(self, config_version: str) -> AuditEvent:
        """Convert this explicit reset to a structured audit event."""
        if not config_version:
            raise KillSwitchError("config_version is required")
        return AuditEvent(
            event_type="KILL_SWITCH_RESET",
            entity_type="kill_switch",
            message="Kill switch reset with explicit reason",
            metadata={
                "previous_state": self.previous_state.value,
                "new_state": self.new_state.value,
                "reset_reason": self.reset_reason,
                "previous_reason_codes": list(self.previous_reason_codes),
                "reset_at": self.reset_at.isoformat(),
                "config_version": config_version,
            },
            config_version=config_version,
            created_at=self.reset_at,
        )


def evaluate_kill_switch_state(inputs: KillSwitchInputs) -> KillSwitchDecision:
    """Evaluate hard kill-switch state from explicit inputs."""
    black_reasons = _black_reasons(inputs)
    if black_reasons:
        return KillSwitchDecision(
            state=KillSwitchState.BLACK,
            action=KillSwitchAction.RISK_OFF_REVIEW_REQUIRED,
            reasons=tuple(black_reasons),
            evaluated_at=inputs.evaluated_at,
        )

    red_reasons = _red_reasons(inputs)
    if red_reasons:
        return KillSwitchDecision(
            state=KillSwitchState.RED,
            action=KillSwitchAction.NO_NEW_TRADES_MANAGE_EXISTING_ONLY,
            reasons=tuple(red_reasons),
            evaluated_at=inputs.evaluated_at,
        )

    yellow_reasons = _yellow_reasons(inputs)
    if yellow_reasons:
        return KillSwitchDecision(
            state=KillSwitchState.YELLOW,
            action=KillSwitchAction.REDUCED_SIZE_ONLY,
            reasons=tuple(yellow_reasons),
            evaluated_at=inputs.evaluated_at,
        )

    return KillSwitchDecision(
        state=KillSwitchState.GREEN,
        action=KillSwitchAction.NORMAL_RECOMMENDATION_ALLOWED,
        reasons=(
            KillSwitchReason(
                code=KillSwitchReasonCode.STATE_GREEN,
                message="kill switch is green; normal recommendations are allowed",
                field="kill_switch_state",
            ),
        ),
        evaluated_at=inputs.evaluated_at,
    )


def reset_kill_switch(
    decision: KillSwitchDecision,
    *,
    reset_reason: str,
    reset_at: datetime,
) -> KillSwitchResetResult:
    """Create an explicit auditable reset event for a kill-switch decision."""
    normalized_reason = reset_reason.strip()
    if not normalized_reason:
        raise KillSwitchError("reset_reason is required")
    if _is_naive(reset_at):
        raise KillSwitchError("reset_at must be timezone-aware")
    return KillSwitchResetResult(
        previous_state=decision.state,
        new_state=KillSwitchState.GREEN,
        reset_reason=normalized_reason,
        reset_at=reset_at,
        previous_reason_codes=decision.reason_codes,
    )


def evaluate_kill_switch(state: RiskState, risk_limits: RiskLimits) -> RiskCheckResult:
    """Evaluate loss, stopped-basket, martingale, and live-order kill switches."""
    rejection_reasons: list[RiskRejectionReason] = []
    limit_amounts = calculate_risk_limit_amounts(state.account_equity, risk_limits)

    if state.weekly_realized_loss < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "weekly_realized_loss must be non-negative",
                "weekly_realized_loss",
            )
        )

    if state.monthly_drawdown < Decimal("0"):
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "monthly_drawdown must be non-negative",
                "monthly_drawdown",
            )
        )

    if state.consecutive_stopped_baskets < 0:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.INVALID_RISK_INPUT,
                "consecutive_stopped_baskets must be non-negative",
                "consecutive_stopped_baskets",
            )
        )

    if state.account_equity > Decimal("0") and state.weekly_realized_loss >= limit_amounts.max_weekly_loss:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.WEEKLY_LOSS_LIMIT_EXCEEDED,
                "weekly realized loss is at or beyond configured limit",
                "weekly_realized_loss",
            )
        )

    if state.account_equity > Decimal("0") and state.monthly_drawdown >= limit_amounts.max_monthly_drawdown:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.MONTHLY_DRAWDOWN_LIMIT_EXCEEDED,
                "monthly drawdown is at or beyond configured limit",
                "monthly_drawdown",
            )
        )

    if state.consecutive_stopped_baskets >= risk_limits.max_consecutive_stopped_baskets:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.CONSECUTIVE_STOPPED_BASKETS_EXCEEDED,
                "consecutive stopped baskets are at or beyond configured limit",
                "consecutive_stopped_baskets",
            )
        )

    if state.martingale_requested:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.MARTINGALE_FORBIDDEN,
                "martingale sizing is forbidden",
                "martingale_requested",
            )
        )

    if state.live_order_requested:
        rejection_reasons.append(
            reject(
                RiskRejectionCode.LIVE_ORDERS_FORBIDDEN,
                "live orders are forbidden",
                "live_order_requested",
            )
        )

    return RiskCheckResult.from_rejections(rejection_reasons)


def _black_reasons(inputs: KillSwitchInputs) -> list[KillSwitchReason]:
    reasons: list[KillSwitchReason] = []
    regime_state = _regime_value(inputs.current_regime)

    if inputs.data_feed_critical_failure or (
        inputs.data_quality is not None and inputs.data_quality.severity == DataQualitySeverity.CRITICAL
    ):
        reasons.append(
            _reason(
                KillSwitchReasonCode.DATA_FEED_CRITICAL_FAILURE,
                "data feed or data quality produced a critical failure",
                "data_quality",
            )
        )
    if inputs.account_equity is None or inputs.account_equity <= Decimal("0"):
        reasons.append(
            _reason(
                KillSwitchReasonCode.ACCOUNT_EQUITY_MISSING,
                "account equity is missing or invalid",
                "account_equity",
            )
        )
    if not inputs.open_positions_verified:
        reasons.append(
            _reason(
                KillSwitchReasonCode.OPEN_POSITIONS_UNVERIFIED,
                "open positions cannot be verified",
                "open_positions_verified",
            )
        )
    if not inputs.broker_account_reconciled:
        reasons.append(
            _reason(
                KillSwitchReasonCode.BROKER_ACCOUNT_RECONCILIATION_FAILED,
                "broker/account reconciliation failed",
                "broker_account_reconciled",
            )
        )
    if inputs.daily_loss_cap_breached:
        reasons.append(_reason(KillSwitchReasonCode.DAILY_LOSS_CAP_BREACHED, "daily loss cap breached", "daily_loss_cap_breached"))
    if inputs.weekly_loss_cap_breached:
        reasons.append(
            _reason(KillSwitchReasonCode.WEEKLY_LOSS_CAP_BREACHED, "weekly loss cap breached", "weekly_loss_cap_breached")
        )
    if inputs.monthly_loss_cap_breached:
        reasons.append(
            _reason(KillSwitchReasonCode.MONTHLY_LOSS_CAP_BREACHED, "monthly loss cap breached", "monthly_loss_cap_breached")
        )
    if inputs.duplicate_order_risk_detected:
        reasons.append(
            _reason(
                KillSwitchReasonCode.DUPLICATE_ORDER_RISK_DETECTED,
                "duplicate order risk detected",
                "duplicate_order_risk_detected",
            )
        )
    if inputs.wrong_way_order_risk_detected:
        reasons.append(
            _reason(
                KillSwitchReasonCode.WRONG_WAY_ORDER_RISK_DETECTED,
                "wrong-way order risk detected",
                "wrong_way_order_risk_detected",
            )
        )
    if regime_state == RegimeLabel.BLACK.value:
        reasons.append(_reason(KillSwitchReasonCode.REGIME_BLACK, "current regime is BLACK", "current_regime"))

    return reasons


def _red_reasons(inputs: KillSwitchInputs) -> list[KillSwitchReason]:
    if _regime_value(inputs.current_regime) != RegimeLabel.RED.value:
        return []
    return [_reason(KillSwitchReasonCode.RED_REGIME, "current regime is RED; no new trades allowed", "current_regime")]


def _yellow_reasons(inputs: KillSwitchInputs) -> list[KillSwitchReason]:
    if not inputs.reduced_size_required and _regime_value(inputs.current_regime) != RegimeLabel.YELLOW.value:
        return []
    return [
        _reason(
            KillSwitchReasonCode.REDUCED_SIZE_REQUIRED,
            "reduced size only while kill switch is yellow",
            "reduced_size_required",
        )
    ]


def _reason(code: KillSwitchReasonCode, message: str, field: str) -> KillSwitchReason:
    return KillSwitchReason(code=code, message=message, field=field)


def _regime_value(regime: RegimeLabel | str) -> str:
    value = regime.value if isinstance(regime, RegimeLabel) else regime.strip().upper()
    allowed_values = {label.value for label in RegimeLabel}
    if value not in allowed_values:
        raise KillSwitchError("current_regime must be a known regime state")
    return value


def _is_naive(value: datetime) -> bool:
    return value.tzinfo is None or value.utcoffset() is None
