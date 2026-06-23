from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from options_engine.backtest import (
    BacktestConfig,
    BacktestError,
    BacktestMode,
    FillModel,
    load_historical_trades_csv,
    run_backtest,
)


def test_backtest_runs_on_sample_historical_csv_with_costs(tmp_path: Path) -> None:
    csv_path = _sample_csv(tmp_path)
    trades = load_historical_trades_csv(csv_path)
    config = BacktestConfig(
        mode=BacktestMode.FULL_MVP_RULES,
        slippage_per_spread=Decimal("0.05"),
        commission_per_contract=Decimal("1.30"),
    )

    result = run_backtest(trades, config)

    assert result.selected_trades == 3
    assert result.executed_trades == 2
    assert result.fill_failures == 1
    assert result.metrics.slippage_drag == Decimal("20.00")
    assert result.metrics.commission_drag == Decimal("10.40")
    assert result.metrics.fill_failure_rate == pytest.approx(1 / 3)
    assert result.metrics.largest_loss < Decimal("0")
    assert result.ending_equity == Decimal("99999.60")
    assert _metric_keys().issubset(result.metrics.to_dict())
    assert _stress_keys().issubset(result.stress_tests)


def test_backtest_modes_apply_filters(tmp_path: Path) -> None:
    trades = load_historical_trades_csv(_sample_csv(tmp_path))

    baseline = run_backtest(
        trades,
        BacktestConfig(mode=BacktestMode.NO_FILTER_BASELINE),
        include_stress_tests=False,
    )
    regime = run_backtest(
        trades,
        BacktestConfig(mode=BacktestMode.SIMPLE_REGIME_FILTER),
        include_stress_tests=False,
    )
    full = run_backtest(
        trades,
        BacktestConfig(mode=BacktestMode.FULL_MVP_RULES),
        include_stress_tests=False,
    )

    assert baseline.selected_trades == 5
    assert baseline.executed_trades == 4
    assert regime.selected_trades == 4
    assert regime.executed_trades == 3
    assert full.selected_trades == 3
    assert full.executed_trades == 2


def test_backtest_stress_tests_include_required_scenarios(tmp_path: Path) -> None:
    trades = load_historical_trades_csv(_sample_csv(tmp_path))
    result = run_backtest(trades, BacktestConfig(mode=BacktestMode.FULL_MVP_RULES))

    assert set(result.stress_tests) == _stress_keys()
    assert result.stress_tests["doubled_slippage"].slippage_drag > result.metrics.slippage_drag
    assert result.stress_tests["tripled_slippage"].slippage_drag > result.stress_tests["doubled_slippage"].slippage_drag
    assert result.stress_tests["remove_best_5_percent_trades"].total_return < result.metrics.total_return
    assert result.stress_tests["parameter_sensitivity"].commission_drag > result.metrics.commission_drag


def test_backtest_fails_for_fantasy_fills_only() -> None:
    with pytest.raises(BacktestError, match="fantasy mid-only fills are not allowed"):
        BacktestConfig(mode=BacktestMode.FULL_MVP_RULES, fill_model=FillModel.MID_ONLY)

    with pytest.raises(BacktestError, match="must include slippage or commission costs"):
        BacktestConfig(
            mode=BacktestMode.FULL_MVP_RULES,
            slippage_per_spread=Decimal("0"),
            commission_per_contract=Decimal("0"),
        )


def test_backtest_rejects_bad_csv_missing_required_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad_backtest.csv"
    csv_path.write_text("trade_id,entry_date\nT1,2026-01-02\n", encoding="utf-8")

    with pytest.raises(BacktestError, match="CSV missing required columns"):
        load_historical_trades_csv(csv_path)


def _sample_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "historical_trades.csv"
    csv_path.write_text(
        "\n".join(
            [
                "trade_id,entry_date,exit_date,symbol,contracts,width,entry_mid_credit,exit_mid_debit,regime_state,data_quality_passed,kill_switch_state,risk_approved,scanner_status,eligibility_status,fill_available",
                "T1,2026-01-02,2026-01-20,SPY,1,5,1.50,0.50,GREEN,true,GREEN,true,WATCHLIST,APPROVED,true",
                "T2,2026-02-01,2026-02-20,SPY,1,5,1.50,2.20,GREEN,true,GREEN,true,WATCHLIST,APPROVED,true",
                "T3,2026-03-01,2026-03-20,QQQ,1,5,1.20,0.20,RED,true,GREEN,true,WATCHLIST,APPROVED,true",
                "T4,2026-04-01,2026-04-20,SPX,1,5,2.50,1.00,YELLOW,false,YELLOW,true,WATCHLIST,APPROVED,true",
                "T5,2026-05-01,2026-05-20,SPY,1,5,1.60,0.80,GREEN,true,GREEN,true,WATCHLIST,APPROVED,false",
            ]
        ),
        encoding="utf-8",
    )
    return csv_path


def _metric_keys() -> set[str]:
    return {
        "total_return",
        "annualized_return",
        "sharpe",
        "sortino",
        "mar",
        "max_drawdown",
        "average_drawdown",
        "recovery_time_days",
        "win_rate",
        "profit_factor",
        "expectancy",
        "average_win",
        "average_loss",
        "largest_loss",
        "slippage_drag",
        "commission_drag",
        "fill_failure_rate",
    }


def _stress_keys() -> set[str]:
    return {
        "doubled_slippage",
        "tripled_slippage",
        "remove_best_5_percent_trades",
        "randomized_entry_dates",
        "parameter_sensitivity",
    }
