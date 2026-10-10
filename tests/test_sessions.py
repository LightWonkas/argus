"""Session-awareness tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.core.enums import AssetClass
from argus.interface import is_market_open, scan_interval_seconds


def _weekday_at(hour: int) -> datetime:
    d = datetime(2026, 5, 25, hour, 0, tzinfo=UTC)
    while d.weekday() >= 5:  # shift to a weekday
        d += timedelta(days=1)
    return d


def _weekend_at(hour: int) -> datetime:
    d = datetime(2026, 5, 25, hour, 0, tzinfo=UTC)
    while d.weekday() < 5:  # shift to the weekend
        d += timedelta(days=1)
    return d


def test_crypto_always_open() -> None:
    assert is_market_open(AssetClass.CRYPTO, _weekend_at(3)) is True
    assert is_market_open(AssetClass.CRYPTO, _weekday_at(3)) is True


def test_bist_open_during_session() -> None:
    # weekday 08:00 UTC (11:00 Istanbul) → open
    assert is_market_open(AssetClass.BIST, _weekday_at(8)) is True


def test_bist_closed_before_open() -> None:
    # weekday 03:00 UTC (before the 07:00 UTC open) → closed
    assert is_market_open(AssetClass.BIST, _weekday_at(3)) is False


def test_bist_closed_weekend() -> None:
    assert is_market_open(AssetClass.BIST, _weekend_at(8)) is False


def test_metal_weekday_open_weekend_closed() -> None:
    assert is_market_open(AssetClass.METAL, _weekday_at(3)) is True
    assert is_market_open(AssetClass.METAL, _weekend_at(3)) is False


def test_scan_interval_ordering() -> None:
    assert scan_interval_seconds("holdings") < scan_interval_seconds("watchlist")
    assert scan_interval_seconds("watchlist") < scan_interval_seconds("broad")


def test_naive_now_rejected() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        is_market_open(AssetClass.BIST, datetime(2026, 5, 25, 8, 0))
