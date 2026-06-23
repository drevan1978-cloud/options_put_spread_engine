from __future__ import annotations

import importlib
from pathlib import Path


MODULES = (
    "options_engine",
    "options_engine.main",
    "options_engine.config.loader",
    "options_engine.storage.database",
    "options_engine.storage.models",
    "options_engine.data.market_data",
    "options_engine.data.option_chain",
    "options_engine.data.data_quality",
    "options_engine.regime.classifier",
    "options_engine.risk.sizing",
    "options_engine.risk.portfolio_heat",
    "options_engine.risk.kill_switch",
    "options_engine.strategy.spread_scanner",
    "options_engine.strategy.eligibility",
    "options_engine.strategy.exits",
    "options_engine.execution.ticket",
    "options_engine.execution.fill_tracker",
    "options_engine.reporting.daily_report",
    "options_engine.utils.logging",
    "options_engine.utils.time",
    "options_engine.utils.enums",
)


REQUIRED_PATHS = (
    "README.md",
    "pyproject.toml",
    ".gitignore",
    "config/default.yaml",
    "config/risk_limits.yaml",
    "config/symbols.yaml",
    "data/raw",
    "data/processed",
    "logs",
    "notebooks",
    "src/options_engine/__init__.py",
    "src/options_engine/main.py",
    "tests/test_scaffold.py",
)


def test_package_modules_import() -> None:
    for module_name in MODULES:
        importlib.import_module(module_name)


def test_requested_scaffold_paths_exist() -> None:
    project_root = Path(__file__).resolve().parents[1]

    missing = [path for path in REQUIRED_PATHS if not (project_root / path).exists()]

    assert missing == []
