"""Trigger engine tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import Severity, Timeframe
from argus.core.schemas import Provenance
from argus.platform import TriggerConfig, TriggerEngine, TriggerKind, TriggerSignal
from argus.platform.triggers import should_wake_brain

BASE = datetime(2026, 5, 29, tzinfo=UTC)


def _series(
    closes: Sequence[float],
    *,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    tf: Timeframe = Timeframe.H1,
) -> OHLCVSeries:
    bars: list[OHLCVBar] = []
    for i, c in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else c
        hi = highs[i] if highs is not None else max(open_, c) + 1
        lo = lows[i] if lows is not None else min(open_, c) - 1
        bars.append(
            OHLCVBar(
                timestamp=BASE + timedelta(hours=i),
                open=open_,
                high=hi,
                low=lo,
                close=c,
                volume=1,
            )
        )
    prov = Provenance(source="test", timestamp=bars[-1].timestamp, confidence=0.9)
    return OHLCVSeries(symbol="BTC/USDT", timeframe=tf, bars=tuple(bars), provenance=prov)


def _kinds(signals: list[TriggerSignal]) -> set[TriggerKind]:
    return {s.kind for s in signals}


def test_rsi_overbought() -> None:
    signals = TriggerEngine().evaluate(_series(list(range(100, 130))))
    assert TriggerKind.RSI_OVERBOUGHT in _kinds(signals)


def test_rsi_oversold() -> None:
    signals = TriggerEngine().evaluate(_series(list(range(130, 100, -1))))
    assert TriggerKind.RSI_OVERSOLD in _kinds(signals)


def test_price_move_high_severity() -> None:
    closes = [100, 99, 101, 100, 99, 101, 100, 99, 101, 100, 99, 101, 100, 99, 101, 120]
    signals = TriggerEngine().evaluate(_series(closes))
    moves = [s for s in signals if s.kind is TriggerKind.PRICE_MOVE]
    assert moves and moves[0].severity is Severity.HIGH  # ~18% > 2×threshold


def test_atr_spike() -> None:
    closes = [100.0] * 21
    highs = [101.0] * 20 + [140.0]
    lows = [99.0] * 20 + [60.0]  # huge range on the last bar
    signals = TriggerEngine().evaluate(_series(closes, highs=highs, lows=lows))
    assert TriggerKind.ATR_SPIKE in _kinds(signals)


def test_ema_cross_up() -> None:
    cfg = TriggerConfig(ema_fast=2, ema_slow=4)
    signals = TriggerEngine(cfg).evaluate(_series([10, 10, 10, 10, 10, 10, 30]))
    assert TriggerKind.EMA_CROSS_UP in _kinds(signals)


def test_ema_cross_down() -> None:
    cfg = TriggerConfig(ema_fast=2, ema_slow=4)
    signals = TriggerEngine(cfg).evaluate(_series([10, 10, 10, 10, 10, 10, 2]))
    assert TriggerKind.EMA_CROSS_DOWN in _kinds(signals)


def test_calm_short_series_no_signals() -> None:
    # 2 bars, 1% move: indicators not warmed up, below threshold → no trigger
    assert TriggerEngine().evaluate(_series([100, 101])) == []


def test_single_bar_no_signals() -> None:
    assert TriggerEngine().evaluate(_series([100])) == []


def _sig(severity: Severity) -> TriggerSignal:
    return TriggerSignal(
        symbol="X",
        timeframe=Timeframe.H1,
        kind=TriggerKind.PRICE_MOVE,
        severity=severity,
        message="",
    )


def test_should_wake_brain() -> None:
    assert should_wake_brain([]) is False
    assert should_wake_brain([_sig(Severity.LOW), _sig(Severity.INFO)]) is False
    assert should_wake_brain([_sig(Severity.MEDIUM)]) is True
    assert should_wake_brain([_sig(Severity.HIGH)]) is True
    # raise the threshold: MEDIUM no longer wakes
    assert should_wake_brain([_sig(Severity.MEDIUM)], min_severity=Severity.HIGH) is False
