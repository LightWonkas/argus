"""TRAdapter — Turkish (BIST/VIOP) market data.

``TRAdapter`` wraps borsapy + KAP + TCMB EVDS. BIST is the top-priority asset class.

Design: borsapy-specific details (pandas DataFrame, the ``.info`` dict) are hidden
behind a **provider** (:class:`BISTDataProvider`) that returns plain Python data.
:class:`TRAdapter` and its unit tests therefore **do not require pandas/borsapy** (DI).
The real borsapy provider is built lazily via :func:`borsapy_provider`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from argus.core.contracts import OHLCVBar, OHLCVSeries, Quote
from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.core.schemas import Provenance, utcnow
from argus.ingestion._convert import nonneg_or_none, to_decimal
from argus.ingestion.base import MarketDataAdapter

DEFAULT_CURRENCY = "TRY"


@runtime_checkable
class BISTDataProvider(Protocol):
    """BIST data provider interface TRAdapter needs.

    Keys expected in the dict returned by ``quote``:
      ``price`` (required), ``currency`` (default TRY), ``change_pct``, ``timestamp``
      (tz-aware datetime), ``volume``, ``bid``, ``ask`` (optional).
    Each ``ohlcv`` row is ``[timestamp(datetime), open, high, low, close, volume]``.
    """

    def quote(self, symbol: str) -> Mapping[str, Any]: ...

    def ohlcv(
        self, symbol: str, timeframe: Timeframe, *, limit: int, adjust: bool = False
    ) -> Sequence[Sequence[Any]]: ...


class TRAdapter(MarketDataAdapter):
    """Convert a BIST data provider to the common contract."""

    def __init__(
        self, provider: BISTDataProvider, *, source: str = "borsapy", confidence: float = 0.9
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0..1]")
        self._provider = provider
        self._source = source
        self._confidence = confidence

    @property
    def source(self) -> str:
        return self._source

    def fetch_quote(self, symbol: str) -> Quote:
        try:
            raw = self._provider.quote(symbol)
        except AdapterError:
            raise
        except Exception as exc:  # provider/network error → wrap as adapter error
            raise AdapterError(f"{symbol}: BIST quote failed: {exc}") from exc

        price = raw.get("price")
        if price is None:
            raise AdapterError(f"{symbol}: BIST quote has no price")

        ts = raw.get("timestamp") or utcnow()
        currency = raw.get("currency") or DEFAULT_CURRENCY
        try:
            provenance = Provenance(source=self._source, timestamp=ts, confidence=self._confidence)
            return Quote(
                symbol=symbol,
                price=to_decimal(price),
                quote_currency=currency,
                change_pct_24h=raw.get("change_pct"),
                base_volume_24h=nonneg_or_none(raw.get("volume")),
                provenance=provenance,
            )
        except ValidationError as exc:
            raise AdapterError(f"{symbol}: invalid BIST quote: {exc}") from exc

    def fetch_ohlcv(
        self, symbol: str, timeframe: Timeframe, *, limit: int = 100, adjust: bool = False
    ) -> OHLCVSeries:
        """Fetch OHLCV. The default is **RAW** (PIT — the view written to the store).

        ``adjust=True`` is used only to derive corporate actions (feed_once);
        the view is honestly labeled via ``Provenance.adjusted``.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        try:
            rows = self._provider.ohlcv(symbol, timeframe, limit=limit, adjust=adjust)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"{symbol} {timeframe}: BIST OHLCV failed: {exc}") from exc

        bars = [self._row_to_bar(row, symbol=symbol, timeframe=timeframe) for row in rows]
        last_ts = bars[-1].timestamp if bars else utcnow()
        provenance = Provenance(
            source=self._source, timestamp=last_ts, confidence=self._confidence, adjusted=adjust
        )
        return OHLCVSeries(
            symbol=symbol, timeframe=timeframe, bars=tuple(bars), provenance=provenance
        )

    def _row_to_bar(self, row: Sequence[Any], *, symbol: str, timeframe: Timeframe) -> OHLCVBar:
        try:
            ts, o, h, low, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        except (IndexError, TypeError) as exc:
            raise AdapterError(f"{symbol} {timeframe}: malformed OHLCV row: {row!r}") from exc
        try:
            return OHLCVBar(
                timestamp=ts,
                open=to_decimal(o),
                high=to_decimal(h),
                low=to_decimal(low),
                close=to_decimal(c),
                volume=to_decimal(v),
            )
        except ValidationError as exc:
            raise AdapterError(f"{symbol} {timeframe}: invalid candle {row!r}: {exc}") from exc
