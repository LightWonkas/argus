"""Indicator/feature service (L2) — technical indicators.

Pure functions; the input is close/high/low series (float), the output is a
``list[float | None]`` of the same length as the input (``None`` during warm-up).
Indicators are **derived analytical values** (not money) → computed with float; prices
are Decimal in the contract and are converted to float here.
"""

from __future__ import annotations

from collections.abc import Sequence

from argus.core.contracts import OHLCVSeries


def ohlcv_floats(series: OHLCVSeries) -> tuple[list[float], list[float], list[float]]:
    """Extract (highs, lows, closes) float lists from the series."""
    highs = [float(b.high) for b in series.bars]
    lows = [float(b.low) for b in series.bars]
    closes = [float(b.close) for b in series.bars]
    return highs, lows, closes


def sma(values: Sequence[float], period: int) -> list[float | None]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    window = float(sum(values[:period]))
    out[period - 1] = window / period
    for i in range(period, n):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average (seed = SMA of the first ``period`` values)."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    mult = 2.0 / (period + 1)
    prev = float(sum(values[:period])) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (values[i] - prev) * mult + prev
        out[i] = prev
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0 and avg_loss == 0:
        # Motionless series (0/0): NOT overbought but neutral — avoids emitting a false
        # RSI-100 "overbought" trigger in a flat/frozen market (honesty).
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Relative Strength Index (Wilder smoothing). First value at index ``period``."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """True Range series."""
    n = len(highs)
    if not (len(lows) == len(closes) == n):
        raise ValueError("highs/lows/closes must have the same length")
    tr: list[float] = [0.0] * n
    if n == 0:
        return tr
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return tr


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """Average True Range (Wilder smoothing)."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(highs)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    tr = true_ranges(highs, lows, closes)
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return the MACD line, the signal line and the histogram."""
    if not (0 < fast < slow):
        raise ValueError("0 < fast < slow is required")
    n = len(values)
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)

    macd_line: list[float | None] = [None] * n
    for i in range(n):
        f, s = ema_fast[i], ema_slow[i]
        if f is not None and s is not None:
            macd_line[i] = f - s

    defined_idx: list[int] = []
    defined_vals: list[float] = []
    for i in range(n):
        m = macd_line[i]
        if m is not None:
            defined_idx.append(i)
            defined_vals.append(m)
    sig_partial = ema(defined_vals, signal)

    signal_line: list[float | None] = [None] * n
    for k, i in enumerate(defined_idx):
        signal_line[i] = sig_partial[k]

    histogram: list[float | None] = [None] * n
    for i in range(n):
        m, sg = macd_line[i], signal_line[i]
        if m is not None and sg is not None:
            histogram[i] = m - sg
    return macd_line, signal_line, histogram
