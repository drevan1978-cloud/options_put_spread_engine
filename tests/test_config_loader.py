from __future__ import annotations

import json
import logging
from pathlib import Path
from textwrap import dedent

import pytest
from _pytest.logging import LogCaptureFixture
from pydantic import ValidationError

from options_engine.config.loader import ConfigLoadError, build_config_change, load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_loads_valid_project_config_and_logs_hash(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="options_engine.config")

    config = load_config(PROJECT_ROOT / "config")

    assert config.symbols.underlyings == ("SPX", "SPY", "QQQ")
    assert config.strategy.min_dte == 30
    assert config.strategy.max_dte == 45
    assert config.risk_limits.allow_martingale is False
    assert config.risk_limits.allow_live_orders is False
    assert len(config.config_version) == 12
    assert len(config.config_hash) == 64
    assert any(config.config_hash in record.getMessage() for record in caplog.records)


def test_missing_config_file_fails_loudly(tmp_path: Path) -> None:
    _write_default_config(tmp_path)
    _write_symbols_config(tmp_path)

    with pytest.raises(ConfigLoadError, match="required config file missing"):
        load_config(tmp_path)


def test_live_orders_enabled_fails_validation(tmp_path: Path) -> None:
    _write_default_config(tmp_path)
    _write_symbols_config(tmp_path)
    _write_risk_limits_config(tmp_path, allow_live_orders=True)

    with pytest.raises(ValidationError, match="allow_live_orders must be false"):
        load_config(tmp_path)


def test_invalid_symbols_fail_validation(tmp_path: Path) -> None:
    _write_default_config(tmp_path)
    _write_risk_limits_config(tmp_path)
    _write_symbols_config(tmp_path, symbols=("SPX", "SPY", "AAPL"))

    with pytest.raises(ValidationError, match="symbols must contain exactly"):
        load_config(tmp_path)


def test_build_config_change_creates_storage_model() -> None:
    config = load_config(PROJECT_ROOT / "config")

    change = build_config_change(
        previous_config=None,
        current_config=config,
        changed_by="pytest",
        summary="initial config load",
    )

    assert change.config_version == config.config_version
    assert change.before_json == "null"
    assert json.loads(change.after_json)["config_hash"] == config.config_hash


def _write_default_config(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "default.yaml").write_text(
        dedent(
            """
            project:
              name: options_put_spread_engine
              environment: test
              live_trading_enabled: false
            storage:
              engine: sqlite
              path: data/processed/test.sqlite
            logging:
              directory: logs
              level: INFO
            strategy:
              min_dte: 30
              max_dte: 45
              min_short_delta_abs: 0.10
              max_short_delta_abs: 0.25
              min_credit_to_width: 0.30
              max_bid_ask_width_pct: 0.15
            """
        ).strip(),
        encoding="utf-8",
    )


def _write_risk_limits_config(
    config_dir: Path,
    *,
    allow_live_orders: bool = False,
    allow_martingale: bool = False,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "risk_limits.yaml").write_text(
        dedent(
            f"""
            risk_limits:
              max_risk_per_trade_cluster_pct: 0.01
              max_risk_per_expiration_pct: 0.03
              max_total_portfolio_heat_pct: 0.06
              max_weekly_loss_pct: 0.02
              max_monthly_drawdown_pct: 0.05
              max_consecutive_stopped_baskets: 2
              allow_martingale: {str(allow_martingale).lower()}
              allow_live_orders: {str(allow_live_orders).lower()}
            """
        ).strip(),
        encoding="utf-8",
    )


def _write_symbols_config(config_dir: Path, *, symbols: tuple[str, ...] = ("SPX", "SPY", "QQQ")) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    symbol_lines = "\n".join(f"  - {symbol}" for symbol in symbols)
    (config_dir / "symbols.yaml").write_text(f"symbols:\n{symbol_lines}\n", encoding="utf-8")
