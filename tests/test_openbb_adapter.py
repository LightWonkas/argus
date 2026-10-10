"""OpenBBAdapter tests — no openbb required (fake provider)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.ingestion.openbb import OpenBBAdapter, interval_for
from argus.ingestion.openbb import _ensure_utc as ensure_utc

_T0 = datetime(2026, 5, 29, 13, 0, tzinfo=UTC)
_T1 = datetime(2026, 5, 29, 14, 0, tzinfo=UTC)


class FakeGlobalProvider:
    def __init__(
        self,
        *,
        quote: dict[str, Any] | None = None,
        ohlcv: list[list[Any]] | None = None,
        raise_on_quote: bool = False,
    ) -> None:
        self._quote = quote
        self._ohlcv = ohlcv or []
        self._raise = raise_on_quote

    def quote(self, symbol: str) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("network error")
        assert self._quote is not None
        return self._quote

    def ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int) -> list[list[Any]]:
        return self._ohlcv


def test_source_default() -> None:
    assert OpenBBAdapter(FakeGlobalProvider(quote={})).source == "openbb"


def test_fetch_quote_defaults_to_usd() -> None:
    provider = FakeGlobalProvider(quote={"price": 512.5, "change_pct": 1.2, "timestamp": _T1})
    q = OpenBBAdapter(provider).fetch_quote("SPY")
    assert q.symbol == "SPY"
    assert q.price == Decimal("512.5")
    assert q.quote_currency == "USD"
    assert q.change_pct_24h == 1.2
    assert q.provenance.source == "openbb"


def test_quote_missing_price_raises() -> None:
    with pytest.raises(AdapterError):
        OpenBBAdapter(FakeGlobalProvider(quote={"currency": "USD"})).fetch_quote("SPY")


def test_provider_error_wrapped() -> None:
    with pytest.raises(AdapterError):
        OpenBBAdapter(FakeGlobalProvider(raise_on_quote=True)).fetch_quote("SPY")


def test_fetch_ohlcv_maps_rows() -> None:
    rows = [
        [_T0, 100, 110, 95, 105, 1000],
        [_T1, 105, 115, 100, 112, 1200],
    ]
    series = OpenBBAdapter(FakeGlobalProvider(ohlcv=rows)).fetch_ohlcv("SPY", Timeframe.D1, limit=2)
    assert series.symbol == "SPY"
    assert len(series.bars) == 2
    assert series.bars[1].close == Decimal("112")
    assert series.provenance.source == "openbb"


def test_fetch_ohlcv_invalid_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        OpenBBAdapter(FakeGlobalProvider(ohlcv=[])).fetch_ohlcv("SPY", Timeframe.D1, limit=0)


def test_fetch_ohlcv_malformed_row_raises() -> None:
    adapter = OpenBBAdapter(FakeGlobalProvider(ohlcv=[[_T0, 100, 110]]))
    with pytest.raises(AdapterError):
        adapter.fetch_ohlcv("SPY", Timeframe.D1)


def test_interval_mapping() -> None:
    assert interval_for(Timeframe.D1) == "1d"
    assert interval_for(Timeframe.W1) == "1wk"
    assert interval_for(Timeframe.MONTH1) == "1mo"
    with pytest.raises(AdapterError):
        interval_for(Timeframe.H4)  # not common across global providers


def test_ensure_utc() -> None:
    assert ensure_utc(datetime(2026, 5, 29, 10, 0)).tzinfo is UTC  # naive → UTC
    assert ensure_utc(_T0) == _T0  # tz-aware is preserved
    d = ensure_utc(date(2026, 5, 29))  # date → 00:00 UTC
    assert (d.hour, d.tzinfo) == (0, UTC)
    with pytest.raises(AdapterError):
        ensure_utc("not-a-date")
