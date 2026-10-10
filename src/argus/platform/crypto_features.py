"""Deterministic crypto feature and market-regime layer.

No value is produced by an LLM. Missing data is carried explicitly as ``None`` and
``missing_data``. CVD is defined here as aggressive buy quantity minus aggressive sell
quantity; this is an approximation of the observed trade flow, not real order-book volume.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from decimal import Decimal

from argus.core.contracts import OHLCVSeries
from argus.core.crypto import (
    BasisSnapshot,
    FeatureSnapshot,
    FundingRate,
    LiquidationEvent,
    LongShortRatio,
    MarketRegime,
    OpenInterest,
    OrderBookLevel,
    OrderBookSnapshot,
    Trade,
)
from argus.core.enums import MarketRegimeKind, OrderSide, Timeframe
from argus.core.schemas import DataStatus
from argus.core.trading import TradeRollup1m
from argus.platform.indicators import atr, ema, macd, ohlcv_floats, rsi


def _last(values: list[float | None]) -> float | None:
    return values[-1] if values else None


def _pct_change(current: Decimal | None, previous: Decimal | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return float((current - previous) / previous * Decimal("100"))


def _realized_volatility(closes: list[float], timeframe: Timeframe, window: int) -> float | None:
    if len(closes) <= window:
        return None
    returns = [
        math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes))
    ]
    periods_per_year = (365 * 24 * 3600) / timeframe.seconds
    return statistics.pstdev(returns) * math.sqrt(periods_per_year) * 100


def _zscore(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    sample = values[-window:]
    deviation = statistics.pstdev(sample)
    if deviation == 0:
        return 0.0
    return (sample[-1] - statistics.fmean(sample)) / deviation


def _book_imbalance(book: OrderBookSnapshot, depth: int) -> float | None:
    bid_qty = sum((level.quantity for level in book.bids[:depth]), Decimal("0"))
    ask_qty = sum((level.quantity for level in book.asks[:depth]), Decimal("0"))
    total = bid_qty + ask_qty
    return float((bid_qty - ask_qty) / total) if total > 0 else None


def _max_gap_bps(levels: tuple[OrderBookLevel, ...]) -> float | None:
    prices = [level.price for level in levels if level.quantity > 0]
    if len(prices) < 2:
        return None
    gaps = [
        abs(float(prices[i] - prices[i - 1])) / float(prices[i - 1]) * 10_000
        for i in range(1, len(prices))
    ]
    return max(gaps)


def _wall_prices(book: OrderBookSnapshot, multiplier: float = 3.0) -> set[Decimal]:
    levels = [level for level in (*book.bids, *book.asks) if level.quantity > 0]
    if len(levels) < 3:
        return set()
    median = statistics.median(float(level.quantity) for level in levels)
    return {level.price for level in levels if float(level.quantity) >= median * multiplier}


def _persistent_walls(history: tuple[OrderBookSnapshot, ...]) -> int | None:
    if len(history) < 2:
        return None
    common = _wall_prices(history[0])
    for snapshot in history[1:]:
        common &= _wall_prices(snapshot)
    return len(common)


def _trend_sign(series: OHLCVSeries, fast: int, slow: int) -> int | None:
    closes = [float(bar.close) for bar in series.bars]
    fast_value = _last(ema(closes, fast))
    slow_value = _last(ema(closes, slow))
    if fast_value is None or slow_value is None:
        return None
    return 1 if fast_value > slow_value else -1 if fast_value < slow_value else 0


def _return_pct(values: list[float], window: int) -> float | None:
    if len(values) <= window or values[-window - 1] == 0:
        return None
    return (values[-1] / values[-window - 1] - 1) * 100


def _correlation(left: list[float], right: list[float], window: int = 30) -> float | None:
    count = min(len(left), len(right), window + 1)
    if count < 3:
        return None
    left_returns = [left[i] / left[i - 1] - 1 for i in range(len(left) - count + 1, len(left))]
    right_returns = [right[i] / right[i - 1] - 1 for i in range(len(right) - count + 1, len(right))]
    if statistics.pstdev(left_returns) == 0 or statistics.pstdev(right_returns) == 0:
        return None
    return statistics.correlation(left_returns, right_returns)


def compute_crypto_features(
    series: OHLCVSeries,
    *,
    ema_fast: int = 12,
    ema_slow: int = 26,
    rsi_period: int = 14,
    atr_period: int = 14,
    realized_vol_window: int = 24,
    volume_zscore_window: int = 30,
    breakout_lookback: int = 20,
    squeeze_funding_abs: Decimal = Decimal("0.001"),
    order_book_depths: tuple[int, ...] = (5, 10, 25),
    higher_timeframes: Mapping[Timeframe, OHLCVSeries] | None = None,
    btc_series: OHLCVSeries | None = None,
    funding: FundingRate | None = None,
    previous_funding: FundingRate | None = None,
    open_interest: OpenInterest | None = None,
    previous_open_interest: OpenInterest | None = None,
    basis: BasisSnapshot | None = None,
    long_short_ratio: LongShortRatio | None = None,
    order_book: OrderBookSnapshot | None = None,
    book_history: tuple[OrderBookSnapshot, ...] = (),
    trades: tuple[Trade, ...] = (),
    trade_rollups: tuple[TradeRollup1m, ...] = (),
    liquidations: tuple[LiquidationEvent, ...] = (),
) -> FeatureSnapshot:
    """Builds a single point-in-time feature snapshot from validated inputs."""
    values: dict[str, Decimal | float | int | bool | str | None] = {}
    evidence: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    highs, lows, closes = ohlcv_floats(series)
    volumes = [float(bar.volume) for bar in series.bars]
    as_of = series.event_timestamp

    if closes:
        fast_value = _last(ema(closes, ema_fast))
        slow_value = _last(ema(closes, ema_slow))
        rsi_value = _last(rsi(closes, rsi_period))
        macd_line, macd_signal, macd_histogram = macd(closes, ema_fast, ema_slow)
        atr_value = _last(atr(highs, lows, closes, atr_period))
        values.update(
            {
                "ema_fast": fast_value,
                "ema_slow": slow_value,
                "ema_trend": (
                    "up"
                    if fast_value is not None and slow_value is not None and fast_value > slow_value
                    else "down"
                    if fast_value is not None and slow_value is not None and fast_value < slow_value
                    else "flat"
                    if fast_value is not None and slow_value is not None
                    else None
                ),
                "rsi": rsi_value,
                "macd": _last(macd_line),
                "macd_signal": _last(macd_signal),
                "macd_histogram": _last(macd_histogram),
                "atr": atr_value,
                "normalized_atr_pct": (
                    atr_value / closes[-1] * 100
                    if atr_value is not None and closes[-1] > 0
                    else None
                ),
                "realized_volatility_annualized_pct": _realized_volatility(
                    closes, series.timeframe, realized_vol_window
                ),
                "volume_zscore": _zscore(volumes, volume_zscore_window),
                "momentum_10_pct": _return_pct(closes, 10),
            }
        )
        vwap_denominator = sum(volumes)
        values["vwap"] = (
            sum(
                ((float(bar.high) + float(bar.low) + float(bar.close)) / 3) * float(bar.volume)
                for bar in series.bars
            )
            / vwap_denominator
            if vwap_denominator > 0
            else None
        )
        bollinger_window = 20
        if len(closes) >= bollinger_window:
            bollinger_sample = closes[-bollinger_window:]
            middle = statistics.fmean(bollinger_sample)
            deviation = statistics.pstdev(bollinger_sample)
            values.update(
                {
                    "bollinger_middle": middle,
                    "bollinger_upper": middle + 2 * deviation,
                    "bollinger_lower": middle - 2 * deviation,
                    "bollinger_width_pct": (4 * deviation / middle * 100 if middle > 0 else None),
                }
            )
        else:
            values.update(
                {
                    "bollinger_middle": None,
                    "bollinger_upper": None,
                    "bollinger_lower": None,
                    "bollinger_width_pct": None,
                }
            )
            missing.append("bollinger_warmup")
        evidence["technical"] = (f"ohlcv:{series.symbol}:{series.timeframe.value}",)

        if len(series.bars) > breakout_lookback:
            prior = series.bars[-breakout_lookback - 1 : -1]
            resistance = max(bar.high for bar in prior)
            support = min(bar.low for bar in prior)
            last_close = series.bars[-1].close
            values["support"] = support
            values["resistance"] = resistance
            values["range_position"] = (
                float((last_close - support) / (resistance - support))
                if resistance > support
                else None
            )
            values["breakout"] = (
                "up" if last_close > resistance else "down" if last_close < support else "range"
            )
            previous_close = series.bars[-2].close if len(series.bars) >= 2 else None
            values["false_breakout"] = bool(
                previous_close is not None
                and (previous_close > resistance or previous_close < support)
                and support <= last_close <= resistance
            )
        else:
            missing.append("breakout_history")
            values.update(
                {"support": None, "resistance": None, "range_position": None, "breakout": None}
            )
            values["false_breakout"] = None
    else:
        missing.append("ohlcv")

    timeframe_series = {series.timeframe: series, **dict(higher_timeframes or {})}
    signs = [
        sign
        for item in timeframe_series.values()
        if (sign := _trend_sign(item, ema_fast, ema_slow)) is not None
    ]
    values["mtf_alignment"] = abs(sum(signs)) / len(signs) if signs else None
    values["mtf_direction"] = (
        "up"
        if signs and sum(signs) > 0
        else "down"
        if signs and sum(signs) < 0
        else "mixed"
        if signs
        else None
    )
    if not signs:
        missing.append("multi_timeframe_warmup")

    if btc_series is not None and btc_series.bars:
        btc_closes = [float(bar.close) for bar in btc_series.bars]
        coin_return = _return_pct(closes, 20)
        btc_return = _return_pct(btc_closes, 20)
        values["btc_correlation"] = _correlation(closes, btc_closes)
        values["relative_strength_vs_btc_pct"] = (
            coin_return - btc_return if coin_return is not None and btc_return is not None else None
        )
        evidence["btc_benchmark"] = (btc_series.provenance.source,)
    elif series.symbol.upper() == "BTCUSDT":
        values["btc_correlation"] = 1.0
        values["relative_strength_vs_btc_pct"] = 0.0
    else:
        values["btc_correlation"] = None
        values["relative_strength_vs_btc_pct"] = None
        missing.append("btc_benchmark")

    values["funding_rate"] = funding.rate if funding else None
    values["funding_change"] = (
        funding.rate - previous_funding.rate if funding and previous_funding else None
    )
    values["open_interest"] = open_interest.contracts if open_interest else None
    values["open_interest_change_pct"] = _pct_change(
        open_interest.contracts if open_interest else None,
        previous_open_interest.contracts if previous_open_interest else None,
    )
    values["basis_pct"] = basis.basis_pct if basis else None
    values["long_short_ratio"] = long_short_ratio.account_ratio if long_short_ratio else None
    for name, present in (
        ("funding", funding is not None),
        ("open_interest", open_interest is not None),
        ("basis", basis is not None),
        ("long_short_ratio", long_short_ratio is not None),
    ):
        if not present:
            missing.append(name)
    if any(item is not None for item in (funding, open_interest, basis, long_short_ratio)):
        evidence["derivatives"] = tuple(
            item.source
            for item in (funding, open_interest, basis, long_short_ratio)
            if item is not None
        )

    price_change = (
        (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 and closes[-2] else None
    )
    oi_change = values.get("open_interest_change_pct")
    values["price_oi_divergence"] = (
        bool(price_change * float(oi_change) < 0)
        if price_change is not None and isinstance(oi_change, (float, int))
        else None
    )
    values["squeeze_risk"] = bool(
        funding
        and abs(funding.rate) >= squeeze_funding_abs
        and isinstance(oi_change, (float, int))
        and oi_change > 5
    )

    if order_book is None:
        missing.append("order_book")
        values.update(
            {
                "spread_bps": None,
                "liquidity_gap_bps": None,
                "persistent_wall_count": None,
                "spoofing_suspicion": "insufficient_evidence",
            }
        )
    else:
        best_bid, best_ask = order_book.best_bid, order_book.best_ask
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
            values["spread_bps"] = float((best_ask - best_bid) / mid * 10_000)
        else:
            values["spread_bps"] = None
        for depth in order_book_depths:
            values[f"book_imbalance_{depth}"] = _book_imbalance(order_book, depth)
        bid_gap = _max_gap_bps(order_book.bids)
        ask_gap = _max_gap_bps(order_book.asks)
        values["liquidity_gap_bps"] = max(
            (gap for gap in (bid_gap, ask_gap) if gap is not None), default=None
        )
        persistence_history = book_history or (order_book,)
        persistent = _persistent_walls(persistence_history)
        values["persistent_wall_count"] = persistent
        values["spoofing_suspicion"] = "insufficient_evidence" if persistent is None else "low"
        evidence["order_book"] = (order_book.source,)

    if trades:
        buy_qty = sum(
            (trade.quantity for trade in trades if trade.side is OrderSide.BUY), Decimal("0")
        )
        sell_qty = sum(
            (trade.quantity for trade in trades if trade.side is OrderSide.SELL), Decimal("0")
        )
        trade_sources = tuple(sorted({trade.source for trade in trades}))
    else:
        buy_qty = sum((row.buy_quantity for row in trade_rollups), Decimal("0"))
        sell_qty = sum((row.sell_quantity for row in trade_rollups), Decimal("0"))
        trade_sources = ("sqlite:trade-rollup-1m",) if trade_rollups else ()
    total_qty = buy_qty + sell_qty
    values["aggressive_buy_ratio"] = float(buy_qty / total_qty) if total_qty else None
    values["cvd_base"] = buy_qty - sell_qty if trades or trade_rollups else None
    if not trades and not trade_rollups:
        missing.append("trades")
    else:
        evidence["trade_flow"] = trade_sources

    long_liq = sum(
        (event.notional for event in liquidations if event.side is OrderSide.SELL), Decimal("0")
    )
    short_liq = sum(
        (event.notional for event in liquidations if event.side is OrderSide.BUY), Decimal("0")
    )
    values["long_liquidation_notional"] = long_liq if liquidations else None
    values["short_liquidation_notional"] = short_liq if liquidations else None
    values["liquidation_notional"] = long_liq + short_liq if liquidations else None
    if not liquidations:
        missing.append("liquidations")
    else:
        evidence["liquidations"] = tuple(sorted({event.source for event in liquidations}))

    status = DataStatus.OK if closes else DataStatus.PENDING
    return FeatureSnapshot(
        symbol=series.symbol,
        timeframe=series.timeframe,
        as_of=as_of,
        values=values,
        evidence_sources=evidence,
        missing_data=tuple(dict.fromkeys(missing)),
        data_status=status,
    )


def classify_market_regime(
    features: FeatureSnapshot,
    *,
    high_volatility_natr_pct: float = 4.0,
    low_liquidity_spread_bps: float = 20.0,
    event_risk: bool = False,
) -> MarketRegime:
    """Single dominant regime from a feature snapshot; used/missing features are explicit."""
    values = features.values
    spread_bps = values.get("spread_bps")
    normalized_atr = values.get("normalized_atr_pct")
    used: list[str] = []
    regime = MarketRegimeKind.UNKNOWN
    confidence = 0

    if event_risk:
        regime, confidence = MarketRegimeKind.EVENT_RISK, 85
        used.append("event_risk")
    elif isinstance(spread_bps, (int, float)) and spread_bps >= low_liquidity_spread_bps:
        regime, confidence = MarketRegimeKind.LOW_LIQUIDITY, 85
        used.append("spread_bps")
    elif isinstance(normalized_atr, (int, float)) and normalized_atr >= high_volatility_natr_pct:
        regime, confidence = MarketRegimeKind.HIGH_VOLATILITY, 80
        used.append("normalized_atr_pct")
    elif values.get("squeeze_risk") is True:
        regime, confidence = MarketRegimeKind.SQUEEZE_RISK, 80
        used.extend(("funding_rate", "open_interest_change_pct"))
    elif values.get("ema_trend") == "up" and values.get("mtf_direction") == "up":
        regime, confidence = MarketRegimeKind.TREND_UP, 75
        used.extend(("ema_trend", "mtf_direction"))
    elif values.get("ema_trend") == "down" and values.get("mtf_direction") == "down":
        regime, confidence = MarketRegimeKind.TREND_DOWN, 75
        used.extend(("ema_trend", "mtf_direction"))
    elif values.get("breakout") == "range":
        regime, confidence = MarketRegimeKind.RANGE, 65
        used.extend(("breakout", "range_position"))

    missing = tuple(
        name
        for name in ("normalized_atr_pct", "spread_bps", "ema_trend", "mtf_direction")
        if values.get(name) is None
    )
    return MarketRegime(
        symbol=features.symbol,
        as_of=features.as_of,
        regime=regime,
        confidence_pct=confidence,
        features_used=tuple(used),
        missing_data=missing,
        data_status=features.data_status,
    )
