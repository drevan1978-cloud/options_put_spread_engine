"""Configuration loading, validation, and versioning."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from options_engine.storage.models import ConfigChange

LOGGER_NAME = "options_engine.config"
REQUIRED_SYMBOLS = ("SPX", "SPY", "QQQ")


class ConfigLoadError(RuntimeError):
    """Raised when required configuration files cannot be loaded."""


class ProjectConfig(BaseModel):
    """Project-level configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    live_trading_enabled: bool

    @model_validator(mode="after")
    def live_trading_must_be_disabled(self) -> ProjectConfig:
        """Reject any config that enables live trading."""
        if self.live_trading_enabled:
            raise ValueError("live_trading_enabled must be false")
        return self


class StorageConfig(BaseModel):
    """Local durable storage configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: Literal["sqlite", "duckdb"]
    path: Path


class LoggingConfig(BaseModel):
    """Logging configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @field_validator("level", mode="before")
    @classmethod
    def normalize_level(cls, value: object) -> str:
        """Normalize log levels to uppercase names."""
        return str(value).upper()


class StrategyDefaults(BaseModel):
    """Static strategy configuration values, not strategy logic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_dte: int = Field(gt=0)
    max_dte: int = Field(gt=0)
    min_short_delta_abs: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    max_short_delta_abs: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    min_credit_to_width: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_bid_ask_width_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))

    @model_validator(mode="after")
    def ranges_must_be_ordered(self) -> StrategyDefaults:
        """Require min/max ranges to be internally consistent."""
        if self.max_dte < self.min_dte:
            raise ValueError("max_dte must be greater than or equal to min_dte")
        if self.max_short_delta_abs < self.min_short_delta_abs:
            raise ValueError("max_short_delta_abs must be greater than or equal to min_short_delta_abs")
        return self


class RiskLimits(BaseModel):
    """Validated risk-limit configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_risk_per_trade_cluster_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_risk_per_expiration_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_total_portfolio_heat_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_weekly_loss_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_monthly_drawdown_pct: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_consecutive_stopped_baskets: int = Field(ge=0)
    allow_martingale: bool
    allow_live_orders: bool

    @model_validator(mode="after")
    def forbidden_controls_must_be_disabled(self) -> RiskLimits:
        """Reject forbidden v1 controls."""
        if self.allow_martingale:
            raise ValueError("allow_martingale must be false")
        if self.allow_live_orders:
            raise ValueError("allow_live_orders must be false")
        return self


class SymbolsConfig(BaseModel):
    """Supported v1 underlyings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    underlyings: tuple[str, ...]

    @field_validator("underlyings", mode="before")
    @classmethod
    def normalize_underlyings(cls, value: object) -> tuple[str, ...]:
        """Normalize and validate the v1 symbol universe."""
        if not isinstance(value, list | tuple):
            raise ValueError("symbols must be a list")

        symbols = tuple(str(symbol).upper() for symbol in value)
        if len(symbols) != len(set(symbols)):
            raise ValueError("symbols must not contain duplicates")
        if set(symbols) != set(REQUIRED_SYMBOLS):
            raise ValueError("symbols must contain exactly SPX, SPY, and QQQ")
        return symbols


class EngineConfigDraft(BaseModel):
    """Validated configuration before version metadata is attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: ProjectConfig
    storage: StorageConfig
    logging: LoggingConfig
    strategy: StrategyDefaults
    risk_limits: RiskLimits
    symbols: SymbolsConfig


class EngineConfig(EngineConfigDraft):
    """Fully validated configuration with deterministic version metadata."""

    config_version: str = Field(min_length=12, max_length=12)
    config_hash: str = Field(min_length=64, max_length=64)


def load_config(config_dir: Path, logger: logging.Logger | None = None) -> EngineConfig:
    """Load, validate, version, and log the engine configuration."""
    raw_config = load_config_files(config_dir)
    config = build_engine_config(raw_config)
    active_logger = logger or logging.getLogger(LOGGER_NAME)
    active_logger.info(
        "config_loaded version=%s hash=%s",
        config.config_version,
        config.config_hash,
        extra={"config_version": config.config_version, "config_hash": config.config_hash},
    )
    return config


def load_config_files(config_dir: Path) -> dict[str, Any]:
    """Load the three required config files into a raw config mapping."""
    default_config = _read_yaml_mapping(config_dir / "default.yaml")
    risk_config = _read_yaml_mapping(config_dir / "risk_limits.yaml")
    symbols_config = _read_yaml_mapping(config_dir / "symbols.yaml")

    try:
        risk_limits = risk_config["risk_limits"]
        symbols = symbols_config["symbols"]
    except KeyError as exc:
        raise ConfigLoadError(f"missing required config section: {exc.args[0]}") from exc

    return {
        "project": _required_section(default_config, "project"),
        "storage": _required_section(default_config, "storage"),
        "logging": _required_section(default_config, "logging"),
        "strategy": _required_section(default_config, "strategy"),
        "risk_limits": risk_limits,
        "symbols": _normalize_symbols_section(symbols),
    }


def build_engine_config(raw_config: dict[str, Any]) -> EngineConfig:
    """Validate raw config data and attach deterministic hash metadata."""
    draft = EngineConfigDraft.model_validate(raw_config)
    config_hash = hash_config(draft)
    return EngineConfig.model_validate(
        {
            **draft.model_dump(mode="python"),
            "config_version": config_hash[:12],
            "config_hash": config_hash,
        }
    )


def hash_config(config: EngineConfigDraft) -> str:
    """Return a SHA-256 hash for a validated config payload."""
    canonical_payload = canonical_config_json(config)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def canonical_config_json(config: BaseModel) -> str:
    """Return canonical JSON for deterministic config hashing and audit records."""
    return json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def build_config_change(
    previous_config: EngineConfig | None,
    current_config: EngineConfig,
    changed_by: str,
    summary: str,
    changed_at: datetime | None = None,
) -> ConfigChange:
    """Build a storage model representing a config change event."""
    if not changed_by:
        raise ValueError("changed_by is required")
    if not summary:
        raise ValueError("summary is required")

    return ConfigChange(
        config_version=current_config.config_version,
        changed_at=changed_at or datetime.now(UTC),
        changed_by=changed_by,
        summary=summary,
        before_json="null" if previous_config is None else canonical_config_json(previous_config),
        after_json=canonical_config_json(current_config),
    )


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigLoadError(f"required config file missing: {path}")

    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file)

    if not isinstance(loaded, dict):
        raise ConfigLoadError(f"config file must contain a mapping: {path}")
    return loaded


def _required_section(config: dict[str, Any], section_name: str) -> Any:
    try:
        return config[section_name]
    except KeyError as exc:
        raise ConfigLoadError(f"missing required config section: {section_name}") from exc


def _normalize_symbols_section(symbols: Any) -> dict[str, Any]:
    if isinstance(symbols, dict):
        return symbols
    return {"underlyings": symbols}


__all__ = [
    "ConfigLoadError",
    "EngineConfig",
    "EngineConfigDraft",
    "LoggingConfig",
    "ProjectConfig",
    "RiskLimits",
    "StorageConfig",
    "StrategyDefaults",
    "SymbolsConfig",
    "ValidationError",
    "build_config_change",
    "build_engine_config",
    "canonical_config_json",
    "hash_config",
    "load_config",
    "load_config_files",
]
