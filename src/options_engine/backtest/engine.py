"""Simple offline backtest engine with explicit cost and fill assumptions."""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from statistics import mean, pstdev
from typing import Final

SUPPORTED_SYMBOLS: Final[frozenset[str]] = frozenset({"SPX", "SPY", "QQQ"})
WATCHLIST_STATUSES: Final[frozenset[str]] = frozenset({"WATCHLIST", "ELIGIBLE_FOR_REVIEW"})
APPROVED_ELIGIBILITY_STATUSES: Final[frozenset[str]] = frozenset({"APPROVED"})
ALLOWED_REGIMES: Final[frozenset[str]] = frozenset({"GREEN", "YELLOW"})
ALLOWED_KILL_SWITCH_STATES: Final[frozenset[str]] = frozenset({"GREEN", "YELLOW"})
REQUIRED_BACKTEST_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "trade_id",
        "entry_date",
        "exit_date",
        "symbol",
        "contracts",
        "width",
        "entry_mid_credit",
        "exit_mid_debit",
        "regime_state",
        "data_quality_passed",
        "kill_switch_state",
        "risk_approved",
        "scanner_status",
        "eligibility_status",
        "fill_available",
    }
)


class BacktestError(ValueError):
    """Raised when a backtest input or assumption is unsafe."""


class BacktestMode(StrEnum):
    """Supported deterministic backtest modes."""

    NO_FILTER_BASELINE = "no_filter_baseline"
    SIMPLE_REGIME_FILTER = "simple_regime_filter"
    FULL_MVP_RULES = "full_mvp_rules"


