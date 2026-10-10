"""Links recorded news + closed-candle chart evidence to PIT research/TradePlan."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal

from argus.config.models import RuntimeConfig
from argus.core.contracts import OHLCVSeries
from argus.core.crypto import FeatureSnapshot, MarketRegime, SpotTicker
from argus.core.enums import MarketRegimeKind, Timeframe
from argus.core.intelligence import (
    CoinResearchResult,
    CostSource,
    CryptoNewsRecord,
    MarketReactionStatus,
    NewsCoverageStatus,
    NewsEventType,
    NewsSentiment,
    NewsVerificationStatus,
    TradePlan,
    TradeStrategyBasis,
)
from argus.core.schemas import DataStatus
from argus.core.trading import MissionStatus, TradingMode
from argus.decision.trade_planner import MultiHorizonTradePlanner
from argus.platform.intraday_features import analyze_intraday_timeframes
from argus.platform.news_reaction import NewsMarketReactionAnalyzer
from argus.storage.sqlite import SqliteStore


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    return None


class IntradayResearchCoordinator:
    """Its only side effect is a PIT record; it creates no OrderIntent or exchange call."""

    def __init__(self, store: SqliteStore, config: RuntimeConfig) -> None:
        self.store = store
        self.config = config
        planner = config.intraday_planner
        self.planner = MultiHorizonTradePlanner(
            minimum_confidence=Decimal(str(planner.minimum_confidence)),
            minimum_news_reliability=Decimal(str(planner.minimum_news_reliability)),
            maximum_manipulation_risk=Decimal(str(planner.maximum_manipulation_risk)),
            minimum_required_net_edge_bps=Decimal(str(config.fees.minimum_net_edge_bps)),
            maximum_holding_minutes=planner.maximum_holding_minutes,
            plan_validity_seconds=planner.plan_validity_seconds,
            reevaluation_cooldown_seconds=planner.reevaluation_cooldown_seconds,
            meaningful_edge_change_bps=Decimal(str(planner.meaningful_edge_change_bps)),
        )

    def process(
        self,
        *,
        symbol: str,
        as_of: datetime,
        spot: SpotTicker,
        features: FeatureSnapshot,
        regime: MarketRegime,
        series_by_timeframe: Mapping[Timeframe, OHLCVSeries],
    ) -> tuple[CoinResearchResult | None, TradePlan | None]:
        canonical = symbol.upper()
        coverage = self._coverage_status(canonical)
        # A symbol that just entered the dynamic universe may be absent from the current
        # checkpoint until the next news scan. This expected PIT race is not a parse error,
        # and assuming NO_RELEVANT_NEWS would not be safe either. Until news coverage is
        # actually evaluated, close only the research/TradePlan branch.
        if coverage is None:
            return None, None
        multi = analyze_intraday_timeframes(canonical, series_by_timeframe, as_of=as_of)
        news = self._latest_usable_news(canonical, as_of=as_of)
        blocking_news = self._blocking_news(canonical, as_of=as_of)
        reaction = None
        if news is not None:
            primary = series_by_timeframe.get(Timeframe.M1)
            btc = primary if canonical == "BTCUSDT" else self._btc_m1()
            if primary is not None:
                reaction = NewsMarketReactionAnalyzer().analyze(
                    news,
                    coin_closed_bars=primary.bars,
                    btc_closed_bars=btc.bars if btc is not None else (),
                    bar_timeframe=Timeframe.M1,
                    as_of=as_of,
                )
                self.store.save_news_market_reaction(reaction)

        eligibility = next(
            (
                row
                for row in self.store.latest_universe_snapshots()
                if row.symbol.upper() == canonical
            ),
            None,
        )
        rejection: list[str] = []
        if eligibility is None or not eligibility.eligible:
            rejection.append("PIT universe eligibility missing/rejected")
        if multi.data_status is not DataStatus.OK:
            rejection.append("1m/5m/15m/1h closed-candle alignment is missing")

        unhealthy_coverage = coverage in {
            NewsCoverageStatus.PROVIDER_UNAVAILABLE,
            NewsCoverageStatus.PROVIDER_STALE,
            NewsCoverageStatus.NEWS_ERROR,
            NewsCoverageStatus.NEWS_CONFLICT,
        }
        if unhealthy_coverage:
            rejection.append(f"news coverage is not healthy enough for a plan: {coverage.value}")
        if blocking_news:
            rejection.append(
                "high-risk recent news: "
                + ",".join(sorted({item.event_type.value for item in blocking_news}))
            )

        positive_catalyst = (
            news is not None
            and news.sentiment is NewsSentiment.POSITIVE
            and reaction is not None
            and not reaction.blocks_entry
        )
        critical_source_gap = not self._critical_source_coverage()
        if positive_catalyst and critical_source_gap:
            rejection.append(
                "critical official source coverage is insufficient; news plan is fail-closed"
            )
        technical_up = (
            regime.regime is MarketRegimeKind.TREND_UP
            and sum(item.trend == "up" for item in multi.analyses.values()) >= 3
        )
        neutral_for_technical = news is None or news.sentiment in {
            NewsSentiment.NEUTRAL,
            NewsSentiment.UNKNOWN,
        }
        if (
            positive_catalyst
            and not critical_source_gap
            and not blocking_news
            and not unhealthy_coverage
        ):
            strategy_basis = TradeStrategyBasis.NEWS_CATALYST
        elif (
            coverage in {NewsCoverageStatus.NO_RELEVANT_NEWS, NewsCoverageStatus.RELEVANT_NEWS}
            and neutral_for_technical
            and technical_up
            and not blocking_news
        ):
            strategy_basis = TradeStrategyBasis.TECHNICAL_INTRADAY
        else:
            strategy_basis = TradeStrategyBasis.NO_TRADE
            if news is not None and (reaction is None or reaction.blocks_entry):
                rejection.append("price reaction to the news catalyst does not allow an entry")
            if neutral_for_technical and not technical_up:
                rejection.append("no strong multi-timeframe TREND_UP for technical intraday")

        commission = self.store.latest_commission_schedule(canonical)
        cost_source = CostSource.VERIFIED_ACCOUNT
        if commission is None or commission.taker_rate is None:
            if (
                self.config.trading.mode in {TradingMode.PUBLIC_MONITOR, TradingMode.SHADOW}
                and self.config.fees.allow_conservative_config_fallback
            ):
                cost_source = CostSource.CONSERVATIVE_ESTIMATE
            else:
                cost_source = CostSource.NO_DATA
                rejection.append("verified commission NO_DATA")

        spread = _decimal(features.values.get("spread_bps"))
        slippage = eligibility.estimated_slippage_bps if eligibility is not None else None
        if spread is None or slippage is None:
            rejection.append("spread/slippage estimate NO_DATA")
        technical_score = Decimal(regime.confidence_pct)
        news_reliability = news.source_reliability if news is not None else None
        manipulation = (
            Decimal("90")
            if reaction is not None and reaction.status is MarketReactionStatus.PUMP_RISK
            else (
                Decimal("10")
                if reaction is not None or coverage is NewsCoverageStatus.NO_RELEVANT_NEWS
                else None
            )
        )
        liquidity_score = (
            max(Decimal("0"), Decimal("100") - spread * Decimal("2"))
            if spread is not None
            else None
        )
        confidence = (
            min(technical_score, news.confidence)
            if strategy_basis is TradeStrategyBasis.NEWS_CATALYST and news is not None
            else technical_score
        )
        evidence = (
            *(
                f"market:{timeframe.value}:{analysis.trend}"
                for timeframe, analysis in multi.analyses.items()
            ),
            *((f"news:{news.news_id}",) if news is not None else ()),
            f"news_coverage:{coverage.value}",
        )
        valid_until = min(
            news.expires_at if news is not None else as_of + timedelta(minutes=5),
            as_of + timedelta(minutes=5),
        )
        result = CoinResearchResult(
            symbol=canonical,
            as_of=as_of,
            eligibility=eligibility is not None and eligibility.eligible,
            strategy_basis=strategy_basis,
            news_coverage_status=coverage,
            market_regime=regime.regime,
            technical_score=technical_score,
            news_score=news.impact_score if news is not None else None,
            news_reliability=news_reliability,
            market_reaction=(
                reaction.status if reaction is not None else MarketReactionStatus.NO_DATA
            ),
            liquidity_score=liquidity_score,
            manipulation_risk=manipulation,
            execution_quality=liquidity_score,
            confidence=confidence,
            evidence=evidence,
            rejection_reasons=tuple(dict.fromkeys(rejection)),
            valid_until=valid_until,
            data_status=(
                DataStatus.OK
                if features.data_status is DataStatus.OK and multi.data_status is DataStatus.OK
                else DataStatus.PENDING
            ),
        )
        self.store.save_coin_research_result(result)
        plan = self._plan(
            result,
            news,
            spot,
            features,
            eligibility,
            commission,
            cost_source=cost_source,
            as_of=as_of,
        )
        if plan is not None:
            self.store.save_trade_plan(plan)
        return result, plan

    def _latest_usable_news(self, symbol: str, *, as_of: datetime) -> CryptoNewsRecord | None:
        return next(
            (
                item
                for item in self.store.latest_crypto_news(limit=500)
                if symbol in item.related_symbols
                and item.first_seen_at <= as_of < item.expires_at
                and item.data_status is DataStatus.OK
                and item.verification_status
                in {NewsVerificationStatus.VERIFIED, NewsVerificationStatus.SINGLE_SOURCE}
            ),
            None,
        )

    def _coverage_status(self, symbol: str) -> NewsCoverageStatus | None:
        raw = self.store.get_checkpoint("news_intelligence:health")
        if raw is None:
            return NewsCoverageStatus.PROVIDER_UNAVAILABLE
        try:
            payload = json.loads(raw)
            coverage_by_symbol = payload["coverage_by_symbol"]
            if not isinstance(coverage_by_symbol, dict):
                return NewsCoverageStatus.NEWS_ERROR
            if symbol not in coverage_by_symbol:
                return None
            value = coverage_by_symbol[symbol]
            return NewsCoverageStatus(value)
        except (KeyError, TypeError, ValueError):
            return NewsCoverageStatus.NEWS_ERROR

    def _critical_source_coverage(self) -> bool:
        raw = self.store.get_checkpoint("news_intelligence:health")
        if raw is None:
            return False
        try:
            payload = json.loads(raw)
            return int(payload.get("critical_sources_healthy", 0)) >= int(
                self.config.news_intelligence.minimum_critical_sources_healthy
            )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _blocking_news(self, symbol: str, *, as_of: datetime) -> tuple[CryptoNewsRecord, ...]:
        blocked_types = {
            NewsEventType.EXPLOIT,
            NewsEventType.SECURITY_INCIDENT,
            NewsEventType.DELISTING,
            NewsEventType.OUTAGE,
            NewsEventType.REGULATORY,
            NewsEventType.RUMOR,
            NewsEventType.HACK,
            NewsEventType.BRIDGE_ATTACK,
            NewsEventType.STABLECOIN_DEPEG,
            NewsEventType.DEPOSIT_PAUSED,
            NewsEventType.WITHDRAWAL_PAUSED,
            NewsEventType.TRADING_PAIR_REMOVED,
            NewsEventType.REGULATORY_ACTION,
        }
        return tuple(
            item
            for item in self.store.latest_crypto_news(limit=500)
            if symbol in item.related_symbols
            and item.first_seen_at <= as_of < item.expires_at
            and (
                item.event_type in blocked_types
                or item.verification_status is NewsVerificationStatus.DISPUTED
            )
        )

    def _btc_m1(self) -> OHLCVSeries | None:
        series = self.store.load_series("BTCUSDT", Timeframe.M1, limit=500)
        return series if series.bars else None

    def _plan(
        self,
        research: CoinResearchResult,
        news: CryptoNewsRecord | None,
        spot: SpotTicker,
        features: FeatureSnapshot,
        eligibility: object,
        commission: object,
        *,
        cost_source: CostSource,
        as_of: datetime,
    ) -> TradePlan | None:
        mission = next(
            (
                item
                for item in self.store.list_trading_missions(limit=100)
                if item.status is MissionStatus.RUNNING
            ),
            None,
        )
        mission_id: str | None = mission.mission_id if mission is not None else None
        if mission_id is None and self.config.trading.mode is TradingMode.PUBLIC_MONITOR:
            mission_id = "PUBLIC_MONITOR"
        if mission_id is None and self.config.trading.mode is TradingMode.SHADOW:
            mission_id = "SHADOW_RESEARCH"
        if mission_id is None:
            return None

        from argus.core.trading import CommissionSchedule, UniverseEligibilitySnapshot

        verified = commission if isinstance(commission, CommissionSchedule) else None
        eligible = eligibility if isinstance(eligibility, UniverseEligibilitySnapshot) else None
        fee_rate = (
            max(verified.maker_rate or Decimal("0"), verified.taker_rate)
            if verified is not None and verified.taker_rate is not None
            else (
                Decimal(str(self.config.fees.conservative_taker_bps)) / Decimal("10000")
                if cost_source is CostSource.CONSERVATIVE_ESTIMATE
                else None
            )
        )
        spread = _decimal(features.values.get("spread_bps"))
        slippage = eligible.estimated_slippage_bps if eligible is not None else None
        one_way_cost: Decimal | None = (
            fee_rate * Decimal("10000") + spread / Decimal("2") + slippage
            if fee_rate is not None and spread is not None and slippage is not None
            else None
        )
        natr = _decimal(features.values.get("normalized_atr_pct"))
        gross = natr * Decimal("100") if natr is not None else Decimal("0")
        support = _decimal(features.values.get("support"))
        reactions = self.store.latest_news_market_reactions(symbol=research.symbol, limit=1)
        volume_ratio = reactions[0].volume_ratio if reactions else None
        age = int((as_of - news.first_seen_at).total_seconds() // 60) if news is not None else None
        return self.planner.plan(
            research,
            mission_id=mission_id,
            expected_entry_price=spot.last_price,
            stop_price=support,
            expected_gross_return_bps=gross,
            expected_entry_cost_bps=one_way_cost,
            expected_exit_cost_bps=one_way_cost,
            uncertainty_buffer_bps=Decimal(str(self.config.fees.model_uncertainty_buffer_bps)),
            news_event_type=news.event_type if news is not None else NewsEventType.UNKNOWN,
            news_age_minutes=age,
            trend_strength=research.technical_score or Decimal("0"),
            volume_ratio=volume_ratio,
            volatility_pct=natr,
            strategy_basis=research.strategy_basis,
            cost_source=cost_source,
            now=as_of,
        )


__all__ = ["IntradayResearchCoordinator"]
