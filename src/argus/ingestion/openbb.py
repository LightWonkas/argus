"""OpenBBAdapter — global (secondary/context) market data: equities/forex/commodities/macro.

OpenBB is the global data backbone. Asset-class priority is BIST→metals→crypto;
OpenBB supplies **secondary/context** data (US/global equities, FX, intermarket DXY, etc.).

Design: isolation behind a provider (:class:`GlobalDataProvider`), as in the TR/borsapy
pattern → :class:`OpenBBAdapter` and its tests **do not require openbb/pandas** (DI).
``openbb`` is lazily imported only when the live provider is built; OpenBB is a very
heavy package and live calls need provider keys/network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from argus.core.contracts import OHLCVBar, OHLCVSeries, Quote
from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.core.schemas import Provenance, utcnow
from argus.ingestion._convert import nonneg_or_none, to_decimal
from argus.ingestion.base import MarketDataAdapter

# Timeframe → OpenBB/yfinance interval string. 4h is uncommon among global providers.
_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1wk",
    Timeframe.MONTH1: "1mo",
}


def interval_for(timeframe: Timeframe) -> str:
    """Map a Timeframe to an OpenBB interval string; raise if unsupported."""
    interval = _INTERVAL.get(timeframe)
    if interval is None:
        raise AdapterError(f"timeframe not supported for OpenBB: {timeframe.value}")
    return interval


@runtime_checkable
class GlobalDataProvider(Protocol):
    """Provider interface OpenBBAdapter needs (OpenBB implements it).

    ``quote`` keys: ``price`` (required), ``currency`` (default USD), ``change_pct``,
    ``timestamp`` (tz-aware datetime), ``volume``.
    ``ohlcv`` rows: ``[timestamp(datetime), open, high, low, close, volume]``.
    """

    def quote(self, symbol: str) -> Mapping[str, Any]: ...

    def ohlcv(
        self, symbol: str, timeframe: Timeframe, *, limit: int
    ) -> Sequence[Sequence[Any]]: ...


class OpenBBAdapter(MarketDataAdapter):
    """Convert a global data provider to the common contract."""

    def __init__(
        self, provider: GlobalDataProvider, *, source: str = "openbb", confidence: float = 0.9
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
        except Exception as exc:
            raise AdapterError(f"{symbol}: OpenBB quote failed: {exc}") from exc

        price = raw.get("price")
        if price is None:
            raise AdapterError(f"{symbol}: OpenBB quote has no price")
        ts = raw.get("timestamp") or utcnow()
        currency = raw.get("currency") or "USD"
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
            raise AdapterError(f"{symbol}: invalid OpenBB quote: {exc}") from exc

    def fetch_ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        if limit <= 0:
            raise ValueError("limit must be positive")
        try:
            rows = self._provider.ohlcv(symbol, timeframe, limit=limit)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"{symbol} {timeframe}: OpenBB OHLCV failed: {exc}") from exc

        bars = [self._row_to_bar(row, symbol=symbol, timeframe=timeframe) for row in rows]
        last_ts = bars[-1].timestamp if bars else utcnow()
        provenance = Provenance(source=self._source, timestamp=last_ts, confidence=self._confidence)
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


def _ensure_utc(value: Any) -> datetime:
    """Convert an OpenBB date/datetime to tz-aware UTC (naive → UTC, date → 00:00 UTC)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise AdapterError(f"could not parse OpenBB timestamp: {value!r}")


def _attr(obj: Any, *names: str) -> Any:
    """Return the first present/non-None attribute (OpenBB field-name variations)."""
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return None


class OpenBBProvider:
    """Live global data provider (OpenBB). Implements ``GlobalDataProvider``.

    openbb is lazily imported only here. **Not unit tested** (network + provider key).
    Written defensively against the documented OpenBB 4.x API; field/attribute names
    can vary by provider → it should be smoke tested on the user's machine.
    """

    def __init__(self, *, provider: str | None = None) -> None:
        from openbb import obb  # lazy import (very heavy package)

        self._obb = obb
        self._provider = provider  # e.g. "yfinance", "fmp" (None → OpenBB default)

    def _kwargs(self) -> dict[str, Any]:
        return {"provider": self._provider} if self._provider else {}

    def ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int) -> list[list[Any]]:
        interval = interval_for(timeframe)
        data = self._obb.equity.price.historical(symbol=symbol, interval=interval, **self._kwargs())
        results = getattr(data, "results", data) or []
        rows: list[list[Any]] = []
        for r in results:
            ts_raw = _attr(r, "date", "timestamp")
            if ts_raw is None:
                continue
            rows.append(
                [
                    _ensure_utc(ts_raw),
                    _attr(r, "open"),
                    _attr(r, "high"),
                    _attr(r, "low"),
                    _attr(r, "close", "adj_close"),
                    _attr(r, "volume") or 0,
                ]
            )
        return rows[-limit:]

    def quote(self, symbol: str) -> dict[str, Any]:
        data = self._obb.equity.price.quote(symbol=symbol, **self._kwargs())
        results = getattr(data, "results", data)
        item = results[0] if isinstance(results, list) and results else results
        ts_raw = _attr(item, "last_timestamp", "date")
        return {
            "price": _attr(item, "last_price", "price", "close", "prev_close"),
            "currency": _attr(item, "currency") or "USD",
            "change_pct": _attr(item, "change_percent", "change_percent_1d"),
            "volume": _attr(item, "volume"),
            "timestamp": _ensure_utc(ts_raw) if ts_raw is not None else None,
        }


def openbb_adapter(*, provider: str | None = None, confidence: float = 0.9) -> OpenBBAdapter:
    """Build an ``OpenBBAdapter`` backed by the live OpenBB provider (lazy openbb import)."""
    return OpenBBAdapter(OpenBBProvider(provider=provider), confidence=confidence)
