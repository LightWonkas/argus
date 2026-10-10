"""1m/5m/15m/1h closed-candle analysis and PIT alignment."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import Timeframe
from argus.core.intelligence import MultiTimeframeAnalysis, TimeframeAnalysis
from argus.core.schemas import DataStatus, utcnow

REQUIRED_INTRADAY_TIMEFRAMES = (
    Timeframe.M1,
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.H1,
)


def closed_bars(series: OHLCVSeries, *, as_of: datetime) -> tuple[OHLCVBar, ...]:
    """Treats the bar timestamp as its open time; returns only bars that have closed."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be tz-aware")
    close_delay = timedelta(seconds=series.timeframe.seconds)
    return tuple(bar for bar in series.bars if bar.timestamp + close_delay <= as_of)


def _realized_volatility_pct(bars: tuple[OHLCVBar, ...]) -> Decimal:
    returns = [(right.close / left.close) - Decimal("1") for left, right in pairwise(bars)]
    if not returns:
        return Decimal("0")
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal("0")) / Decimal(len(returns))
    with localcontext() as context:
        context.prec = 28
        return variance.sqrt() * Decimal("100")


def analyze_closed_timeframe(
    series: OHLCVSeries,
    *,
    as_of: datetime | None = None,
    lookback: int = 30,
) -> TimeframeAnalysis:
    at = as_of or utcnow()
    if lookback < 3:
        raise ValueError("intraday lookback must be at least 3")
    usable = closed_bars(series, as_of=at)[-lookback:]
    excluded = len(series.bars) - len(closed_bars(series, as_of=at))
    if len(usable) < 3:
        return TimeframeAnalysis(
            timeframe=series.timeframe,
            as_of=at,
            bars_used=len(usable),
            provisional_bars_excluded=excluded,
            data_status=DataStatus.PENDING,
        )
    first, last = usable[0], usable[-1]
    return_bps = (last.close / first.close - Decimal("1")) * Decimal("10000")
    previous_volume = sum((bar.volume for bar in usable[:-1]), Decimal("0")) / Decimal(
        len(usable) - 1
    )
    volume_ratio = last.volume / previous_volume if previous_volume > 0 else Decimal("0")
    total_volume = sum((bar.volume for bar in usable), Decimal("0"))
    vwap_proxy = (
        sum((bar.close * bar.volume for bar in usable), Decimal("0")) / total_volume
        if total_volume > 0
        else last.close
    )
    flat_threshold = Decimal("5")
    trend = (
        "up"
        if return_bps > flat_threshold
        else ("down" if return_bps < -flat_threshold else "flat")
    )
    return TimeframeAnalysis(
        timeframe=series.timeframe,
        as_of=at,
        bars_used=len(usable),
        provisional_bars_excluded=excluded,
        close_price=last.close,
        return_bps=return_bps,
        volume_ratio=volume_ratio,
        realized_volatility_pct=_realized_volatility_pct(usable),
        vwap_proxy=vwap_proxy,
        trend=trend,
        data_status=DataStatus.OK,
    )


def analyze_intraday_timeframes(
    symbol: str,
    series_by_timeframe: Mapping[Timeframe, OHLCVSeries],
    *,
    as_of: datetime | None = None,
    lookback: int = 30,
) -> MultiTimeframeAnalysis:
    at = as_of or utcnow()
    analyses: dict[Timeframe, TimeframeAnalysis] = {}
    missing: list[str] = []
    for timeframe in REQUIRED_INTRADAY_TIMEFRAMES:
        series = series_by_timeframe.get(timeframe)
        if series is None:
            missing.append(timeframe.value)
            continue
        if series.symbol.upper() != symbol.upper():
            raise ValueError("multi-timeframe series must belong to the same symbol")
        analysis = analyze_closed_timeframe(series, as_of=at, lookback=lookback)
        analyses[timeframe] = analysis
        if analysis.data_status is not DataStatus.OK:
            missing.append(f"{timeframe.value}:insufficient_closed_bars")
    aligned = not missing and len(analyses) == len(REQUIRED_INTRADAY_TIMEFRAMES)
    return MultiTimeframeAnalysis(
        symbol=symbol.upper(),
        as_of=at,
        analyses=analyses,
        aligned=aligned,
        missing_data=tuple(missing),
        data_status=DataStatus.OK if aligned else DataStatus.PENDING,
    )


__all__ = [
    "REQUIRED_INTRADAY_TIMEFRAMES",
    "analyze_closed_timeframe",
    "analyze_intraday_timeframes",
    "closed_bars",
]
