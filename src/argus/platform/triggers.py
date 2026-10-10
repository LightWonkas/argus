"""Trigger engine (L2/L6) — computes technical triggers **locally** (no LLM).

Cheap monitoring triggers are computed locally; only a trigger of sufficient severity
wakes the brain. The engine is pure/side-effect free: it evaluates an OHLCV series and
returns a list of :class:`TriggerSignal`. Cooldown/dedup/routing and event publishing
belong to the L6 watch loop.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from argus.core.contracts import OHLCVSeries
from argus.core.enums import Severity, Timeframe
from argus.platform.indicators import atr, ema, ohlcv_floats, rsi, true_ranges


class TriggerKind(StrEnum):
    """Technical trigger kinds."""

    RSI_OVERBOUGHT = "rsi_overbought"
    RSI_OVERSOLD = "rsi_oversold"
    PRICE_MOVE = "price_move"
    ATR_SPIKE = "atr_spike"
    EMA_CROSS_UP = "ema_cross_up"
    EMA_CROSS_DOWN = "ema_cross_down"


class TriggerSignal(BaseModel):
    """A single technical trigger finding (local, before the brain)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    kind: TriggerKind
    severity: Severity
    message: str
    value: float | None = None


class TriggerConfig(BaseModel):
    """Trigger thresholds — "config is data" (hot-reloadable from YAML later)."""

    model_config = ConfigDict(frozen=True)

    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    price_move_pct: float = 5.0  # close-to-close % threshold of the last bar
    atr_period: int = 14
    atr_spike_mult: float = 2.0  # last TR / ATR ≥ this → spike
    ema_fast: int = 12
    ema_slow: int = 26


class TriggerEngine:
    """Evaluate an OHLCV series and emit technical triggers (side-effect free)."""

    def __init__(self, config: TriggerConfig | None = None) -> None:
        self.config = config or TriggerConfig()

    def evaluate(self, series: OHLCVSeries) -> list[TriggerSignal]:
        cfg = self.config
        signals: list[TriggerSignal] = []
        if len(series.bars) < 2:
            return signals

        highs, lows, closes = ohlcv_floats(series)

        def add(kind: TriggerKind, severity: Severity, message: str, value: float | None) -> None:
            signals.append(
                TriggerSignal(
                    symbol=series.symbol,
                    timeframe=series.timeframe,
                    kind=kind,
                    severity=severity,
                    message=message,
                    value=value,
                )
            )

        # ── RSI extremes ───────────────────────────────────────────────────────
        last_rsi = rsi(closes, cfg.rsi_period)[-1]
        if last_rsi is not None:
            if last_rsi >= cfg.rsi_overbought:
                add(
                    TriggerKind.RSI_OVERBOUGHT,
                    Severity.MEDIUM,
                    f"RSI {last_rsi:.1f} (overbought)",
                    last_rsi,
                )
            elif last_rsi <= cfg.rsi_oversold:
                add(
                    TriggerKind.RSI_OVERSOLD,
                    Severity.MEDIUM,
                    f"RSI {last_rsi:.1f} (oversold)",
                    last_rsi,
                )

        # ── Price move (last close) ────────────────────────────────────────────
        prev_close, last_close = closes[-2], closes[-1]
        if prev_close > 0:
            move = (last_close - prev_close) / prev_close * 100.0
            if abs(move) >= cfg.price_move_pct:
                severity = Severity.HIGH if abs(move) >= 2 * cfg.price_move_pct else Severity.MEDIUM
                add(TriggerKind.PRICE_MOVE, severity, f"{move:+.2f}% price move", move)

        # ── ATR spike ───────────────────────────────────────────────────────────
        last_atr = atr(highs, lows, closes, cfg.atr_period)[-1]
        if last_atr is not None and last_atr > 0:
            ratio = true_ranges(highs, lows, closes)[-1] / last_atr
            if ratio >= cfg.atr_spike_mult:
                add(TriggerKind.ATR_SPIKE, Severity.MEDIUM, f"ATR spike {ratio:.1f}×", ratio)

        # ── EMA crossover (golden/death cross) ───────────────────────────────────
        ef = ema(closes, cfg.ema_fast)
        es = ema(closes, cfg.ema_slow)
        ef_last, ef_prev, es_last, es_prev = ef[-1], ef[-2], es[-1], es[-2]
        if (
            ef_last is not None
            and ef_prev is not None
            and es_last is not None
            and es_prev is not None
        ):
            prev_diff = ef_prev - es_prev
            cur_diff = ef_last - es_last
            if prev_diff <= 0 < cur_diff:
                add(
                    TriggerKind.EMA_CROSS_UP,
                    Severity.MEDIUM,
                    "EMA upward crossover (golden cross)",
                    cur_diff,
                )
            elif prev_diff >= 0 > cur_diff:
                add(
                    TriggerKind.EMA_CROSS_DOWN,
                    Severity.MEDIUM,
                    "EMA downward crossover (death cross)",
                    cur_diff,
                )

        return signals


def should_wake_brain(
    signals: list[TriggerSignal], *, min_severity: Severity = Severity.MEDIUM
) -> bool:
    """Wake the brain if any trigger reaches the threshold severity.

    Basis of the cost governor: the LLM only wakes on a trigger of sufficient severity.
    """
    return any(s.severity.rank >= min_severity.rank for s in signals)
