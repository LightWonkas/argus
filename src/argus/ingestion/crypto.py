"""CryptoAdapter — crypto market data (ccxt/Binance).

The ccxt client is **injected** (DI): the real ``ccxt.binance()`` in production, a
fake client in tests. Unit tests therefore need no network, and ``CryptoAdapter``
can be imported even when ccxt is not installed (ccxt is lazily imported only inside
:func:`binance_adapter`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from argus.core.contracts import OHLCVBar, OHLCVSeries, Quote
from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.core.schemas import Provenance, utcnow
from argus.ingestion._convert import nonneg_or_none, pos_or_none, to_decimal
from argus.ingestion.base import MarketDataAdapter


@runtime_checkable
class ExchangeClient(Protocol):
    """The subset of the ccxt exchange client we use (structural type)."""

    id: str

    def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[float]]: ...


_KNOWN_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "TRY", "EUR")


def normalize_crypto_symbol(symbol: str) -> str:
    """Normalize ``BTC/USDT`` or ``BTCUSDT`` input to the canonical ``BTCUSDT`` form."""
    compact = symbol.strip().upper().replace("/", "")
    if not compact or not any(compact.endswith(quote) for quote in _KNOWN_QUOTES):
        raise AdapterError(f"unsupported crypto symbol format: {symbol!r}")
    return compact


def ccxt_symbol(symbol: str) -> str:
    """Convert a canonical symbol to ccxt's ``BASE/QUOTE`` form."""
    value = normalize_crypto_symbol(symbol)
    for quote in _KNOWN_QUOTES:
        if value.endswith(quote) and len(value) > len(quote):
            return f"{value[: -len(quote)]}/{quote}"
    raise AdapterError(f"could not resolve crypto quote currency: {symbol!r}")


def _quote_currency(symbol: str) -> str:
    return ccxt_symbol(symbol).split("/", 1)[1]


class CryptoAdapter(MarketDataAdapter):
    """Convert a ccxt-compatible exchange client to the common contract."""

    def __init__(self, client: ExchangeClient, *, confidence: float = 0.95) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0..1]")
        self._client = client
        self._confidence = confidence

    @property
    def source(self) -> str:
        return f"ccxt:{self._client.id}"

    def fetch_quote(self, symbol: str) -> Quote:
        provider_symbol = ccxt_symbol(symbol)
        try:
            raw = self._client.fetch_ticker(provider_symbol)
        except AdapterError:
            raise
        except Exception as exc:  # external client error → wrap as adapter error
            raise AdapterError(f"{symbol}: fetch_ticker failed: {exc}") from exc

        last = raw.get("last")
        if last is None:
            last = raw.get("close")
        if last is None:
            raise AdapterError(f"{symbol}: ticker has no 'last'/'close'")

        ts_ms = raw.get("timestamp")
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else utcnow()
        provenance = Provenance(source=self.source, timestamp=ts, confidence=self._confidence)

        try:
            return Quote(
                symbol=symbol,
                price=to_decimal(last),
                quote_currency=_quote_currency(symbol),
                change_pct_24h=raw.get("percentage"),
                bid=pos_or_none(raw.get("bid")),
                ask=pos_or_none(raw.get("ask")),
                base_volume_24h=nonneg_or_none(raw.get("baseVolume")),
                provenance=provenance,
            )
        except ValidationError as exc:
            raise AdapterError(f"{symbol}: invalid quote: {exc}") from exc

    def fetch_ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        if limit <= 0:
            raise ValueError("limit must be positive")
        try:
            rows = self._client.fetch_ohlcv(ccxt_symbol(symbol), timeframe.value, limit=limit)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"{symbol} {timeframe}: fetch_ohlcv failed: {exc}") from exc

        bars: list[OHLCVBar] = []
        for row in rows:
            bars.append(self._row_to_bar(row, symbol=symbol, timeframe=timeframe))

        last_ts = bars[-1].timestamp if bars else utcnow()
        provenance = Provenance(source=self.source, timestamp=last_ts, confidence=self._confidence)
        return OHLCVSeries(
            symbol=symbol, timeframe=timeframe, bars=tuple(bars), provenance=provenance
        )

    @staticmethod
    def _row_to_bar(row: list[float], *, symbol: str, timeframe: Timeframe) -> OHLCVBar:
        try:
            ms, o, h, low, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        except (IndexError, TypeError) as exc:
            raise AdapterError(f"{symbol} {timeframe}: malformed OHLCV row: {row!r}") from exc
        try:
            return OHLCVBar(
                timestamp=datetime.fromtimestamp(ms / 1000, tz=UTC),
                open=to_decimal(o),
                high=to_decimal(h),
                low=to_decimal(low),
                close=to_decimal(c),
                volume=to_decimal(v),
            )
        except ValidationError as exc:
            raise AdapterError(f"{symbol} {timeframe}: invalid candle {row!r}: {exc}") from exc


def binance_adapter(*, confidence: float = 0.95) -> CryptoAdapter:
    """Build a ``CryptoAdapter`` backed by the real Binance (ccxt).

    ccxt is imported only here (at call time); public market data needs no
    API key.
    """
    import ccxt  # lazy import

    client = ccxt.binance({"enableRateLimit": True})
    return CryptoAdapter(client, confidence=confidence)
