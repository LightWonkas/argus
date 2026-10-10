"""Deterministically classifies the PIT price/volume reaction to a news item."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise

from argus.core.contracts import OHLCVBar
from argus.core.enums import Timeframe
from argus.core.intelligence import (
    CryptoNewsRecord,
    MarketReactionStatus,
    NewsMarketReaction,
    NewsSentiment,
)
from argus.core.schemas import DataStatus, utcnow


class NewsMarketReactionAnalyzer:
    def __init__(
        self,
        *,
        already_priced_before_bps: Decimal = Decimal("300"),
        meaningful_after_bps: Decimal = Decimal("50"),
        pump_move_bps: Decimal = Decimal("500"),
        pump_volume_ratio: Decimal = Decimal("3"),
    ) -> None:
        self.already_priced_before_bps = already_priced_before_bps
        self.meaningful_after_bps = meaningful_after_bps
        self.pump_move_bps = pump_move_bps
        self.pump_volume_ratio = pump_volume_ratio

    @staticmethod
    def _move(start: Decimal, end: Decimal) -> Decimal:
        return (end / start - Decimal("1")) * Decimal("10000")

    @staticmethod
    def _volatility(bars: Sequence[OHLCVBar]) -> Decimal | None:
        values = [right.close / left.close - Decimal("1") for left, right in pairwise(bars)]
        if not values:
            return None
        mean = sum(values, Decimal("0")) / Decimal(len(values))
        variance = sum(((item - mean) ** 2 for item in values), Decimal("0")) / Decimal(len(values))
        with localcontext() as context:
            context.prec = 28
            return variance.sqrt() * Decimal("100")

    def analyze(
        self,
        news: CryptoNewsRecord,
        *,
        coin_closed_bars: Sequence[OHLCVBar],
        btc_closed_bars: Sequence[OHLCVBar] = (),
        spread_before_bps: Decimal | None = None,
        spread_after_bps: Decimal | None = None,
        depth_before_quote: Decimal | None = None,
        depth_after_quote: Decimal | None = None,
        order_book_imbalance_before: Decimal | None = None,
        order_book_imbalance_after: Decimal | None = None,
        open_interest_before: Decimal | None = None,
        open_interest_after: Decimal | None = None,
        funding_before: Decimal | None = None,
        funding_after: Decimal | None = None,
        estimated_slippage_bps: Decimal | None = None,
        bar_timeframe: Timeframe = Timeframe.M1,
        as_of: datetime | None = None,
    ) -> NewsMarketReaction:
        at = as_of or utcnow()
        if at.tzinfo is None:
            raise ValueError("reaction as_of must be tz-aware")
        # First-seen time is the backtest knowledge boundary; publish time proves no access.
        if news.first_seen_at > at:
            return self._no_data(news, at, "news has not been seen by Argus yet")
        close_delay = timedelta(seconds=bar_timeframe.seconds)
        visible = sorted(
            (bar for bar in coin_closed_bars if bar.timestamp + close_delay <= at),
            key=lambda bar: bar.timestamp,
        )
        before = [bar for bar in visible if bar.timestamp + close_delay <= news.first_seen_at]
        after = [bar for bar in visible if bar.timestamp >= news.first_seen_at]
        if len(before) < 2 or not after:
            return self._no_data(news, at, "not enough closed candles before/after the news")
        price_before = self._move(before[0].close, before[-1].close)
        price_after = self._move(before[-1].close, after[-1].close)
        baseline_volume = sum((bar.volume for bar in before), Decimal("0")) / Decimal(len(before))
        after_volume = sum((bar.volume for bar in after), Decimal("0")) / Decimal(len(after))
        volume_ratio = after_volume / baseline_volume if baseline_volume > 0 else None
        spread_change = (
            spread_after_bps - spread_before_bps
            if spread_before_bps is not None and spread_after_bps is not None
            else None
        )
        depth_change = (
            (depth_after_quote / depth_before_quote - Decimal("1")) * Decimal("100")
            if depth_before_quote is not None
            and depth_after_quote is not None
            and depth_before_quote > 0
            else None
        )
        btc_after = [
            bar
            for bar in btc_closed_bars
            if news.first_seen_at <= bar.timestamp and bar.timestamp + close_delay <= at
        ]
        btc_before = [
            bar for bar in btc_closed_bars if bar.timestamp + close_delay <= news.first_seen_at
        ]
        relative = None
        if btc_before and btc_after:
            relative = price_after - self._move(btc_before[-1].close, btc_after[-1].close)

        open_interest_change = (
            (open_interest_after / open_interest_before - Decimal("1")) * Decimal("100")
            if open_interest_before is not None
            and open_interest_after is not None
            and open_interest_before > 0
            else None
        )
        funding_change = (
            (funding_after - funding_before) * Decimal("10000")
            if funding_before is not None and funding_after is not None
            else None
        )
        extreme = max((abs(self._move(before[-1].close, bar.close)) for bar in after), default=None)
        reversal = max(Decimal("0"), extreme - abs(price_after)) if extreme is not None else None

        reasons: list[str] = []
        status: MarketReactionStatus
        adverse = (
            news.sentiment is NewsSentiment.POSITIVE and price_after <= -self.meaningful_after_bps
        ) or (news.sentiment is NewsSentiment.NEGATIVE and price_after >= self.meaningful_after_bps)
        if (
            reversal is not None
            and extreme is not None
            and reversal >= self.meaningful_after_bps * Decimal("2")
            and extreme >= self.meaningful_after_bps * Decimal("3")
        ):
            status = MarketReactionStatus.REVERSAL_RISK
            reasons.append("marked reversal from the initial reaction extreme")
        elif (
            abs(price_after) >= self.pump_move_bps
            and volume_ratio is not None
            and volume_ratio >= self.pump_volume_ratio
        ):
            if (spread_change is not None and spread_change > 0) or (
                depth_change is not None and depth_change < 0
            ):
                status = MarketReactionStatus.OVERREACTION
                reasons.append("spread or liquidity deteriorated while price/volume expanded")
            else:
                status = MarketReactionStatus.PUMP_RISK
                reasons.append("abrupt price and volume expansion: chase/pump risk")
        elif adverse:
            status = MarketReactionStatus.ADVERSE_REACTION
            reasons.append("price reaction contradicts the news direction")
        elif abs(price_before) >= self.already_priced_before_bps and abs(price_after) < abs(
            price_before
        ) / Decimal("2"):
            status = MarketReactionStatus.ALREADY_PRICED
            reasons.append("most of the move happened before the news was first seen")
        elif abs(price_after) < self.meaningful_after_bps:
            status = MarketReactionStatus.NOT_PRICED
            reasons.append("no meaningful move after the news yet")
        elif relative is None:
            status = MarketReactionStatus.INCONCLUSIVE
            reasons.append("no BTC relative-performance data")
        else:
            status = MarketReactionStatus.PARTIALLY_PRICED
            reasons.append("measurable but not excessive reaction after the news")
        blocks_entry = status in {
            MarketReactionStatus.ALREADY_PRICED,
            MarketReactionStatus.PUMP_RISK,
            MarketReactionStatus.INCONCLUSIVE,
            MarketReactionStatus.ADVERSE_REACTION,
            MarketReactionStatus.NO_DATA,
            MarketReactionStatus.OVERREACTION,
            MarketReactionStatus.REVERSAL_RISK,
            MarketReactionStatus.INSUFFICIENT_DATA,
        }
        age_minutes = max(0, int((at - news.first_seen_at).total_seconds() // 60))
        ttl_minutes = max(0, int((news.expires_at - news.first_seen_at).total_seconds() // 60))
        return NewsMarketReaction(
            news_id=news.news_id,
            symbol=news.related_symbols[0] if news.related_symbols else "UNKNOWN",
            as_of=at,
            status=status,
            price_move_before_bps=price_before,
            price_move_after_bps=price_after,
            volume_ratio=volume_ratio,
            spread_change_bps=spread_change,
            depth_change_pct=depth_change,
            order_book_imbalance_before=order_book_imbalance_before,
            order_book_imbalance_after=order_book_imbalance_after,
            open_interest_change_pct=open_interest_change,
            funding_change_bps=funding_change,
            relative_to_btc_bps=relative,
            realized_volatility_pct=self._volatility(after),
            reversal_from_extreme_bps=reversal,
            estimated_slippage_bps=estimated_slippage_bps,
            freshness_seconds=max(0, int((at - news.first_seen_at).total_seconds())),
            remaining_effect_minutes=max(0, ttl_minutes - age_minutes),
            blocks_entry=blocks_entry,
            reasons=tuple(reasons),
            data_status=DataStatus.OK,
        )

    @staticmethod
    def _no_data(news: CryptoNewsRecord, at: datetime, reason: str) -> NewsMarketReaction:
        return NewsMarketReaction(
            news_id=news.news_id,
            symbol=news.related_symbols[0] if news.related_symbols else "UNKNOWN",
            as_of=at,
            status=MarketReactionStatus.NO_DATA,
            blocks_entry=True,
            reasons=(reason,),
            data_status=DataStatus.PENDING,
        )


__all__ = ["NewsMarketReactionAnalyzer"]
