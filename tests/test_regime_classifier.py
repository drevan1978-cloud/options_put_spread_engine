from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from options_engine.data.data_quality import DataQualityDecision, DataQualityResult, DataQualitySeverity
from options_engine.regime import (
    AccountReconciliationStatus,
    RegimeInputs,
    RegimeLabel,
    RegimeReasonCode,
    VIXTermStructureStatus,
    classify_regime_state,
)
from options_engine.storage.database import initialize_database, insert_regime_state


def test_classifies_green_regime_when_all_green_conditions_are_met() -> None:
    classification = classify_regime_state(_inputs())

    assert classification.regime == RegimeLabel.GREEN
    assert _reason_codes(classification) == {
        RegimeReasonCode.GREEN_CONDITIONS_MET,
        RegimeReasonCode.PRICE_ABOVE_50DMA,
        RegimeReasonCode.IV_ABOVE_RV,
        RegimeReasonCode.VIX_TERM_STRUCTURE_NORMAL,
        RegimeReasonCode.DATA_QUALITY_PASSED,
    }
    assert classification.details["classifier"] == "deterministic_regime_state_machine_v1"


def test_classifies_yellow_when_trend_is_weakening_above_50dma() -> None:
    classification = classify_regime_state(
        _inputs(underlying_close=Decimal("451"), moving_average_50=Decimal("450"))
    )

    assert classification.regime == RegimeLabel.YELLOW
    assert RegimeReasonCode.TREND_WEAKENING in _reason_codes(classification)


def test_classifies_yellow_when_volatility_is_elevated_above_50dma() -> None:
    classification = classify_regime_state(_inputs(implied_volatility=Decimal("0.35"), realized_volatility=Decimal("0.20")))

    assert classification.regime == RegimeLabel.YELLOW
    assert RegimeReasonCode.VOLATILITY_ELEVATED in _reason_codes(classification)


def test_classifies_red_when_price_is_below_50dma() -> None:
    classification = classify_regime_state(
        _inputs(underlying_close=Decimal("440"), moving_average_50=Decimal("450"))
    )

    assert classification.regime == RegimeLabel.RED
    assert _reason_codes(classification) == {RegimeReasonCode.PRICE_BELOW_50DMA}


def test_classifies_red_when_vix_term_structure_is_inverted() -> None:
    classification = classify_regime_state(_inputs(vix_term_structure=VIXTermStructureStatus.INVERTED))

    assert classification.regime == RegimeLabel.RED
    assert RegimeReasonCode.VIX_TERM_STRUCTURE_INVERTED in _reason_codes(classification)


def test_classifies_red_when_realized_volatility_exceeds_implied_volatility() -> None:
    classification = classify_regime_state(_inputs(implied_volatility=Decimal("0.18"), realized_volatility=Decimal("0.22")))

    assert classification.regime == RegimeLabel.RED
    assert RegimeReasonCode.REALIZED_VOL_ABOVE_IV in _reason_codes(classification)


def test_classifies_red_when_abnormal_loss_cluster_is_present() -> None:
    classification = classify_regime_state(_inputs(abnormal_loss_cluster=True))

    assert classification.regime == RegimeLabel.RED
    assert RegimeReasonCode.ABNORMAL_LOSS_CLUSTER in _reason_codes(classification)


def test_classifies_black_for_critical_data_quality_failure() -> None:
    classification = classify_regime_state(_inputs(data_quality=_data_quality_failure(DataQualitySeverity.CRITICAL)))

    assert classification.regime == RegimeLabel.BLACK
    assert RegimeReasonCode.DATA_QUALITY_CRITICAL_FAILURE in _reason_codes(classification)


def test_classifies_black_for_broker_account_reconciliation_failure() -> None:
    classification = classify_regime_state(_inputs(account_reconciliation=AccountReconciliationStatus.FAILED))

    assert classification.regime == RegimeLabel.BLACK
    assert RegimeReasonCode.BROKER_ACCOUNT_RECONCILIATION_FAILED in _reason_codes(classification)


