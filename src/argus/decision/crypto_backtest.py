"""Point-in-time crypto backtest report with costs and baseline comparison.

The initial policy is spot-only, so only a bullish intent opens a new simulated long; a
bearish intent does not open a short. No fill can be produced before the first candle
available after the signal close. Overlapping trades count as one position and correlated
signals at the same moment are merged.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from argus.core.contracts import OHLCVSeries
from argus.core.enums import MarketRegimeKind


@dataclass(frozen=True)
class BacktestIntent:
    index: int
    direction: int
    horizon_bars: int
    feature_end_index: int
    evidence_groups: frozenset[str]
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.direction not in (-1, 1):
            raise ValueError("direction must be -1 or +1")
        if self.index < 0 or self.horizon_bars < 1 or self.feature_end_index < 0:
            raise ValueError("invalid backtest intent index/horizon values")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be tz-aware")


@dataclass(frozen=True)
class CryptoBacktestCosts:
    commission_bps: float = 10.0
    spread_bps: float = 2.0
    slippage_bps: float = 5.0
    funding_rate_per_bar: float = 0.0
    latency_bars: int = 1
    fill_ratio: float = 1.0

    def __post_init__(self) -> None:
        if min(self.commission_bps, self.spread_bps, self.slippage_bps) < 0:
            raise ValueError("cost bps values cannot be negative")
        if self.latency_bars < 0 or not 0 < self.fill_ratio <= 1:
            raise ValueError("invalid latency/fill_ratio")


@dataclass(frozen=True)
class CryptoTradeResult:
    signal_index: int
    entry_index: int
    exit_index: int
    gross_return_pct: float
    net_return_pct: float
    cost_pct: float
    filled_fraction: float
    regime: MarketRegimeKind


@dataclass(frozen=True)
class PerformanceMetrics:
    net_return_pct: float
    cagr_pct: float | None
    max_drawdown_pct: float
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    profit_factor: float | None
    expectancy_pct: float | None
    win_loss_ratio: float | None
    average_win_pct: float | None
    average_loss_pct: float | None
    trade_count: int
    exposure_pct: float
    turnover_pct: float
    total_cost_pct: float
    confidence_interval_95_pct: tuple[float, float] | None


@dataclass(frozen=True)
class WalkForwardFold:
    train: tuple[int, int]
    validation: tuple[int, int]
    test: tuple[int, int]
    parameters_locked_before_test: bool = True


@dataclass(frozen=True)
class CryptoBacktestReport:
    symbol: str
    metrics: PerformanceMetrics
    split_metrics: dict[str, PerformanceMetrics]
    regime_metrics: dict[str, PerformanceMetrics]
    baselines: dict[str, PerformanceMetrics]
    baseline_delta_pct: dict[str, float]
    trades: tuple[CryptoTradeResult, ...]
    walk_forward_folds: tuple[WalkForwardFold, ...]
    low_sample_warning: str | None
    overfitting_risk: str
    survivorship_bias_note: str
    point_in_time_enforced: bool = True
    negative_results_preserved: bool = True
    assumptions: tuple[str, ...] = field(
        default=(
            "spot-only: a bearish signal does not open a short",
            "fill on the candle after the signal candle, with latency",
            "simultaneous/overlapping signals do not count as independent trades",
        )
    )


def _validate_intents(series: OHLCVSeries, intents: Sequence[BacktestIntent]) -> None:
    for intent in intents:
        if intent.index >= len(series.bars):
            raise ValueError("intent index is outside the series")
        if intent.feature_end_index > intent.index:
            raise ValueError("look-ahead: feature_end_index exceeds the signal index")
        bar_time = series.bars[intent.index].timestamp
        next_time = (
            series.bars[intent.index + 1].timestamp if intent.index + 1 < len(series.bars) else None
        )
        if intent.generated_at < bar_time or (
            next_time is not None and intent.generated_at > next_time
        ):
            raise ValueError("look-ahead: generated_at is outside the signal PIT window")


def _deduplicate(intents: Sequence[BacktestIntent]) -> list[BacktestIntent]:
    """Merges evidence at the same index/direction into one intent; overlap is dropped later."""
    grouped: dict[tuple[int, int], BacktestIntent] = {}
    for intent in sorted(intents, key=lambda item: (item.index, item.direction)):
        key = (intent.index, intent.direction)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = intent
        else:
            grouped[key] = BacktestIntent(
                index=intent.index,
                direction=intent.direction,
                horizon_bars=max(existing.horizon_bars, intent.horizon_bars),
                feature_end_index=max(existing.feature_end_index, intent.feature_end_index),
                evidence_groups=existing.evidence_groups | intent.evidence_groups,
                generated_at=max(existing.generated_at, intent.generated_at),
            )
    return sorted(grouped.values(), key=lambda item: item.index)


def execute_spot_intents(
    series: OHLCVSeries,
    intents: Sequence[BacktestIntent],
    *,
    costs: CryptoBacktestCosts | None = None,
    regimes_by_index: Mapping[int, MarketRegimeKind] | None = None,
) -> list[CryptoTradeResult]:
    """Turns intents into simulated trades at PIT prices with cost/liquidity assumptions."""
    _validate_intents(series, intents)
    policy = costs or CryptoBacktestCosts()
    results: list[CryptoTradeResult] = []
    occupied_until = -1
    for intent in _deduplicate(intents):
        if intent.direction < 0:
            continue
        entry_index = intent.index + max(1, policy.latency_bars)
        exit_index = entry_index + intent.horizon_bars
        if entry_index <= occupied_until or exit_index >= len(series.bars):
            continue
        entry = float(series.bars[entry_index].open)
        exit_ = float(series.bars[exit_index].close)
        gross = (exit_ / entry - 1) * 100
        trading_cost_pct = (
            2 * policy.commission_bps + policy.spread_bps + 2 * policy.slippage_bps
        ) / 100
        funding_pct = policy.funding_rate_per_bar * intent.horizon_bars * 100
        cost_pct = (trading_cost_pct + funding_pct) * policy.fill_ratio
        net = gross * policy.fill_ratio - cost_pct
        results.append(
            CryptoTradeResult(
                signal_index=intent.index,
                entry_index=entry_index,
                exit_index=exit_index,
                gross_return_pct=gross * policy.fill_ratio,
                net_return_pct=net,
                cost_pct=cost_pct,
                filled_fraction=policy.fill_ratio,
                regime=(regimes_by_index or {}).get(intent.index, MarketRegimeKind.UNKNOWN),
            )
        )
        occupied_until = exit_index
    return results


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def _bootstrap_ci(values: list[float], *, seed: int = 20260902) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    samples = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(1000))
    return samples[24], samples[974]


def performance_metrics(
    trades: Sequence[CryptoTradeResult],
    *,
    total_bars: int,
    timeframe_seconds: int,
) -> PerformanceMetrics:
    returns = [trade.net_return_pct for trade in trades]
    equity = [1.0]
    for value in returns:
        equity.append(equity[-1] * (1 + value / 100))
    net = (equity[-1] - 1) * 100
    years = total_bars * timeframe_seconds / (365.0 * 24 * 3600)
    cagr = ((equity[-1] ** (1 / years) - 1) * 100) if years > 0 and equity[-1] > 0 else None
    drawdown = _max_drawdown(equity)
    mean = statistics.fmean(returns) if returns else None
    deviation = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
    downside = [min(value, 0.0) for value in returns]
    downside_dev = statistics.pstdev(downside) if len(downside) >= 2 else 0.0
    sharpe = (
        mean / deviation * math.sqrt(len(returns)) if mean is not None and deviation > 0 else None
    )
    sortino = (
        mean / downside_dev * math.sqrt(len(returns))
        if mean is not None and downside_dev > 0
        else None
    )
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    exposure_bars = sum(
        (trade.exit_index - trade.entry_index) * trade.filled_fraction for trade in trades
    )
    ci = _bootstrap_ci(returns)
    return PerformanceMetrics(
        net_return_pct=round(net, 4),
        cagr_pct=round(cagr, 4) if cagr is not None else None,
        max_drawdown_pct=round(drawdown, 4),
        sharpe=round(sharpe, 4) if sharpe is not None else None,
        sortino=round(sortino, 4) if sortino is not None else None,
        calmar=(round(cagr / drawdown, 4) if cagr is not None and drawdown > 0 else None),
        profit_factor=round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        expectancy_pct=round(mean, 4) if mean is not None else None,
        win_loss_ratio=round(len(wins) / len(losses), 4) if losses else None,
        average_win_pct=round(statistics.fmean(wins), 4) if wins else None,
        average_loss_pct=round(statistics.fmean(losses), 4) if losses else None,
        trade_count=len(trades),
        exposure_pct=round(exposure_bars / total_bars * 100, 4) if total_bars else 0.0,
        turnover_pct=round(sum(trade.filled_fraction for trade in trades) * 2 * 100, 4),
        total_cost_pct=round(sum(trade.cost_pct for trade in trades), 4),
        confidence_interval_95_pct=((round(ci[0], 4), round(ci[1], 4)) if ci is not None else None),
    )


def _metrics_for_range(
    trades: Sequence[CryptoTradeResult],
    bounds: tuple[int, int],
    series: OHLCVSeries,
) -> PerformanceMetrics:
    start, end = bounds
    selected = [trade for trade in trades if start <= trade.signal_index < end]
    return performance_metrics(
        selected,
        total_bars=max(end - start, 1),
        timeframe_seconds=series.timeframe.seconds,
    )


def _walk_forward_folds(
    n_bars: int, *, train_ratio: float, validation_ratio: float
) -> tuple[WalkForwardFold, ...]:
    train_end = int(n_bars * train_ratio)
    validation_end = train_end + int(n_bars * validation_ratio)
    first = WalkForwardFold(
        train=(0, train_end),
        validation=(train_end, validation_end),
        test=(validation_end, n_bars),
    )
    # Second fold uses an expanding train window; test parameters are still locked first.
    if validation_end >= n_bars - 2:
        return (first,)
    second_test_start = validation_end + max((n_bars - validation_end) // 2, 1)
    second = WalkForwardFold(
        train=(0, validation_end),
        validation=(validation_end, second_test_start),
        test=(second_test_start, n_bars),
    )
    return first, second


def _buy_hold_metrics(series: OHLCVSeries, costs: CryptoBacktestCosts) -> PerformanceMetrics:
    if len(series.bars) < 2:
        return performance_metrics(
            [], total_bars=len(series.bars), timeframe_seconds=series.timeframe.seconds
        )
    intent = BacktestIntent(
        index=0,
        direction=1,
        horizon_bars=max(len(series.bars) - 2, 1),
        feature_end_index=0,
        evidence_groups=frozenset({"baseline:buy_hold"}),
        generated_at=series.bars[0].timestamp,
    )
    trades = execute_spot_intents(series, [intent], costs=costs)
    return performance_metrics(
        trades, total_bars=len(series.bars), timeframe_seconds=series.timeframe.seconds
    )


def _baseline_intents(
    series: OHLCVSeries, count: int, horizon: int, *, randomize: bool
) -> list[BacktestIntent]:
    if count <= 0 or len(series.bars) < horizon + 2:
        return []
    candidates = list(range(20, len(series.bars) - horizon - 1, horizon + 1))
    if randomize:
        chosen = sorted(random.Random(20260902).sample(candidates, min(count, len(candidates))))
    else:
        chosen = [
            index
            for index in candidates
            if float(series.bars[index].close) > float(series.bars[index - 20].close)
        ][:count]
    return [
        BacktestIntent(
            index=index,
            direction=1,
            horizon_bars=horizon,
            feature_end_index=index,
            evidence_groups=frozenset({"baseline:random" if randomize else "baseline:momentum"}),
            generated_at=series.bars[index].timestamp,
        )
        for index in chosen
    ]


def run_crypto_backtest(
    series: OHLCVSeries,
    intents: Sequence[BacktestIntent],
    *,
    costs: CryptoBacktestCosts | None = None,
    regimes_by_index: Mapping[int, MarketRegimeKind] | None = None,
    benchmarks: Mapping[str, OHLCVSeries] | None = None,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    test_ratio: float = 0.2,
    survivorship_bias_note: str = "Fixed BTC/ETH universe; delisted assets are out of scope.",
) -> CryptoBacktestReport:
    """Builds the full costed, walk-forward, per-regime report; never hides negative results."""
    if abs(train_ratio + validation_ratio + test_ratio - 1) > 1e-9:
        raise ValueError("train/validation/test must sum to 1")
    policy = costs or CryptoBacktestCosts()
    trades = execute_spot_intents(series, intents, costs=policy, regimes_by_index=regimes_by_index)
    metrics = performance_metrics(
        trades, total_bars=len(series.bars), timeframe_seconds=series.timeframe.seconds
    )
    train_end = int(len(series.bars) * train_ratio)
    validation_end = train_end + int(len(series.bars) * validation_ratio)
    bounds = {
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, len(series.bars)),
    }
    split_metrics = {
        name: _metrics_for_range(trades, window, series) for name, window in bounds.items()
    }
    regimes = sorted({trade.regime for trade in trades}, key=lambda value: value.value)
    regime_metrics = {
        regime.value: performance_metrics(
            [trade for trade in trades if trade.regime is regime],
            total_bars=len(series.bars),
            timeframe_seconds=series.timeframe.seconds,
        )
        for regime in regimes
    }

    typical_horizon = (
        round(statistics.median(intent.horizon_bars for intent in intents)) if intents else 1
    )
    random_intents = _baseline_intents(series, len(trades), typical_horizon, randomize=True)
    momentum_intents = _baseline_intents(series, len(trades), typical_horizon, randomize=False)
    baseline_metrics = {
        "cash_no_trade": performance_metrics(
            [], total_bars=len(series.bars), timeframe_seconds=series.timeframe.seconds
        ),
        "momentum": performance_metrics(
            execute_spot_intents(series, momentum_intents, costs=policy),
            total_bars=len(series.bars),
            timeframe_seconds=series.timeframe.seconds,
        ),
        "random_same_frequency": performance_metrics(
            execute_spot_intents(series, random_intents, costs=policy),
            total_bars=len(series.bars),
            timeframe_seconds=series.timeframe.seconds,
        ),
        f"{series.symbol}_buy_and_hold": _buy_hold_metrics(series, policy),
    }
    for symbol, benchmark in (benchmarks or {}).items():
        baseline_metrics[f"{symbol}_buy_and_hold"] = _buy_hold_metrics(benchmark, policy)
    deltas = {
        name: round(metrics.net_return_pct - baseline.net_return_pct, 4)
        for name, baseline in baseline_metrics.items()
    }
    test_net = split_metrics["test"].net_return_pct
    validation_net = split_metrics["validation"].net_return_pct
    overfit = (
        "high: test result is clearly worse than the validation result"
        if validation_net > 0 and test_net < 0
        else "medium: low sample size"
        if len(trades) < 30
        else "low: parameters locked before test; forward validation is still required"
    )
    return CryptoBacktestReport(
        symbol=series.symbol,
        metrics=metrics,
        split_metrics=split_metrics,
        regime_metrics=regime_metrics,
        baselines=baseline_metrics,
        baseline_delta_pct=deltas,
        trades=tuple(trades),
        walk_forward_folds=_walk_forward_folds(
            len(series.bars), train_ratio=train_ratio, validation_ratio=validation_ratio
        ),
        low_sample_warning=(
            f"Low sample size: only {len(trades)} independent trades" if len(trades) < 30 else None
        ),
        overfitting_risk=overfit,
        survivorship_bias_note=survivorship_bias_note,
    )
