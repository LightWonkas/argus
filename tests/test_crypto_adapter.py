"""CryptoAdapter tests — no network needed (a fake ccxt client is injected)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.ingestion import CryptoAdapter

# Fake OHLCV: [ms, open, high, low, close, volume]
_BASE_MS = 1_748_520_000_000  # around 2025-05-29T12:00:00Z
_HOUR_MS = 3_600_000


class FakeExchange:
    """Offline fake exchange that satisfies the ExchangeClient protocol."""

    id = "binance"

    def __init__(
        self,
        *,
        ticker: dict[str, Any] | None = None,
        ohlcv: list[list[float]] | None = None,
        raise_on_ticker: bool = False,
    ) -> None:
        self._ticker = ticker
        self._ohlcv = ohlcv or []
        self._raise = raise_on_ticker

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("network error")
        assert self._ticker is not None
        return self._ticker

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[float]]:
        return self._ohlcv


def test_source_label() -> None:
    adapter = CryptoAdapter(FakeExchange(ticker={}))
    assert adapter.source == "ccxt:binance"


def test_fetch_quote_maps_fields() -> None:
    ticker = {
        "last": 50000.5,
        "bid": 49999.0,
        "ask": 50001.0,
        "baseVolume": 1234.5,
        "percentage": 2.5,
        "timestamp": _BASE_MS,
    }
    adapter = CryptoAdapter(FakeExchange(ticker=ticker), confidence=0.9)
    q = adapter.fetch_quote("BTC/USDT")

    assert q.symbol == "BTC/USDT"
    assert q.price == Decimal("50000.5")
    assert q.quote_currency == "USDT"
    assert q.change_pct_24h == 2.5
    assert q.bid == Decimal("49999.0")
    assert q.provenance.source == "ccxt:binance"
    assert q.provenance.confidence == 0.9


def test_fetch_quote_falls_back_to_close() -> None:
    adapter = CryptoAdapter(FakeExchange(ticker={"last": None, "close": 100.0, "timestamp": None}))
    q = adapter.fetch_quote("ETH/USDT")
    assert q.price == Decimal("100.0")


def test_fetch_quote_missing_price_raises() -> None:
    adapter = CryptoAdapter(FakeExchange(ticker={"last": None, "close": None}))
    with pytest.raises(AdapterError):
        adapter.fetch_quote("BTC/USDT")


def test_compact_symbol_is_accepted_and_normalized_for_ccxt() -> None:
    adapter = CryptoAdapter(FakeExchange(ticker={"last": 1.0}))
    quote = adapter.fetch_quote("BTCUSDT")
    assert quote.symbol == "BTCUSDT"
    assert quote.quote_currency == "USDT"


def test_unknown_quote_symbol_raises() -> None:
    adapter = CryptoAdapter(FakeExchange(ticker={"last": 1.0}))
    with pytest.raises(AdapterError):
        adapter.fetch_quote("NOTAPAIR")


def test_fetch_ticker_error_wrapped() -> None:
    adapter = CryptoAdapter(FakeExchange(raise_on_ticker=True))
    with pytest.raises(AdapterError):
        adapter.fetch_quote("BTC/USDT")


def test_fetch_ohlcv_maps_rows() -> None:
    rows = [
        [_BASE_MS, 100.0, 110.0, 95.0, 105.0, 10.0],
        [_BASE_MS + _HOUR_MS, 105.0, 115.0, 100.0, 112.0, 12.0],
    ]
    adapter = CryptoAdapter(FakeExchange(ohlcv=rows))
    series = adapter.fetch_ohlcv("BTC/USDT", Timeframe.H1, limit=2)

    assert series.symbol == "BTC/USDT"
    assert series.timeframe is Timeframe.H1
    assert len(series.bars) == 2
    assert series.bars[0].close == Decimal("105.0")
    assert series.bars[1].high == Decimal("115.0")
    assert series.provenance.source == "ccxt:binance"


def test_fetch_ohlcv_invalid_limit() -> None:
    adapter = CryptoAdapter(FakeExchange(ohlcv=[]))
    with pytest.raises(ValueError, match="limit"):
        adapter.fetch_ohlcv("BTC/USDT", Timeframe.H1, limit=0)


def test_fetch_ohlcv_malformed_row_raises() -> None:
    adapter = CryptoAdapter(FakeExchange(ohlcv=[[_BASE_MS, 100.0]]))  # missing fields
    with pytest.raises(AdapterError):
        adapter.fetch_ohlcv("BTC/USDT", Timeframe.H1)
