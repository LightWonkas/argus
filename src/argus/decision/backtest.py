"""Minimal deterministic backtest engine — no LLM, $0, testable offline.

Replays OHLCV from the PIT store **incrementally**: each step sees only the candles up to
that moment (``bars[:i+1]`` — tests prove there is no look-ahead) and runs the same path as
the live pipeline: ``TriggerEngine → deterministic agents → SignalAggregator``. Every
proposal that is directional and passes the wake threshold counts as a **signal**.

Evaluation uses the SAME functions as self-tracking
(:func:`~argus.storage.outcome.evaluate_direction`): threshold k×ATR at signal time,
touch+close dual metric, random/bullish/momentum baselines → the live hit rate and the
backtest hit rate are measured with the same ruler (metric consistency).

Honesty notes (also written into the report):
- **Compute ≠ edge.** A negative or no-effect result is a finding, not a failure.
- Signals may **overlap** (one trend produces several triggers) → n is not the number of
  independent samples; the bootstrap CI can therefore be optimistic.
- NO parameter optimisation is done (single-config replay) — the in-sample/out-of-sample
  split is a regime-shift indicator, not an overfitting test.
- Multiple testing: one test per horizon × metric × segment combination; the
  Bonferroni-corrected threshold is reported.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argus.brain.agents import AnalysisContext, default_agents
from argus.core.contracts import OHLCVSeries
from argus.core.enums import AssetClass, Severity, Stance
from argus.decision.aggregator import SignalAggregator
from argus.platform.triggers import TriggerConfig, TriggerEngine, should_wake_brain
from argus.storage.outcome import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_K_ATR,
    Metric,
    OutcomeVerdict,
    Variant,
    evaluate_direction,
    momentum_sign,
    random_sign,
)

# Minimum candles before replay starts (indicator warm-up; EMA26+MACD signal ~35).
DEFAULT_WARMUP_BARS = 40
_BOOTSTRAP_ITERATIONS = 1000
_BOOTSTRAP_SEED = 20260611  # deterministic CI (same report on re-run)


@dataclass(frozen=True)
class BacktestSignal:
    """A single signal produced during replay (with data up to that moment)."""

    index: int  # candle where the signal was produced (bars[:index+1] was visible)
    time: datetime
    direction: int  # +1 bullish / -1 bearish
    strength: int
    confluence: int
    in_sample: bool


@dataclass(frozen=True)
class MetricCount:
    """Hit counter for one metric (definite verdicts; skips are not scored)."""

    hit: int = 0
    n: int = 0

    @property
    def pct(self) -> float | None:
        return round(self.hit / self.n * 100, 1) if self.n else None


@dataclass(frozen=True)
class HorizonSegmentStats:
    """Statistics of one (horizon, segment) cell — against the baselines."""

    horizon_bars: int
    segment: str  # "in_sample" | "out_of_sample"
    n_signals: int
    mean_ret_pct: float | None  # mean direction-signed forward return
    median_ret_pct: float | None
    ci_low_pct: float | None  # bootstrap 95% CI (on the mean)
    ci_high_pct: float | None
    touch: MetricCount = field(default_factory=MetricCount)
    close: MetricCount = field(default_factory=MetricCount)
    baselines: dict[str, dict[str, MetricCount]] = field(default_factory=dict)

    def delta_pts(self, metric: str) -> float | None:
        """System% − random-baseline% (when both sides have n>0)."""
        sys_stat = self.touch if metric == "touch" else self.close
        rnd = self.baselines.get("random", {}).get(metric, MetricCount())
        if sys_stat.pct is None or rnd.pct is None:
            return None
        return round(sys_stat.pct - rnd.pct, 1)


@dataclass(frozen=True)
class BacktestReport:
    """Full result of one run (the report is rendered by the CLI)."""

    symbol: str
    timeframe: str
    n_bars: int
    split: float
    split_index: int
    warmup_bars: int
    k_atr: float
    atr_period: int
    adjusted_series: bool  # is the series back-adjusted (Provenance.adjusted)
    signals: tuple[BacktestSignal, ...]
    stats: tuple[HorizonSegmentStats, ...]
    n_tests: int  # multiple-testing note: horizon × metric × segment
    bonferroni_alpha: float  # 0.05 / n_tests


def replay_signals(
    series: OHLCVSeries,
    *,
    split: float = 0.7,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    min_severity: Severity = Severity.MEDIUM,
    asset_class: AssetClass | None = None,
    trigger_config: TriggerConfig | None = None,
) -> list[BacktestSignal]:
    """Replays the series incrementally and produces signals — **no look-ahead**.

    Each step sees only ``bars[:i+1]``; the deterministic path of the live pipeline runs
    (trigger → agents → aggregator). Steps that are directional (BULLISH/BEARISH) and pass
    the wake threshold are signals. No LLM → $0, deterministic.
    """
    engine = TriggerEngine(trigger_config) if trigger_config is not None else TriggerEngine()
    aggregator = SignalAggregator(default_agents())
    bars = series.bars
    split_index = int(len(bars) * split)
    signals: list[BacktestSignal] = []

    for i in range(warmup_bars, len(bars)):
        visible = series.model_copy(update={"bars": bars[: i + 1]})
        trigger_signals = engine.evaluate(visible)
        if not should_wake_brain(trigger_signals, min_severity=min_severity):
            continue
        ctx = AnalysisContext(
            symbol=series.symbol,
            timeframe=series.timeframe,
            series=visible,
            signals=tuple(trigger_signals),
            asset_class=asset_class,
        )
        proposal = aggregator.aggregate(ctx)
        if proposal.direction not in (Stance.BULLISH, Stance.BEARISH):
            continue
        signals.append(
            BacktestSignal(
                index=i,
                time=bars[i].timestamp,
                direction=1 if proposal.direction is Stance.BULLISH else -1,
                strength=proposal.strength,
                confluence=proposal.confluence_score,
                in_sample=i < split_index,
            )
        )
    return signals


def _signed_forward_return(
    series: OHLCVSeries, *, index: int, horizon: int, direction: int
) -> float | None:
    """Signed forward return %: (close[i+h]/close[i] − 1) × direction; None past the data."""
    bars = series.bars
    if index + horizon >= len(bars):
        return None
    entry = float(bars[index].close)
    exit_ = float(bars[index + horizon].close)
    if entry <= 0:
        return None
    return (exit_ - entry) / entry * 100 * direction


def _bootstrap_ci(values: list[float], *, seed: int) -> tuple[float, float] | None:
    """95% bootstrap CI for the mean (deterministic seed). n<3 → no CI possible (None)."""
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choices(values, k=len(values))) for _ in range(_BOOTSTRAP_ITERATIONS)
    )
    low = means[round(0.025 * (len(means) - 1))]
    high = means[round(0.975 * (len(means) - 1))]
    return round(low, 2), round(high, 2)


def _count_verdict(counts: dict[str, int], verdict: OutcomeVerdict) -> None:
    if verdict is OutcomeVerdict.CORRECT:
        counts["hit"] += 1
        counts["n"] += 1
    elif verdict is OutcomeVerdict.INCORRECT:
        counts["n"] += 1
    # SKIP/TOO_EARLY are not scored (same rule as self-tracking)


def evaluate_signals(
    series: OHLCVSeries,
    signals: list[BacktestSignal],
    *,
    horizons: tuple[int, ...] = (1, 5, 20),
    k_atr: float = DEFAULT_K_ATR,
    atr_period: int = DEFAULT_ATR_PERIOD,
) -> list[HorizonSegmentStats]:
    """Evaluates signals with the self-tracking ruler (touch/close, k×ATR, baselines)."""
    tf_seconds = series.timeframe.seconds
    far_future = series.bars[-1].timestamp + timedelta(days=1) if series.bars else None
    stats: list[HorizonSegmentStats] = []

    for horizon in horizons:
        horizon_td = timedelta(seconds=tf_seconds * horizon)
        for segment, seg_signals in (
            ("in_sample", [s for s in signals if s.in_sample]),
            ("out_of_sample", [s for s in signals if not s.in_sample]),
        ):
            returns: list[float] = []
            sys_counts = {m: {"hit": 0, "n": 0} for m in ("touch", "close")}
            base_counts = {
                v.value: {m: {"hit": 0, "n": 0} for m in ("touch", "close")}
                for v in (Variant.RANDOM, Variant.BULLISH, Variant.MOMENTUM)
            }
            for sig in seg_signals:
                ret = _signed_forward_return(
                    series, index=sig.index, horizon=horizon, direction=sig.direction
                )
                if ret is not None:
                    returns.append(ret)
                if far_future is None:
                    continue
                for metric in (Metric.TOUCH, Metric.CLOSE):
                    result = evaluate_direction(
                        sig.direction,
                        sig.time,
                        series,
                        now=far_future,
                        horizon=horizon_td,
                        metric=metric,
                        k_atr=k_atr,
                        atr_period=atr_period,
                    )
                    _count_verdict(sys_counts[metric.value], result.verdict)
                    # Baselines — compete at the same moment/horizon (self-tracking rule).
                    for variant, sign in (
                        (Variant.RANDOM, random_sign(sig.index)),
                        (Variant.BULLISH, 1),
                        (Variant.MOMENTUM, momentum_sign(series, sig.time)),
                    ):
                        b = evaluate_direction(
                            sign,
                            sig.time,
                            series,
                            now=far_future,
                            horizon=horizon_td,
                            metric=metric,
                            k_atr=k_atr,
                            atr_period=atr_period,
                        )
                        _count_verdict(base_counts[variant.value][metric.value], b.verdict)

            ci = _bootstrap_ci(returns, seed=_BOOTSTRAP_SEED + horizon)
            stats.append(
                HorizonSegmentStats(
                    horizon_bars=horizon,
                    segment=segment,
                    n_signals=len(seg_signals),
                    mean_ret_pct=round(statistics.fmean(returns), 2) if returns else None,
                    median_ret_pct=round(statistics.median(returns), 2) if returns else None,
                    ci_low_pct=ci[0] if ci else None,
                    ci_high_pct=ci[1] if ci else None,
                    touch=MetricCount(**sys_counts["touch"]),
                    close=MetricCount(**sys_counts["close"]),
                    baselines={
                        variant: {m: MetricCount(**c) for m, c in metrics.items()}
                        for variant, metrics in base_counts.items()
                    },
                )
            )
    return stats


def run_backtest(
    series: OHLCVSeries,
    *,
    split: float = 0.7,
    horizons: tuple[int, ...] = (1, 5, 20),
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    k_atr: float = DEFAULT_K_ATR,
    atr_period: int = DEFAULT_ATR_PERIOD,
    min_severity: Severity = Severity.MEDIUM,
    asset_class: AssetClass | None = None,
    trigger_config: TriggerConfig | None = None,
) -> BacktestReport:
    """One-call end-to-end run: replay → evaluation → report data."""
    if not 0 < split < 1:
        raise ValueError("split must be in the range (0,1)")
    signals = replay_signals(
        series,
        split=split,
        warmup_bars=warmup_bars,
        min_severity=min_severity,
        asset_class=asset_class,
        trigger_config=trigger_config,
    )
    stats = evaluate_signals(series, signals, horizons=horizons, k_atr=k_atr, atr_period=atr_period)
    n_tests = len(horizons) * 2 * 2  # horizon × metric(touch/close) × segment(IS/OOS)
    return BacktestReport(
        symbol=series.symbol,
        timeframe=series.timeframe.value,
        n_bars=len(series.bars),
        split=split,
        split_index=int(len(series.bars) * split),
        warmup_bars=warmup_bars,
        k_atr=k_atr,
        atr_period=atr_period,
        adjusted_series=series.provenance.adjusted,
        signals=tuple(signals),
        stats=tuple(stats),
        n_tests=n_tests,
        bonferroni_alpha=round(0.05 / n_tests, 5),
    )
