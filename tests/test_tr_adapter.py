"""TRAdapter (BIST) tests — no borsapy/pandas required (fake provider)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.ingestion.tr import TRAdapter

_T0 = datetime(2026, 5, 29, 7, 0, tzinfo=UTC)
_T1 = datetime(2026, 5, 29, 8, 0, tzinfo=UTC)


class FakeBISTProvider:
    """Fake provider satisfying the BISTDataProvider protocol."""

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
        self.last_adjust: bool | None = None  # which view was requested (test observation)

    def quote(self, symbol: str) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("borsapy error")
        assert self._quote is not None
        return self._quote

    def ohlcv(
        self, symbol: str, timeframe: Timeframe, *, limit: int, adjust: bool = False
    ) -> list[list[Any]]:
        self.last_adjust = adjust
        return self._ohlcv


def test_source_default() -> None:
    assert TRAdapter(FakeBISTProvider(quote={})).source == "borsapy"


def test_fetch_quote_defaults_to_try() -> None:
    provider = FakeBISTProvider(
        quote={"price": 312.5, "change_pct": 1.8, "volume": 1_000_000, "timestamp": _T1}
    )
    q = TRAdapter(provider, confidence=0.9).fetch_quote("THYAO")

    assert q.symbol == "THYAO"
    assert q.price == Decimal("312.5")
    assert q.quote_currency == "TRY"  # default
    assert q.change_pct_24h == 1.8
    assert q.base_volume_24h == Decimal("1000000")
    assert q.provenance.source == "borsapy"
    assert q.provenance.confidence == 0.9


def test_fetch_quote_explicit_currency() -> None:
    provider = FakeBISTProvider(quote={"price": 100, "currency": "USD", "timestamp": _T1})
    q = TRAdapter(provider).fetch_quote("XU100")
    assert q.quote_currency == "USD"


def test_quote_missing_price_raises() -> None:
    with pytest.raises(AdapterError):
        TRAdapter(FakeBISTProvider(quote={"currency": "TRY"})).fetch_quote("THYAO")


def test_quote_naive_timestamp_raises() -> None:
    # naive timestamp → Provenance rejects it → wrapped in AdapterError
    provider = FakeBISTProvider(quote={"price": 10, "timestamp": datetime(2026, 1, 1)})
    with pytest.raises(AdapterError):
        TRAdapter(provider).fetch_quote("THYAO")


def test_provider_error_wrapped() -> None:
    with pytest.raises(AdapterError):
        TRAdapter(FakeBISTProvider(raise_on_quote=True)).fetch_quote("THYAO")


def test_fetch_ohlcv_maps_rows() -> None:
    rows = [
        [_T0, 100, 110, 95, 105, 1000],
        [_T1, 105, 115, 100, 112, 1200],
    ]
    series = TRAdapter(FakeBISTProvider(ohlcv=rows)).fetch_ohlcv("THYAO", Timeframe.H1, limit=2)

    assert series.symbol == "THYAO"
    assert len(series.bars) == 2
    assert series.bars[1].close == Decimal("112")
    assert series.provenance.source == "borsapy"


def test_fetch_ohlcv_malformed_row_raises() -> None:
    adapter = TRAdapter(FakeBISTProvider(ohlcv=[[_T0, 100, 110]]))  # missing fields
    with pytest.raises(AdapterError):
        adapter.fetch_ohlcv("THYAO", Timeframe.D1)


def test_fetch_ohlcv_default_is_raw_pit() -> None:
    # The default fetch is RAW (adjust=False) → the store stays pure PIT; provenance is
    # labelled honestly.
    provider = FakeBISTProvider(ohlcv=[[_T0, 100, 110, 95, 105, 1000]])
    series = TRAdapter(provider).fetch_ohlcv("THYAO", Timeframe.D1, limit=1)
    assert provider.last_adjust is False
    assert series.provenance.adjusted is False


def test_fetch_ohlcv_adjusted_view_is_labeled() -> None:
    # adjust=True is only for corporate-action derivation; the view is labelled explicitly.
    provider = FakeBISTProvider(ohlcv=[[_T0, 100, 110, 95, 105, 1000]])
    series = TRAdapter(provider).fetch_ohlcv("THYAO", Timeframe.D1, limit=1, adjust=True)
    assert provider.last_adjust is True
    assert series.provenance.adjusted is True


def test_fetch_ohlcv_invalid_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        TRAdapter(FakeBISTProvider(ohlcv=[])).fetch_ohlcv("THYAO", Timeframe.D1, limit=-1)
