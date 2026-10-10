"""borsapy-based :class:`~argus.ingestion.tr.BISTDataProvider` (live).

``borsapy`` is an unofficial third-party library. This is an optional integration,
disabled unless the ``bist`` extra is installed; users are responsible for
complying with the upstream data sources' terms of service.

borsapy + pandas are lazily imported **at runtime only, inside class methods**;
importing this module does not require borsapy/pandas. The pure helpers (interval mapping,
tz normalization, lookback) are unit tested; the pandas/network glue (``quote``/``ohlcv``)
is an external integration and is not unit tested here → it is smoke tested on the
user's machine (with network + pandas).

Verified borsapy API (source: github.com/saidsurucu/borsapy):
  * ``Ticker(sym).info`` → ``last`` (price), ``currency``, ``change_percent``,
    ``volume``, ``update_time``.
  * ``history(period, interval, start, end, ...)`` → DataFrame with columns
    ``Open/High/Low/Close/Volume``, DatetimeIndex. interval: ``1m,5m,15m,30m,1h,1d,1wk,1mo``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from argus.core.enums import Timeframe
from argus.core.errors import AdapterError
from argus.core.schemas import utcnow

if TYPE_CHECKING:
    from argus.ingestion.tr import TRAdapter

# BIST session time reference (used to interpret naive timestamps).
_ISTANBUL = ZoneInfo("Europe/Istanbul")

# Timeframe → borsapy interval string. borsapy does not offer 4h.
_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1wk",
    Timeframe.MONTH1: "1mo",
}

# Wider lookback on daily+ timeframes to cover weekends/holidays.
_DAILY_PLUS = {Timeframe.D1, Timeframe.W1, Timeframe.MONTH1}


def interval_for(timeframe: Timeframe) -> str:
    """Map a Timeframe to a borsapy interval string; raise if unsupported."""
    interval = _INTERVAL.get(timeframe)
    if interval is None:
        raise AdapterError(f"timeframe not supported for BIST: {timeframe.value}")
    return interval


def lookback_seconds(timeframe: Timeframe, limit: int) -> float:
    """Lookback duration (seconds) sufficient for ``limit`` candles."""
    factor = 2.2 if timeframe in _DAILY_PLUS else 1.4
    return max(limit, 1) * timeframe.seconds * factor


def to_utc(ts: Any) -> datetime:
    """Convert a pandas Timestamp / datetime to tz-aware UTC.

    A naive timestamp is assumed to be BIST session time (Europe/Istanbul) and converted.
    """
    dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if not isinstance(dt, datetime):
        raise AdapterError(f"could not convert timestamp to datetime: {ts!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ISTANBUL)
    return dt.astimezone(UTC)


def _info_get(info: Any, key: str) -> Any:
    getter = getattr(info, "get", None)
    if callable(getter):
        return getter(key)
    try:
        return info[key]
    except (KeyError, TypeError, IndexError):
        return None


def _is_nan(value: Any) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


class BorsapyProvider:
    """Live BIST data provider (borsapy). Implements the ``BISTDataProvider`` protocol."""

    def __init__(self) -> None:
        import borsapy  # lazy import (borsapy + pandas are only needed here)

        self._bp = borsapy

    def quote(self, symbol: str) -> dict[str, Any]:
        info = self._bp.Ticker(symbol).info
        ts_raw = _info_get(info, "update_time")
        timestamp = to_utc(ts_raw) if ts_raw is not None else None
        return {
            "price": _info_get(info, "last"),
            "currency": _info_get(info, "currency") or "TRY",
            "change_pct": _info_get(info, "change_percent"),
            "volume": _info_get(info, "volume"),
            "timestamp": timestamp,
        }

    def ohlcv(
        self, symbol: str, timeframe: Timeframe, *, limit: int, adjust: bool = False
    ) -> list[list[Any]]:
        interval = interval_for(timeframe)
        # Pass start as an ISO date string (same as the borsapy examples; avoids tz
        # comparison issues). Default adjust=False → RAW price (for the PIT store);
        # adjust=True is fetched only to derive corporate actions (feed_once).
        start = (
            (utcnow() - timedelta(seconds=lookback_seconds(timeframe, limit))).date().isoformat()
        )
        df = self._bp.Ticker(symbol).history(
            start=start, interval=interval, adjust=adjust, auto_adjust=False
        )

        index = list(df.index)
        opens = self._column(df, "Open")
        highs = self._column(df, "High")
        lows = self._column(df, "Low")
        closes = self._column(df, "Close")
        volumes = self._column(df, "Volume", default=0.0, length=len(index))

        rows: list[list[Any]] = []
        for i in range(len(index)):
            o, h, low, c = opens[i], highs[i], lows[i], closes[i]
            if any(_is_nan(x) for x in (o, h, low, c)):  # skip missing/partial candles
                continue
            vol = volumes[i]
            if _is_nan(vol):  # index volume can be NaN → 0
                vol = 0.0
            rows.append([to_utc(index[i]), o, h, low, c, vol])
        return rows[-limit:]

    @staticmethod
    def _column(df: Any, name: str, *, default: float | None = None, length: int = 0) -> list[Any]:
        for candidate in (name, name.lower()):
            if candidate in df.columns:
                values: list[Any] = df[candidate].tolist()
                return values
        if default is not None:
            return [default] * length
        raise AdapterError(f"OHLCV column not found: {name}")


def borsapy_provider() -> BorsapyProvider:
    """Build a live :class:`BorsapyProvider` (borsapy is lazily imported)."""
    return BorsapyProvider()


def bist_adapter(*, confidence: float = 0.9) -> TRAdapter:
    """Build a ``TRAdapter`` backed by the live borsapy provider."""
    from argus.ingestion.tr import TRAdapter

    return TRAdapter(BorsapyProvider(), source="borsapy", confidence=confidence)
