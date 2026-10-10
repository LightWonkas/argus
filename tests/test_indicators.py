"""Technical indicator tests (pure math, checked against known values)."""

from __future__ import annotations

import pytest

from argus.platform.indicators import atr, ema, macd, rsi, sma, true_ranges


def test_sma_basic() -> None:
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_short_series_all_none() -> None:
    assert sma([1, 2], 3) == [None, None]


def test_sma_invalid_period() -> None:
    with pytest.raises(ValueError, match="period"):
        sma([1, 2, 3], 0)


def test_ema_seed_and_values() -> None:
    result = ema([1, 2, 3, 4], 2)
    assert result[0] is None
    assert result[1] == pytest.approx(1.5)  # seed = SMA(1,2)
    assert result[2] == pytest.approx(2.5)
    assert result[3] == pytest.approx(3.5)


def test_rsi_monotonic_up_is_100() -> None:
    closes = list(range(100, 120))  # strictly rising → no losses
    result = rsi(closes, 14)
    assert all(v is None for v in result[:14])
    assert result[14] == pytest.approx(100.0)


def test_rsi_monotonic_down_is_0() -> None:
    closes = list(range(120, 100, -1))  # strictly falling → no gains
    result = rsi(closes, 14)
    assert result[14] == pytest.approx(0.0)


def test_rsi_short_series_all_none() -> None:
    assert rsi([1, 2, 3], 14) == [None, None, None]


def test_rsi_flat_series_is_neutral_not_overbought() -> None:
    # Flat series (0 gain / 0 loss) → neutral 50; returning 100 would raise a false
    # "overbought" trigger in a sideways market (regression test).
    closes = [100.0] * 30
    result = rsi(closes, 14)
    assert result[-1] == pytest.approx(50.0)


def test_atr_constant_range() -> None:
    highs = [10.0] * 5
    lows = [8.0] * 5
    closes = [9.0] * 5
    assert atr(highs, lows, closes, 3) == [None, None, 2.0, 2.0, 2.0]


def test_true_ranges_length_mismatch() -> None:
    with pytest.raises(ValueError, match="must have the same length"):
        true_ranges([1, 2], [1], [1, 2])


def test_macd_alignment() -> None:
    closes = [float(x) for x in range(1, 61)]  # 60 values
    macd_line, signal_line, histogram = macd(closes)  # fast=12, slow=26, signal=9

    assert len(macd_line) == len(signal_line) == len(histogram) == 60
    assert macd_line[24] is None  # undefined before slow-1 = 25
    assert macd_line[25] is not None
    assert signal_line[32] is None  # +9 signal warm-up
    assert signal_line[33] is not None
    assert histogram[33] is not None


def test_macd_invalid_periods() -> None:
    with pytest.raises(ValueError, match="fast"):
        macd([1.0, 2.0, 3.0], fast=26, slow=12)
