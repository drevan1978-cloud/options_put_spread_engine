from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from options_engine.config.loader import RiskLimits, load_config
from options_engine.risk import (
    RiskDecision,
    RiskRejectionCode,
    RiskSizingError,
    calculate_allowed_trade_risk,
    calculate_contracts,
    calculate_max_loss_per_spread,
    validate_no_martingale,
    validate_risk_limits,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_calculates_sizing_math() -> None:
    max_loss = calculate_max_loss_per_spread(width=Decimal("5"), credit=Decimal("1.60"), multiplier=100)
    allowed_risk = calculate_allowed_trade_risk(
        account_equity=Decimal("100000"),
        risk_pct=Decimal("0.01"),
        regime_multiplier=Decimal("0.50"),
    )
    contracts = calculate_contracts(allowed_risk=allowed_risk, max_loss_per_spread=max_loss)

    assert max_loss == Decimal("340.00")
    assert allowed_risk == Decimal("500.00000")
    assert contracts == 1


def test_calculate_max_loss_rejects_zero_or_negative_values() -> None:
    with pytest.raises(RiskSizingError) as width_error:
        calculate_max_loss_per_spread(width=Decimal("0"), credit=Decimal("1.00"), multiplier=100)
    with pytest.raises(RiskSizingError) as max_loss_error:
        calculate_max_loss_per_spread(width=Decimal("5"), credit=Decimal("5"), multiplier=100)

    assert width_error.value.reason.code == RiskRejectionCode.INVALID_RISK_INPUT
    assert max_loss_error.value.reason.code == RiskRejectionCode.INVALID_RISK_INPUT


def test_allowed_trade_risk_rejects_missing_account_equity() -> None:
    with pytest.raises(RiskSizingError) as error:
        calculate_allowed_trade_risk(
            account_equity=None,
            risk_pct=Decimal("0.01"),
            regime_multiplier=Decimal("1"),
        )

    assert error.value.reason.code == RiskRejectionCode.INVALID_ACCOUNT_EQUITY


def test_allowed_trade_risk_rejects_invalid_risk_pct_and_regime_multiplier() -> None:
    with pytest.raises(RiskSizingError) as risk_pct_error:
        calculate_allowed_trade_risk(
            account_equity=Decimal("100000"),
            risk_pct=Decimal("0"),
            regime_multiplier=Decimal("1"),
        )
    with pytest.raises(RiskSizingError) as multiplier_error:
        calculate_allowed_trade_risk(
            account_equity=Decimal("100000"),
            risk_pct=Decimal("0.01"),
            regime_multiplier=Decimal("1.25"),
        )

    assert risk_pct_error.value.reason.code == RiskRejectionCode.INVALID_RISK_INPUT
    assert multiplier_error.value.reason.code == RiskRejectionCode.INVALID_RISK_INPUT


def test_calculate_contracts_rejects_contracts_less_than_one() -> None:
    with pytest.raises(RiskSizingError) as error:
        calculate_contracts(allowed_risk=Decimal("100"), max_loss_per_spread=Decimal("340"))

    assert error.value.reason.code == RiskRejectionCode.CONTRACTS_LESS_THAN_ONE


def test_calculate_contracts_rejects_non_positive_max_loss() -> None:
    with pytest.raises(RiskSizingError) as error:
        calculate_contracts(allowed_risk=Decimal("100"), max_loss_per_spread=Decimal("0"))

    assert error.value.reason.code == RiskRejectionCode.INVALID_RISK_INPUT


def test_validate_no_martingale_passes_when_disabled() -> None:
    result = validate_no_martingale(_risk_limits())

    assert result.decision == RiskDecision.PASS
    assert result.rejection_reasons == ()


def test_validate_no_martingale_rejects_when_enabled() -> None:
    result = validate_no_martingale(SimpleNamespace(allow_martingale=True))

    assert result.decision == RiskDecision.NO_TRADE
    assert _codes(result) == {RiskRejectionCode.MARTINGALE_FORBIDDEN}


def test_validate_risk_limits_rejects_invalid_percentages() -> None:
    invalid_config = SimpleNamespace(
        max_risk_per_trade_cluster_pct=Decimal("1.25"),
        max_risk_per_expiration_pct=Decimal("0.03"),
        max_total_portfolio_heat_pct=Decimal("0.06"),
        max_weekly_loss_pct=Decimal("0.02"),
        max_monthly_drawdown_pct=Decimal("0.05"),
        max_consecutive_stopped_baskets=2,
        allow_martingale=False,
        allow_live_orders=False,
    )

    result = validate_risk_limits(invalid_config)

    assert result.decision == RiskDecision.NO_TRADE
    assert RiskRejectionCode.INVALID_RISK_INPUT in _codes(result)


def test_validate_risk_limits_rejects_size_increase_during_drawdown() -> None:
    config = SimpleNamespace(
        risk_limits=_risk_limits(),
        drawdown_active=True,
        current_risk_pct=Decimal("0.005"),
        requested_risk_pct=Decimal("0.010"),
    )

    result = validate_risk_limits(config)

    assert result.decision == RiskDecision.NO_TRADE
    assert _codes(result) == {RiskRejectionCode.SIZE_INCREASE_DURING_DRAWDOWN}


def _risk_limits() -> RiskLimits:
    return load_config(PROJECT_ROOT / "config").risk_limits


def _codes(result: object) -> set[RiskRejectionCode]:
    return {reason.code for reason in result.rejection_reasons}