class FillModel(StrEnum):
    """Supported fill assumptions."""

    CONSERVATIVE = "conservative"
    MID_ONLY = "mid_only"


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Backtest assumptions that are fixed per run."""

    mode: BacktestMode
    starting_equity: Decimal = Decimal("100000")
    multiplier: Decimal = Decimal("100")
    slippage_per_spread: Decimal = Decimal("0.05")
    commission_per_contract: Decimal = Decimal("1.30")
    fill_model: FillModel = FillModel.CONSERVATIVE
    random_seed: int = 17

    def __post_init__(self) -> None:
        if self.starting_equity <= Decimal("0"):
            raise BacktestError("starting_equity must be positive")
        if self.multiplier <= Decimal("0"):
            raise BacktestError("multiplier must be positive")
        if self.slippage_per_spread < Decimal("0"):
            raise BacktestError("slippage_per_spread must be non-negative")
        if self.commission_per_contract < Decimal("0"):
            raise BacktestError("commission_per_contract must be non-negative")
        if self.fill_model == FillModel.MID_ONLY:
            raise BacktestError("fantasy mid-only fills are not allowed")
        if self.slippage_per_spread == Decimal("0") and self.commission_per_contract == Decimal("0"):
            raise BacktestError("backtests must include slippage or commission costs")


@dataclass(frozen=True, slots=True)
class HistoricalTrade:
    """One historical put-spread trade sample for offline backtesting."""

    trade_id: str
    entry_date: date
    exit_date: date
    symbol: str
    contracts: int
    width: Decimal
    entry_mid_credit: Decimal
    exit_mid_debit: Decimal
    regime_state: str
    data_quality_passed: bool
    kill_switch_state: str
    risk_approved: bool
    scanner_status: str
    eligibility_status: str
    fill_available: bool = True

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if normalized_symbol not in SUPPORTED_SYMBOLS:
            raise BacktestError("backtest only supports SPX, SPY, and QQQ in v1")
        if not self.trade_id.strip():
            raise BacktestError("trade_id is required")
        if self.exit_date < self.entry_date:
            raise BacktestError("exit_date must be on or after entry_date")
        if self.contracts <= 0:
            raise BacktestError("contracts must be positive")
        if self.width <= Decimal("0"):
            raise BacktestError("width must be positive")
        if self.entry_mid_credit <= Decimal("0"):
            raise BacktestError("entry_mid_credit must be positive")
        if self.exit_mid_debit < Decimal("0"):
            raise BacktestError("exit_mid_debit must be non-negative")
        if self.entry_mid_credit >= self.width:
            raise BacktestError("entry_mid_credit must be below spread width")
        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "regime_state", self.regime_state.strip().upper())
        object.__setattr__(self, "kill_switch_state", self.kill_switch_state.strip().upper())
        object.__setattr__(self, "scanner_status", self.scanner_status.strip().upper())
        object.__setattr__(self, "eligibility_status", self.eligibility_status.strip().upper())


@dataclass(frozen=True, slots=True)
class TradeBacktestResult:
    """Executed historical trade result after costs."""

    trade_id: str
    entry_date: date
    exit_date: date
    symbol: str
    net_pnl: Decimal
    gross_mid_pnl: Decimal
    slippage_drag: Decimal
    commission_drag: Decimal
    return_on_equity: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Required performance and cost metrics for a backtest run."""

    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    mar: float
    max_drawdown: float
    average_drawdown: float
    recovery_time_days: int
    win_rate: float
    profit_factor: float
    expectancy: Decimal
    average_win: Decimal
    average_loss: Decimal
    largest_loss: Decimal
    slippage_drag: Decimal
    commission_drag: Decimal
    fill_failure_rate: float

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe metric values."""
        return {
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "mar": self.mar,
            "max_drawdown": self.max_drawdown,
            "average_drawdown": self.average_drawdown,
            "recovery_time_days": self.recovery_time_days,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy": str(self.expectancy),
            "average_win": str(self.average_win),
            "average_loss": str(self.average_loss),
            "largest_loss": str(self.largest_loss),
            "slippage_drag": str(self.slippage_drag),
            "commission_drag": str(self.commission_drag),
            "fill_failure_rate": self.fill_failure_rate,
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Complete backtest result including fixed stress scenarios."""

    mode: BacktestMode
    starting_equity: Decimal
    ending_equity: Decimal
    selected_trades: int
    executed_trades: int
    fill_failures: int
    trade_results: tuple[TradeBacktestResult, ...]
    metrics: BacktestMetrics
    stress_tests: dict[str, BacktestMetrics]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe result payload."""
        return {
            "mode": self.mode.value,
            "starting_equity": str(self.starting_equity),
            "ending_equity": str(self.ending_equity),
            "selected_trades": self.selected_trades,
            "executed_trades": self.executed_trades,
            "fill_failures": self.fill_failures,
            "metrics": self.metrics.to_dict(),
            "stress_tests": {name: metrics.to_dict() for name, metrics in self.stress_tests.items()},
        }


def run_backtest(
    trades: list[HistoricalTrade],
    config: BacktestConfig,
    *,
    include_stress_tests: bool = True,
) -> BacktestResult:
    """Run a deterministic historical backtest with explicit costs."""
    if not trades:
        raise BacktestError("backtest requires at least one historical trade")

    selected_trades = [trade for trade in _sort_trades(trades) if _passes_mode_filter(trade, config.mode)]
    executed_trades = [trade for trade in selected_trades if trade.fill_available]
    fill_failures = len(selected_trades) - len(executed_trades)
    trade_results = tuple(_execute_trade(trade, config) for trade in executed_trades)
    ending_equity = config.starting_equity + sum((result.net_pnl for result in trade_results), Decimal("0"))
    metrics = _calculate_metrics(
        trade_results=trade_results,
        starting_equity=config.starting_equity,
        ending_equity=ending_equity,
        fill_failures=fill_failures,
        selected_trades=len(selected_trades),
        first_entry_date=min(trade.entry_date for trade in trades),
    )
    stress_tests = _run_stress_tests(trades, config, trade_results) if include_stress_tests else {}

    return BacktestResult(
        mode=config.mode,
        starting_equity=config.starting_equity,
        ending_equity=ending_equity,
        selected_trades=len(selected_trades),
        executed_trades=len(trade_results),
        fill_failures=fill_failures,
        trade_results=trade_results,
        metrics=metrics,
        stress_tests=stress_tests,
    )


def load_historical_trades_csv(csv_path: Path) -> list[HistoricalTrade]:
    """Load historical trade samples from a local CSV file."""
    if not csv_path.exists():
        raise BacktestError(f"CSV file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        _validate_columns(reader.fieldnames)
        trades = [_parse_trade_row(_normalize_row(row), row_number=index + 2) for index, row in enumerate(reader)]

    if not trades:
        raise BacktestError(f"CSV file contains no historical trades: {csv_path}")
    return trades


def _run_stress_tests(
    trades: list[HistoricalTrade],
    config: BacktestConfig,
    base_trade_results: tuple[TradeBacktestResult, ...],
) -> dict[str, BacktestMetrics]:
    return {
        "doubled_slippage": run_backtest(
            trades,
            replace(config, slippage_per_spread=config.slippage_per_spread * Decimal("2")),
            include_stress_tests=False,
        ).metrics,
        "tripled_slippage": run_backtest(
            trades,
            replace(config, slippage_per_spread=config.slippage_per_spread * Decimal("3")),
            include_stress_tests=False,
        ).metrics,
        "remove_best_5_percent_trades": run_backtest(
            _remove_best_trade_samples(trades, base_trade_results),
            config,
            include_stress_tests=False,
        ).metrics,
        "randomized_entry_dates": run_backtest(
            _randomize_entry_dates(trades, config.random_seed),
            config,
            include_stress_tests=False,
        ).metrics,
        "parameter_sensitivity": run_backtest(
            trades,
            replace(
                config,
                slippage_per_spread=config.slippage_per_spread * Decimal("1.5"),
                commission_per_contract=config.commission_per_contract * Decimal("1.5"),
            ),
            include_stress_tests=False,
        ).metrics,
    }


def _execute_trade(trade: HistoricalTrade, config: BacktestConfig) -> TradeBacktestResult:
    entry_fill_credit = trade.entry_mid_credit - config.slippage_per_spread
    exit_fill_debit = trade.exit_mid_debit + config.slippage_per_spread
    if entry_fill_credit <= Decimal("0"):
        raise BacktestError(f"trade {trade.trade_id}: slippage exceeds entry credit")

    gross_mid_pnl = (trade.entry_mid_credit - trade.exit_mid_debit) * config.multiplier * Decimal(trade.contracts)
    slippage_drag = config.slippage_per_spread * Decimal("2") * config.multiplier * Decimal(trade.contracts)
    commission_drag = config.commission_per_contract * Decimal("4") * Decimal(trade.contracts)
    net_pnl = (entry_fill_credit - exit_fill_debit) * config.multiplier * Decimal(trade.contracts) - commission_drag
    return TradeBacktestResult(
        trade_id=trade.trade_id,
        entry_date=trade.entry_date,
        exit_date=trade.exit_date,
        symbol=trade.symbol,
        net_pnl=net_pnl,
        gross_mid_pnl=gross_mid_pnl,
        slippage_drag=slippage_drag,
        commission_drag=commission_drag,
        return_on_equity=float(net_pnl / config.starting_equity),
    )


def _calculate_metrics(
    *,
    trade_results: tuple[TradeBacktestResult, ...],
    starting_equity: Decimal,
    ending_equity: Decimal,
    fill_failures: int,
    selected_trades: int,
    first_entry_date: date,
) -> BacktestMetrics:
    total_return = float((ending_equity - starting_equity) / starting_equity)
    annualized_return = _annualized_return(starting_equity, ending_equity, first_entry_date, trade_results)
    returns = [result.return_on_equity for result in trade_results]
    sharpe = _sharpe(returns)
    sortino = _sortino(returns)
    max_drawdown, average_drawdown, recovery_time_days = _drawdown_metrics(starting_equity, first_entry_date, trade_results)
    wins = [result.net_pnl for result in trade_results if result.net_pnl > Decimal("0")]
    losses = [result.net_pnl for result in trade_results if result.net_pnl < Decimal("0")]
    win_total = sum(wins, Decimal("0"))
    loss_total = sum(losses, Decimal("0"))
    expectancy = _average([result.net_pnl for result in trade_results])
    profit_factor = float(win_total / abs(loss_total)) if loss_total < Decimal("0") else 0.0
    return BacktestMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        sharpe=sharpe,
        sortino=sortino,
        mar=annualized_return / max_drawdown if max_drawdown > 0 else 0.0,
        max_drawdown=max_drawdown,
        average_drawdown=average_drawdown,
        recovery_time_days=recovery_time_days,
        win_rate=len(wins) / len(trade_results) if trade_results else 0.0,
        profit_factor=profit_factor,
        expectancy=expectancy,
        average_win=_average(wins),
        average_loss=_average(losses),
        largest_loss=min(losses) if losses else Decimal("0"),
        slippage_drag=sum((result.slippage_drag for result in trade_results), Decimal("0")),
        commission_drag=sum((result.commission_drag for result in trade_results), Decimal("0")),
        fill_failure_rate=fill_failures / selected_trades if selected_trades else 0.0,
    )


def _annualized_return(
    starting_equity: Decimal,
    ending_equity: Decimal,
    first_entry_date: date,
    trade_results: tuple[TradeBacktestResult, ...],
) -> float:
    if not trade_results or ending_equity <= Decimal("0"):
        return 0.0
    elapsed_days = max((max(result.exit_date for result in trade_results) - first_entry_date).days, 1)
    return float((ending_equity / starting_equity) ** (Decimal("365") / Decimal(elapsed_days)) - Decimal("1"))


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    standard_deviation = pstdev(returns)
    if standard_deviation == 0:
        return 0.0
    return mean(returns) / standard_deviation * math.sqrt(252)


def _sortino(returns: list[float]) -> float:
    downside_returns = [value for value in returns if value < 0]
    if len(downside_returns) < 2:
        return 0.0
    downside_deviation = pstdev(downside_returns)
    if downside_deviation == 0:
        return 0.0
    return mean(returns) / downside_deviation * math.sqrt(252)


def _drawdown_metrics(
    starting_equity: Decimal,
    first_entry_date: date,
    trade_results: tuple[TradeBacktestResult, ...],
) -> tuple[float, float, int]:
    equity = starting_equity
    peak = starting_equity
    peak_date = first_entry_date
    drawdown_start: date | None = None
    drawdowns: list[float] = []
    max_recovery_days = 0

    for result in sorted(trade_results, key=lambda item: (item.exit_date, item.trade_id)):
        equity += result.net_pnl
        if equity >= peak:
            if drawdown_start is not None:
                max_recovery_days = max(max_recovery_days, (result.exit_date - drawdown_start).days)
                drawdown_start = None
            peak = equity
            peak_date = result.exit_date
            drawdowns.append(0.0)
            continue

        if drawdown_start is None:
            drawdown_start = peak_date
        drawdowns.append(float(abs((equity - peak) / peak)))

    if drawdown_start is not None and trade_results:
        max_recovery_days = max(max_recovery_days, (trade_results[-1].exit_date - drawdown_start).days)

    nonzero_drawdowns = [value for value in drawdowns if value > 0]
    max_drawdown = max(nonzero_drawdowns, default=0.0)
    average_drawdown = mean(nonzero_drawdowns) if nonzero_drawdowns else 0.0
    return max_drawdown, average_drawdown, max_recovery_days


def _passes_mode_filter(trade: HistoricalTrade, mode: BacktestMode) -> bool:
    if mode == BacktestMode.NO_FILTER_BASELINE:
        return True
    if mode == BacktestMode.SIMPLE_REGIME_FILTER:
        return trade.regime_state in ALLOWED_REGIMES
    if mode == BacktestMode.FULL_MVP_RULES:
        return (
            trade.regime_state in ALLOWED_REGIMES
            and trade.data_quality_passed
            and trade.kill_switch_state in ALLOWED_KILL_SWITCH_STATES
            and trade.risk_approved
            and trade.scanner_status in WATCHLIST_STATUSES
            and trade.eligibility_status in APPROVED_ELIGIBILITY_STATUSES
        )
    raise BacktestError(f"unsupported backtest mode: {mode}")


def _remove_best_trade_samples(
    trades: list[HistoricalTrade],
    base_trade_results: tuple[TradeBacktestResult, ...],
) -> list[HistoricalTrade]:
    if not base_trade_results:
        return trades
    remove_count = max(1, math.ceil(len(base_trade_results) * 0.05))
    removed_ids = {
        result.trade_id
        for result in sorted(base_trade_results, key=lambda item: item.net_pnl, reverse=True)[:remove_count]
    }
    remaining = [trade for trade in trades if trade.trade_id not in removed_ids]
    return remaining if remaining else trades


def _randomize_entry_dates(trades: list[HistoricalTrade], random_seed: int) -> list[HistoricalTrade]:
    shuffled_entry_dates = [trade.entry_date for trade in trades]
    random.Random(random_seed).shuffle(shuffled_entry_dates)
    randomized: list[HistoricalTrade] = []
    for trade, entry_date in zip(trades, shuffled_entry_dates, strict=True):
        holding_period = trade.exit_date - trade.entry_date
        randomized.append(replace(trade, entry_date=entry_date, exit_date=entry_date + holding_period))
    return randomized


def _sort_trades(trades: list[HistoricalTrade]) -> list[HistoricalTrade]:
    return sorted(trades, key=lambda trade: (trade.entry_date, trade.exit_date, trade.trade_id))


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _validate_columns(fieldnames: list[str] | None) -> None:
    if fieldnames is None:
        raise BacktestError("CSV file is empty")
    normalized = {field.strip() for field in fieldnames}
    missing = REQUIRED_BACKTEST_COLUMNS.difference(normalized)
    if missing:
        raise BacktestError(f"CSV missing required columns: {', '.join(sorted(missing))}")


def _parse_trade_row(row: dict[str, str], row_number: int) -> HistoricalTrade:
    return HistoricalTrade(
        trade_id=_required_text(row, "trade_id", row_number),
        entry_date=_parse_date(_required_text(row, "entry_date", row_number), "entry_date", row_number),
        exit_date=_parse_date(_required_text(row, "exit_date", row_number), "exit_date", row_number),
        symbol=_required_text(row, "symbol", row_number),
        contracts=_parse_int(row, "contracts", row_number),
        width=_parse_decimal(row, "width", row_number),
        entry_mid_credit=_parse_decimal(row, "entry_mid_credit", row_number),
        exit_mid_debit=_parse_decimal(row, "exit_mid_debit", row_number),
        regime_state=_required_text(row, "regime_state", row_number),
        data_quality_passed=_parse_bool(row, "data_quality_passed", row_number),
        kill_switch_state=_required_text(row, "kill_switch_state", row_number),
        risk_approved=_parse_bool(row, "risk_approved", row_number),
        scanner_status=_required_text(row, "scanner_status", row_number),
        eligibility_status=_required_text(row, "eligibility_status", row_number),
        fill_available=_parse_bool(row, "fill_available", row_number),
    )


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {column.strip(): value for column, value in row.items() if column is not None}


def _required_text(row: dict[str, str], column: str, row_number: int) -> str:
    value = row.get(column)
    if value is None or value.strip() == "":
        raise BacktestError(f"row {row_number}: {column} is required")
    return value.strip()


def _parse_date(raw_value: str, column: str, row_number: int) -> date:
    try:
        return date.fromisoformat(raw_value)
    except ValueError as exc:
        raise BacktestError(f"row {row_number}: {column} is malformed") from exc


def _parse_int(row: dict[str, str], column: str, row_number: int) -> int:
    value = _required_text(row, column, row_number)
    try:
        return int(value)
    except ValueError as exc:
        raise BacktestError(f"row {row_number}: {column} is malformed") from exc


def _parse_decimal(row: dict[str, str], column: str, row_number: int) -> Decimal:
    value = _required_text(row, column, row_number)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise BacktestError(f"row {row_number}: {column} is malformed") from exc


def _parse_bool(row: dict[str, str], column: str, row_number: int) -> bool:
    value = _required_text(row, column, row_number).lower()
    if value in {"true", "1", "yes", "y"}:
        return True
    if value in {"false", "0", "no", "n"}:
        return False
    raise BacktestError(f"row {row_number}: {column} must be true or false")
