"""Backtest engine tests — no-look-ahead proof, determinism, known result.

Fully offline/synthetic; the engine runs the same components as the live pipeline
(TriggerEngine + deterministic agents + aggregator + outcome.evaluate_direction).

Known-result setup: a **zigzag uptrend** (alternating +3% / −1.8%) — RSI ~62 (<70, so the
overbought vote does not fire), EMA12>EMA26 (up) → TechnicalAgent is net BULLISH; the +3% days
cross the ``price_move_pct=2.5`` trigger → signal. Forward return is positive and the k×ATR
touch is reached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.contracts import OHLCVBar, OHLCVSeries, Provenance
from argus.core.enums import Stance, Timeframe
from argus.decision.backtest import BacktestSignal, replay_signals, run_backtest
from argus.platform.triggers import TriggerConfig

BASE = datetime(2026, 1, 1, tzinfo=UTC)

# Zigzag trigger config: +3% days fire a MEDIUM price-move trigger (2.5 ≤ 3 < 5).
_ZIGZAG_TRIGGERS = TriggerConfig(price_move_pct=2.5)


def _series(prices: list[float], *, adjusted: bool = False) -> OHLCVSeries:
    step = timedelta(seconds=Timeframe.D1.seconds)
    bars = tuple(
        OHLCVBar(
            timestamp=BASE + step * i,
            open=Decimal(str(round(prices[i - 1] if i > 0 else p, 4))),
            high=Decimal(str(round(max(prices[i - 1] if i > 0 else p, p) + 1, 4))),
            low=Decimal(str(round(min(prices[i - 1] if i > 0 else p, p) - 1, 4))),
            close=Decimal(str(round(p, 4))),
            volume=Decimal("10"),
        )
        for i, p in enumerate(prices)
    )
    prov = Provenance(
        source="test", timestamp=bars[-1].timestamp, confidence=0.9, adjusted=adjusted
    )
    return OHLCVSeries(symbol="X", timeframe=Timeframe.D1, bars=bars, provenance=prov)


def _zigzag_up(n: int = 90, *, start: float = 100.0) -> list[float]:
    """Steady uptrend alternating +3% / −1.8% (net +1.1% per 2 candles)."""
    prices = [start]
    for i in range(1, n):
        factor = 1.03 if i % 2 == 1 else 0.982
        prices.append(prices[-1] * factor)
    return prices


def _flat(n: int = 90) -> list[float]:
    # Perfectly flat: no trigger (price/RSI/ATR/EMA cross) fires.
    return [100.0] * n


def _signals(prices: list[float], **kwargs: object) -> list[BacktestSignal]:
    return replay_signals(
        _series(prices),
        warmup_bars=40,
        trigger_config=_ZIGZAG_TRIGGERS,
        **kwargs,  # type: ignore[arg-type]
    )


def test_replay_produces_bullish_signals_in_uptrend() -> None:
    signals = _signals(_zigzag_up())
    assert signals, "a zigzag uptrend must produce signals"
    assert all(s.direction == 1 for s in signals)  # EMA up + RSI<70 → bullish


def test_replay_flat_series_produces_no_signals() -> None:
    # No trigger (move below 2.5%) → no wake-up → no signal (cost governor behaviour).
    assert _signals(_flat()) == []


def test_replay_no_look_ahead() -> None:
    """No-look-ahead proof: signals on the shared prefix are the same whatever comes later."""
    full = _zigzag_up(90)
    prefix = full[:65]
    sig_full = [s for s in _signals(full) if s.index < 65]
    sig_prefix = _signals(prefix)

    def key(s: BacktestSignal) -> tuple[int, int, int, int]:
        return (s.index, s.direction, s.strength, s.confluence)

    assert [key(s) for s in sig_full] == [key(s) for s in sig_prefix]


def test_run_backtest_deterministic() -> None:
    series = _series(_zigzag_up())
    r1 = run_backtest(series, horizons=(1, 5), trigger_config=_ZIGZAG_TRIGGERS)
    r2 = run_backtest(series, horizons=(1, 5), trigger_config=_ZIGZAG_TRIGGERS)
    assert r1 == r2  # frozen dataclasses → exact equality (incl. bootstrap CI, fixed seed)


def test_known_result_uptrend_hits_and_positive_return() -> None:
    # Known geometry: signals arrive on +3% days → a 5-candle window starts with a drop
    # (net ≈ +0.46%, inside the threshold); a 6-candle window ends on a +3% day (net ≈ +3.5%)
    # → it EXCEEDS the k_atr=0.5 threshold (≈1.85%): touch + close are both certain hits.
    series = _series(_zigzag_up())
    report = run_backtest(
        series, horizons=(6,), split=0.95, k_atr=0.5, trigger_config=_ZIGZAG_TRIGGERS
    )  # split=0.95 → signals stay in-sample
    s = next(x for x in report.stats if x.segment == "in_sample")
    assert s.n_signals >= 3
    assert s.mean_ret_pct is not None and s.mean_ret_pct > 0  # signed return is + in an uptrend
    assert s.touch.n >= 1 and s.touch.hit >= 1  # the 0.5×ATR touch happened
    assert s.close.n >= 1 and s.close.hit >= 1  # the horizon close is above the threshold too
    # Baselines use the same opportunity set; the bullish baseline hits touch in an uptrend.
    bullish_touch = s.baselines["bullish"]["touch"]
    assert bullish_touch.n >= 1 and bullish_touch.hit == bullish_touch.n
    # A CI was produced (n≥3) and is ordered.
    assert s.ci_low_pct is not None and s.ci_high_pct is not None
    assert s.ci_low_pct <= s.ci_high_pct


def test_split_segments_signals() -> None:
    series = _series(_zigzag_up())
    report = run_backtest(series, split=0.5, horizons=(1,), trigger_config=_ZIGZAG_TRIGGERS)
    boundary = report.split_index
    assert all(s.in_sample == (s.index < boundary) for s in report.signals)
    oos = next(x for x in report.stats if x.segment == "out_of_sample")
    assert oos.n_signals == sum(1 for s in report.signals if not s.in_sample)


def test_report_carries_adjusted_flag_and_test_count() -> None:
    report = run_backtest(
        _series(_zigzag_up(), adjusted=True), horizons=(1, 5), trigger_config=_ZIGZAG_TRIGGERS
    )
    assert report.adjusted_series is True  # the report states which view the run used
    assert report.n_tests == 2 * 2 * 2  # horizon × metric × segment
    assert report.bonferroni_alpha == round(0.05 / 8, 5)


def test_directional_filter_excludes_neutral() -> None:
    # Structural guarantee: signals come only from directional proposals (no NEUTRAL/NO_DATA).
    for s in _signals(_zigzag_up()):
        assert s.direction in (1, -1)
        assert Stance.BULLISH if s.direction == 1 else Stance.BEARISH  # type consistency
