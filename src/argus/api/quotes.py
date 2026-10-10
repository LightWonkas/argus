"""Quote/indicator row construction — pure, network-free (cockpit watchlist table).

**Collects no new data**: only derives last price + RSI/MACD/ATR + freshness from the OHLCV
stored in ``SqliteStore``. If the series is missing or not warmed up the field is ``None``
(shown as DATA PENDING in the UI).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from argus.config.models import WatchlistItem
from argus.core.contracts import OHLCVSeries
from argus.core.schemas import utcnow
from argus.platform.indicators import atr, macd, ohlcv_floats, rsi

# Freshness: how many candle durations old the last candle may be before it counts as
# "stale" (in the spirit of the Data Quality Gate).
_STALE_FACTOR = 3.0


@dataclass(frozen=True)
class QuoteRow:
    """One row of the watchlist table — price + indicators + freshness (secret-free)."""

    symbol: str
    asset_class: str
    tier: str
    price: float | None
    change_pct_24h: float | None
    rsi: float | None
    macd: float | None
    macd_signal: float | None
    atr: float | None
    freshness: str  # "fresh" | "stale" | "none"
    bar_time: datetime | None
    bar_count: int
    # Timeframes defined in config for this symbol (the chart selector offers only these —
    # no fake options; empty config → just the loop default).
    timeframes: tuple[str, ...] = ()


def _freshness(series: OHLCVSeries, *, now: datetime) -> str:
    """Freshness label from the last candle's age (_STALE_FACTOR × candle duration → stale)."""
    last = series.latest
    if last is None:
        return "none"
    age_s = (now - last.timestamp).total_seconds()
    return "fresh" if age_s <= series.timeframe.seconds * _STALE_FACTOR else "stale"


def build_quote_row(
    item: WatchlistItem,
    series: OHLCVSeries | None,
    *,
    now: datetime | None = None,
    default_timeframe: str = "1d",
) -> QuoteRow:
    """Builds a QuoteRow from a watchlist item + its OHLCV series (if any).

    If ``series`` is None or empty, price/indicators are None and freshness is ``none``
    (DATA PENDING). Indicators that are not warmed up (too few candles) are None — nothing
    is made up. ``default_timeframe``: the single TF offered in the selector when the item's
    config has no TFs (the loop default).
    """
    now = now or utcnow()
    timeframes = tuple(item.timeframes) or (default_timeframe,)
    if series is None or not series.bars:
        return QuoteRow(
            symbol=item.symbol,
            asset_class=item.asset_class.value,
            tier=str(item.tier),
            price=None,
            change_pct_24h=None,
            rsi=None,
            macd=None,
            macd_signal=None,
            atr=None,
            freshness="none",
            bar_time=None,
            bar_count=0,
            timeframes=timeframes,
        )

    highs, lows, closes = ohlcv_floats(series)
    last_close = closes[-1]

    # 24h change: look back ~one day's worth of candles (derived from candle duration).
    bars_per_day = max(1, 86_400 // series.timeframe.seconds)
    change: float | None = None
    if len(closes) > bars_per_day:
        ref = closes[-1 - bars_per_day]
        if ref:
            change = (last_close - ref) / ref * 100

    rsi_v = rsi(closes)[-1] if closes else None
    macd_line, signal_line, _ = macd(closes)
    atr_v = atr(highs, lows, closes)[-1] if closes else None

    return QuoteRow(
        symbol=item.symbol,
        asset_class=item.asset_class.value,
        tier=str(item.tier),
        price=last_close,
        change_pct_24h=change,
        rsi=rsi_v,
        macd=macd_line[-1],
        macd_signal=signal_line[-1],
        atr=atr_v,
        freshness=_freshness(series, now=now),
        bar_time=series.latest.timestamp if series.latest else None,
        bar_count=len(series.bars),
        timeframes=timeframes,
    )
