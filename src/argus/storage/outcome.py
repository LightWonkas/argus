"""Automatic outcome evaluation v2 — ATR-normalized threshold, two metrics, baselines.

An analysis/proposal carries a direction (``bias``/``direction``); the price at analysis time
is **derived from PIT OHLCV** (close of the nearest candle ≤ ``created_at``). The significance
threshold is not a fixed percentage but **volatility-normalized**:
``k_atr × ATR@created_at`` (the ATR is PIT too — only from candles up to the analysis time).
Two metrics are measured:

- **touch**: did price touch the threshold in the bias direction **at least once** within
  the horizon (using high/low).
- **close**: is the horizon-end close beyond the threshold (inside → undecided, unscored).

"Hit rate X%" is meaningless on its own: every system verdict is compared, **on the same
symbol/time/horizon**, with three baselines — ``random`` (seeded with the analysis id →
**deterministic**, identical on re-runs), ``bullish`` (always up), ``momentum`` (previous
candle direction).

Honesty rules:

- **No direction claim, no score** → ``SKIP``.
- **No threshold without a warmed-up ATR** → ``SKIP`` (NO invented threshold).
- **Wait until the horizon closes** → ``TOO_EARLY`` (retried; unlike SKIP the verdict
  can change with time).
- **Close inside the threshold = undecided, not a miss** → ``SKIP`` (only clear moves
  are scored).

Pure functions (no network, deterministic); :class:`~argus.storage.evaluator.OutcomeEvaluator`
uses them and calls ``record_outcome``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.platform.indicators import atr as atr_series

# Bias/direction string → direction sign (+1 up / -1 down). Everything else: none (0).
_BIAS_SIGN: dict[str, int] = {"bullish": 1, "bearish": -1}

# Schema defaults (the real values come from the ``outcome`` block of config/config.yaml).
DEFAULT_HORIZON_BARS = 5
DEFAULT_K_ATR = 1.0
DEFAULT_ATR_PERIOD = 14
# Max candles taken before created_at for ATR warm-up (window pinning: improves
# determinism — the same window is used even when a longer history is loaded).
_ATR_WINDOW_FACTOR = 3


class Metric(StrEnum):
    """Hit-rate metric: a touch within the horizon, or the horizon-end close."""

    TOUCH = "touch"
    CLOSE = "close"


class Variant(StrEnum):
    """Source of the evaluated direction: the system, or a baseline."""

    SYSTEM = "system"
    RANDOM = "random"  # coin flip seeded with the analysis id (deterministic)
    BULLISH = "bullish"  # naive baseline that always says up
    MOMENTUM = "momentum"  # baseline repeating the previous candle direction


class OutcomeVerdict(StrEnum):
    """The result of one evaluation."""

    CORRECT = "correct"  # direction confirmed (beyond threshold)
    INCORRECT = "incorrect"  # direction refuted
    SKIP = "skip"  # no direction / no ATR / inside threshold → unscored (verdict is FINAL)
    TOO_EARLY = "too_early"  # horizon/data not complete (retry later)


@dataclass(frozen=True)
class OutcomeResult:
    """Evaluation verdict + human-readable note (written via record_outcome)."""

    verdict: OutcomeVerdict
    note: str
    change_pct: float | None = None  # % change from the base price (extreme/close per metric)


def bias_sign(bias: str | None) -> int:
    """Converts bias/direction text to a direction sign (+1/-1; directionless → 0)."""
    return _BIAS_SIGN.get((bias or "").strip().lower(), 0)


# ── Baseline direction generators (race the system on the same symbol/time/horizon) ───


def random_sign(ref_id: int) -> int:
    """Deterministic coin flip: same id → same direction on every run (reproducible)."""
    return random.Random(ref_id).choice((1, -1))


def momentum_sign(series: OHLCVSeries, at: datetime) -> int:
    """Previous candle direction: body direction of the last completed candle at ``at``.

    If the body is flat (open == close), use the previous close; if that is equal too, 0
    (directionless → SKIP; no direction is invented).
    """
    prev: OHLCVBar | None = None
    last: OHLCVBar | None = None
    for bar in series.bars:  # bars are ordered oldest→newest
        if bar.timestamp <= at:
            prev, last = last, bar
        else:
            break
    if last is None:
        return 0
    if last.close != last.open:
        return 1 if last.close > last.open else -1
    if prev is not None and last.close != prev.close:
        return 1 if last.close > prev.close else -1
    return 0


# ── PIT helpers ──────────────────────────────────────────────────────────────


def pit_atr(series: OHLCVSeries, at: datetime, *, period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """ATR at analysis time — **only** from candles up to ``at`` (no look-ahead).

    The window is pinned to the last ``period × 3`` candles (same result even when a longer
    history is loaded). With fewer than ``period`` candles the ATR is not warmed up → ``None``
    (no invented threshold).
    """
    pit_bars = [b for b in series.bars if b.timestamp <= at][-period * _ATR_WINDOW_FACTOR :]
    if len(pit_bars) < period:
        return None
    values = atr_series(
        [float(b.high) for b in pit_bars],
        [float(b.low) for b in pit_bars],
        [float(b.close) for b in pit_bars],
        period,
    )
    last = values[-1]
    return float(last) if last is not None else None


def _price_at_or_before(series: OHLCVSeries, when: datetime) -> Decimal | None:
    """Close of the **last** candle at/before ``when`` (PIT base price); None if absent."""
    result: Decimal | None = None
    for bar in series.bars:  # bars are ordered oldest→newest (guaranteed by the Data Quality Gate)
        if bar.timestamp <= when:
            result = bar.close
        else:
            break
    return result


def _bar_at_or_after(series: OHLCVSeries, when: datetime) -> OHLCVBar | None:
    """First candle at/after ``when``; None if absent (no data yet)."""
    for bar in series.bars:
        if bar.timestamp >= when:
            return bar
    return None


# ── Core evaluation (direction + metric → verdict) ───────────────────────────


def evaluate_direction(
    sign: int,
    created_at: datetime,
    series: OHLCVSeries,
    *,
    now: datetime,
    horizon: timedelta,
    metric: Metric,
    k_atr: float = DEFAULT_K_ATR,
    atr_period: int = DEFAULT_ATR_PERIOD,
) -> OutcomeResult:
    """Evaluates a direction claim (system or baseline) against the PIT series.

    Both metrics decide **after the horizon closes** (a candle at/after the target time is
    needed to confirm the horizon) → idempotent and deterministic: same input, same verdict.
    """
    if sign == 0:
        return OutcomeResult(OutcomeVerdict.SKIP, "no direction claim (neutral/unclear) → unscored")

    base = _price_at_or_before(series, created_at)
    if base is None or base <= 0:
        return OutcomeResult(OutcomeVerdict.TOO_EARLY, "no base price data at analysis time")

    atr_value = pit_atr(series, created_at, period=atr_period)
    if atr_value is None or atr_value <= 0:
        return OutcomeResult(
            OutcomeVerdict.SKIP, f"ATR not warmed up (<{atr_period} candles) → unscored"
        )
    threshold = k_atr * atr_value

    target_time = created_at + horizon
    if now < target_time:
        return OutcomeResult(
            OutcomeVerdict.TOO_EARLY, f"horizon not reached (target {target_time:%Y-%m-%d %H:%M})"
        )
    eval_bar = _bar_at_or_after(series, target_time)
    if eval_bar is None:
        return OutcomeResult(OutcomeVerdict.TOO_EARLY, "no price data after the horizon")

    base_f = float(base)
    direction = "up" if sign > 0 else "down"
    thr_pct = threshold / base_f * 100

    if metric is Metric.TOUCH:
        # Horizon window: candles after created_at up to the target time (using extremes).
        window = [b for b in series.bars if created_at < b.timestamp <= target_time]
        if sign > 0:
            extreme = max((float(b.high) for b in window), default=base_f)
            touched = extreme >= base_f + threshold
        else:
            extreme = min((float(b.low) for b in window), default=base_f)
            touched = extreme <= base_f - threshold
        change_pct = (extreme - base_f) / base_f * 100
        if touched:
            return OutcomeResult(
                OutcomeVerdict.CORRECT,
                f"touch: price reached the {direction} threshold "
                f"(extreme {change_pct:+.2f}%, threshold {thr_pct:.2f}%=k·ATR)",
                change_pct,
            )
        return OutcomeResult(
            OutcomeVerdict.INCORRECT,
            f"no touch: the {direction} threshold was not reached within the horizon "
            f"(extreme {change_pct:+.2f}%, threshold {thr_pct:.2f}%=k·ATR)",
            change_pct,
        )

    # Metric.CLOSE — is the horizon-end close beyond the threshold.
    eval_close = float(eval_bar.close)
    move = eval_close - base_f
    change_pct = move / base_f * 100
    if sign * move >= threshold:
        return OutcomeResult(
            OutcomeVerdict.CORRECT,
            f"close confirmed: {change_pct:+.2f}% {direction} (threshold {thr_pct:.2f}%=k·ATR)",
            change_pct,
        )
    if sign * move <= -threshold:
        return OutcomeResult(
            OutcomeVerdict.INCORRECT,
            f"close against: {change_pct:+.2f}% (threshold {thr_pct:.2f}%=k·ATR)",
            change_pct,
        )
    return OutcomeResult(
        OutcomeVerdict.SKIP,
        f"close inside threshold ({change_pct:+.2f}%, ±{thr_pct:.2f}%) → undecided, unscored",
        change_pct,
    )
