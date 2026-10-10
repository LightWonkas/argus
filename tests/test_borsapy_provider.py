"""Tests for the pure BorsapyProvider helpers (no borsapy/pandas required).

The pandas/network glue (quote/ohlcv) is external integration and is not tested here;
only the pure conversion helpers are verified.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.ingestion.borsapy_provider import interval_for, lookback_seconds, to_utc


def test_interval_mapping() -> None:
    assert interval_for(Timeframe.M1) == "1m"
    assert interval_for(Timeframe.H1) == "1h"
    assert interval_for(Timeframe.D1) == "1d"
    assert interval_for(Timeframe.W1) == "1wk"  # borsapy uses '1wk', not '1w'
    assert interval_for(Timeframe.MONTH1) == "1mo"


def test_interval_unsupported_4h() -> None:
    with pytest.raises(AdapterError):
        interval_for(Timeframe.H4)  # borsapy does not offer 4h


def test_lookback_scales() -> None:
    assert lookback_seconds(Timeframe.M1, 10) == 10 * 60 * 1.4
    assert lookback_seconds(Timeframe.D1, 10) == 10 * 86_400 * 2.2
    # the daily TF uses a wider lookback to cover weekends/holidays
    assert lookback_seconds(Timeframe.D1, 1) > lookback_seconds(Timeframe.M1, 1)


def test_to_utc_naive_assumed_istanbul() -> None:
    # naive 10:00 Istanbul (UTC+3) → 07:00 UTC
    out = to_utc(datetime(2026, 5, 29, 10, 0))
    assert out.tzinfo is not None
    assert out.utcoffset() is not None and out.utcoffset().total_seconds() == 0
    assert out.hour == 7


def test_to_utc_aware_converted() -> None:
    out = to_utc(datetime(2026, 5, 29, 10, 0, tzinfo=UTC))
    assert out.hour == 10


def test_to_utc_pandas_like() -> None:
    class FakeTimestamp:
        def to_pydatetime(self) -> datetime:
            return datetime(2026, 5, 29, 10, 0)  # naive → assumed Istanbul

    assert to_utc(FakeTimestamp()).hour == 7


def test_to_utc_bad_input_raises() -> None:
    with pytest.raises(AdapterError):
        to_utc("not-a-date")