def test_classifies_black_when_open_risk_cannot_be_verified() -> None:
    classification = classify_regime_state(_inputs(open_risk_verified=False))

    assert classification.regime == RegimeLabel.BLACK
    assert RegimeReasonCode.OPEN_RISK_NOT_VERIFIED in _reason_codes(classification)


def test_classifies_black_when_hard_loss_cap_is_breached() -> None:
    classification = classify_regime_state(_inputs(hard_loss_cap_breached=True))

    assert classification.regime == RegimeLabel.BLACK
    assert RegimeReasonCode.HARD_LOSS_CAP_BREACHED in _reason_codes(classification)


def test_regime_state_persists_to_database_with_reason_codes(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)
    classification = classify_regime_state(_inputs())
    stored_regime = classification.to_storage_model(config_version="test-config")

    with sqlite3.connect(database_path) as connection:
        inserted_id = insert_regime_state(connection, stored_regime)
        row = connection.execute(
            """
            SELECT symbol, as_of, regime, details_json, config_version
            FROM regime_states
            WHERE id = ?
            """,
            (inserted_id,),
        ).fetchone()

    details = json.loads(row[3])
    assert row[0] == "SPY"
    assert row[1] == "2026-06-20T14:00:00+00:00"
    assert row[2] == RegimeLabel.GREEN.value
    assert RegimeReasonCode.GREEN_CONDITIONS_MET.value in details["reason_codes"]
    assert row[4] == "test-config"


def test_legacy_regime_aliases_map_to_new_state_names() -> None:
    assert RegimeLabel.BULLISH == RegimeLabel.GREEN
    assert RegimeLabel.NEUTRAL == RegimeLabel.YELLOW
    assert RegimeLabel.BEARISH == RegimeLabel.RED
    assert RegimeLabel.UNKNOWN == RegimeLabel.BLACK


def _inputs(
    *,
    underlying_close: Decimal = Decimal("460"),
    moving_average_50: Decimal = Decimal("450"),
    implied_volatility: Decimal = Decimal("0.22"),
    realized_volatility: Decimal = Decimal("0.18"),
    vix_term_structure: VIXTermStructureStatus = VIXTermStructureStatus.NORMAL,
    data_quality: DataQualityResult | None = None,
    account_reconciliation: AccountReconciliationStatus = AccountReconciliationStatus.RECONCILED,
    open_risk_verified: bool = True,
    hard_loss_cap_breached: bool = False,
    abnormal_loss_cluster: bool = False,
) -> RegimeInputs:
    return RegimeInputs(
        symbol="SPY",
        as_of=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
        underlying_close=underlying_close,
        moving_average_50=moving_average_50,
        implied_volatility=implied_volatility,
        realized_volatility=realized_volatility,
        vix_term_structure=vix_term_structure,
        data_quality=data_quality or _data_quality_pass(),
        account_reconciliation=account_reconciliation,
        open_risk_verified=open_risk_verified,
        hard_loss_cap_breached=hard_loss_cap_breached,
        abnormal_loss_cluster=abnormal_loss_cluster,
    )


def _data_quality_pass() -> DataQualityResult:
    return DataQualityResult.pass_result(checked_at=datetime(2026, 6, 20, 13, 59, tzinfo=UTC))


def _data_quality_failure(severity: DataQualitySeverity) -> DataQualityResult:
    return DataQualityResult(
        decision=DataQualityDecision.NO_TRADE,
        rejection_reasons=(),
        checked_at=datetime(2026, 6, 20, 13, 59, tzinfo=UTC),
        severity=severity,
        reason_code="TEST_DATA_QUALITY_FAILURE",
        message="test data quality failure",
    )


def _reason_codes(classification: object) -> set[RegimeReasonCode]:
    return {reason.code for reason in classification.reasons}
