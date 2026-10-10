"""Cost-aware, multi-horizon intraday research planner with no execution authority."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from argus.core.enums import MarketRegimeKind
from argus.core.intelligence import (
    CoinResearchResult,
    CostSource,
    MarketReactionStatus,
    NewsCoverageStatus,
    NewsEventType,
    PreferredOrderType,
    TradeHorizon,
    TradePlan,
    TradeStrategyBasis,
)
from argus.core.schemas import DataStatus, utcnow


class MultiHorizonTradePlanner:
    def __init__(
        self,
        *,
        minimum_confidence: Decimal = Decimal("65"),
        minimum_news_reliability: Decimal = Decimal("0.70"),
        maximum_manipulation_risk: Decimal = Decimal("35"),
        minimum_required_net_edge_bps: Decimal = Decimal("35"),
        maximum_holding_minutes: int = 120,
        plan_validity_seconds: int = 300,
        reevaluation_cooldown_seconds: int = 300,
        meaningful_edge_change_bps: Decimal = Decimal("20"),
    ) -> None:
        if not 60 <= maximum_holding_minutes <= 120:
            raise ValueError("maximum_holding_minutes must be in the range 60..120")
        if plan_validity_seconds <= 0 or reevaluation_cooldown_seconds <= 0:
            raise ValueError("plan durations must be positive")
        self.minimum_confidence = minimum_confidence
        self.minimum_news_reliability = minimum_news_reliability
        self.maximum_manipulation_risk = maximum_manipulation_risk
        self.minimum_required_net_edge_bps = minimum_required_net_edge_bps
        self.maximum_holding_minutes = maximum_holding_minutes
        self.plan_validity_seconds = plan_validity_seconds
        self.reevaluation_cooldown_seconds = reevaluation_cooldown_seconds
        self.meaningful_edge_change_bps = meaningful_edge_change_bps

    def plan(
        self,
        research: CoinResearchResult,
        *,
        mission_id: str,
        expected_entry_price: Decimal,
        stop_price: Decimal | None,
        expected_gross_return_bps: Decimal,
        expected_entry_cost_bps: Decimal | None,
        expected_exit_cost_bps: Decimal | None,
        uncertainty_buffer_bps: Decimal,
        estimated_fees_bps: Decimal | None = None,
        expected_slippage_bps: Decimal | None = None,
        spread_cost_bps: Decimal | None = None,
        news_event_type: NewsEventType = NewsEventType.UNKNOWN,
        news_age_minutes: int | None = None,
        trend_strength: Decimal = Decimal("0"),
        volume_ratio: Decimal | None = None,
        volatility_pct: Decimal | None = None,
        order_flow_continuity: Decimal | None = None,
        strategy_basis: TradeStrategyBasis | None = None,
        cost_source: CostSource = CostSource.NO_DATA,
        now: datetime | None = None,
    ) -> TradePlan:
        at = now or utcnow()
        if at.tzinfo is None:
            raise ValueError("plan time must be tz-aware")
        if expected_entry_price <= 0 or (stop_price is not None and stop_price <= 0):
            raise ValueError("entry and, if given, stop must be positive")
        if any(
            value is not None and value < 0
            for value in (expected_entry_cost_bps, expected_exit_cost_bps, uncertainty_buffer_bps)
        ):
            raise ValueError("cost components cannot be negative")
        if expected_entry_cost_bps is not None and expected_exit_cost_bps is not None:
            net_before_uncertainty: Decimal | None = (
                expected_gross_return_bps - expected_entry_cost_bps - expected_exit_cost_bps
            )
        else:
            net_before_uncertainty = None
        expected_net = (
            net_before_uncertainty - uncertainty_buffer_bps
            if net_before_uncertainty is not None
            else None
        )
        selected_basis = research.strategy_basis if strategy_basis is None else strategy_basis
        if strategy_basis is None and selected_basis is TradeStrategyBasis.NO_TRADE:
            selected_basis = (
                TradeStrategyBasis.NEWS_CATALYST
                if research.news_coverage_status is NewsCoverageStatus.RELEVANT_NEWS
                and research.news_reliability is not None
                else TradeStrategyBasis.TECHNICAL_INTRADAY
            )
        rejection_reasons = self._rejections(
            research,
            at=at,
            strategy_basis=selected_basis,
            news_event_type=news_event_type,
            net_before_uncertainty=net_before_uncertainty,
            uncertainty_buffer_bps=uncertainty_buffer_bps,
            stop_price=stop_price,
        )
        if rejection_reasons:
            return self._no_trade(
                research,
                mission_id=mission_id,
                created_at=at,
                gross_bps=expected_gross_return_bps,
                entry_cost_bps=expected_entry_cost_bps,
                exit_cost_bps=expected_exit_cost_bps,
                net_bps=expected_net,
                uncertainty_bps=uncertainty_buffer_bps,
                reasons=rejection_reasons,
                strategy_basis=selected_basis,
                cost_source=cost_source,
            )
        horizon, holding = self._horizon(
            research,
            strategy_basis=selected_basis,
            news_event_type=news_event_type,
            news_age_minutes=news_age_minutes,
            trend_strength=trend_strength,
            volume_ratio=volume_ratio,
            volatility_pct=volatility_pct,
            order_flow_continuity=order_flow_continuity,
        )
        holding = min(holding, self.maximum_holding_minutes)
        time_stop = at + timedelta(minutes=holding)
        return TradePlan(
            mission_id=mission_id,
            symbol=research.symbol.upper(),
            side="buy",
            thesis="news/technical alignment confirmed with positive edge after costs",
            supporting_evidence=research.evidence,
            contradicting_evidence=research.rejection_reasons,
            news_events=(
                (news_event_type.value,) if news_event_type is not NewsEventType.UNKNOWN else ()
            ),
            technical_conditions=tuple(
                item for item in research.evidence if not item.startswith("news:")
            ),
            market_reaction=research.market_reaction,
            strategy_basis=selected_basis,
            created_at=at,
            valid_until=min(
                research.valid_until,
                at + timedelta(seconds=self.plan_validity_seconds),
            ),
            horizon=horizon,
            expected_holding_minutes=holding,
            entry_condition="closed 1m/5m confirmation required; spread/slippage limits must hold",
            preferred_order_type=PreferredOrderType.LIMIT,
            expected_entry_price=expected_entry_price,
            entry_zone=(expected_entry_price, expected_entry_price),
            invalidation_price=stop_price,
            stop_price=stop_price,
            target_prices=(
                expected_entry_price
                * (Decimal("1") + expected_gross_return_bps / Decimal("10000")),
            ),
            take_profit_policy="plan edge after costs or structure target",
            trailing_policy="deterministic trailing only on favourable moves and healthy liquidity",
            time_stop=time_stop,
            thesis_invalidation="exit if news thesis, trend or order-flow evidence is invalidated",
            expected_gross_return_bps=expected_gross_return_bps,
            expected_entry_cost_bps=expected_entry_cost_bps,
            expected_exit_cost_bps=expected_exit_cost_bps,
            estimated_round_trip_cost_bps=(
                expected_entry_cost_bps + expected_exit_cost_bps
                if expected_entry_cost_bps is not None and expected_exit_cost_bps is not None
                else None
            ),
            expected_net_return_bps=expected_net,
            estimated_fees_bps=estimated_fees_bps,
            expected_slippage_bps=expected_slippage_bps,
            spread_cost_bps=spread_cost_bps,
            execution_buffer_bps=uncertainty_buffer_bps,
            uncertainty_buffer_bps=uncertainty_buffer_bps,
            confidence=research.confidence,
            news_evidence=tuple(item for item in research.evidence if item.startswith("news:")),
            technical_evidence=tuple(
                item for item in research.evidence if not item.startswith("news:")
            ),
            risk_flags=(),
            rejection_reasons=(),
            freshness="fresh",
            guard_result="pending",
            cost_source=cost_source,
            live_eligible=False,
            strategy_version="multi-horizon-v1",
            research_id=research.research_id,
            last_reassessed_at=at,
            data_status=DataStatus.OK,
            maximum_holding_time=holding,
            generated_at=at,
            expires_at=min(
                research.valid_until,
                at + timedelta(seconds=self.plan_validity_seconds),
            ),
            expected_gross_edge=expected_gross_return_bps,
            expected_net_edge=expected_net,
        )

    def _rejections(
        self,
        research: CoinResearchResult,
        *,
        at: datetime,
        strategy_basis: TradeStrategyBasis,
        news_event_type: NewsEventType,
        net_before_uncertainty: Decimal | None,
        uncertainty_buffer_bps: Decimal,
        stop_price: Decimal | None,
    ) -> tuple[str, ...]:
        reasons = list(research.rejection_reasons)
        if strategy_basis is TradeStrategyBasis.NO_TRADE:
            reasons.append("research selected neither NEWS_CATALYST nor TECHNICAL_INTRADAY")
        if news_event_type in {
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
        }:
            reasons.append(f"high-risk news blocked entry: {news_event_type.value}")
        if net_before_uncertainty is None:
            reasons.append("verified entry/exit cost NO_DATA")
        if stop_price is None:
            reasons.append("deterministic stop level NO_DATA")
        if research.data_status is not DataStatus.OK:
            reasons.append("research data is not ACCEPT")
        if not research.eligibility:
            reasons.append("symbol is not eligible")
        if at >= research.valid_until:
            reasons.append("research has expired")
        if strategy_basis is TradeStrategyBasis.NEWS_CATALYST:
            if research.news_coverage_status is not NewsCoverageStatus.RELEVANT_NEWS:
                reasons.append("no current relevant news for NEWS_CATALYST")
            if research.market_reaction not in {
                MarketReactionStatus.NOT_PRICED,
                MarketReactionStatus.PARTIALLY_PRICED,
            }:
                reasons.append(
                    f"news-market reaction closed to entry: {research.market_reaction.value}"
                )
            if (
                research.news_reliability is None
                or research.news_reliability < self.minimum_news_reliability
            ):
                reasons.append("news reliability insufficient/NO_DATA")
        elif strategy_basis is TradeStrategyBasis.TECHNICAL_INTRADAY:
            if research.news_coverage_status not in {
                NewsCoverageStatus.NO_RELEVANT_NEWS,
                NewsCoverageStatus.RELEVANT_NEWS,
            }:
                reasons.append(
                    f"news coverage not healthy for a technical plan: "
                    f"{research.news_coverage_status.value}"
                )
            if research.market_regime is not MarketRegimeKind.TREND_UP:
                reasons.append("no TREND_UP regime for technical intraday")
        if research.confidence < self.minimum_confidence:
            reasons.append("research confidence below threshold")
        if (
            research.manipulation_risk is None
            or research.manipulation_risk > self.maximum_manipulation_risk
        ):
            reasons.append("manipulation risk high/NO_DATA")
        required = self.minimum_required_net_edge_bps + uncertainty_buffer_bps
        if net_before_uncertainty is not None and net_before_uncertainty <= required:
            reasons.append("insufficient net edge after costs and uncertainty")
        if not research.evidence:
            reasons.append("no independent evidence")
        return tuple(dict.fromkeys(reasons))

    def _horizon(
        self,
        research: CoinResearchResult,
        *,
        strategy_basis: TradeStrategyBasis,
        news_event_type: NewsEventType,
        news_age_minutes: int | None,
        trend_strength: Decimal,
        volume_ratio: Decimal | None,
        volatility_pct: Decimal | None,
        order_flow_continuity: Decimal | None,
    ) -> tuple[TradeHorizon, int]:
        fast_reaction = (news_age_minutes is not None and news_age_minutes <= 15) and (
            volume_ratio or Decimal("0")
        ) >= Decimal("2")
        if (strategy_basis is TradeStrategyBasis.NEWS_CATALYST and fast_reaction) or (
            volatility_pct or Decimal("0")
        ) >= Decimal("2"):
            return TradeHorizon.SCALP_15_30_MIN, 20
        durable_event = news_event_type in {
            NewsEventType.NETWORK_UPGRADE,
            NewsEventType.PRODUCT_RELEASE,
            NewsEventType.REGULATORY,
            NewsEventType.PARTNERSHIP,
        }
        continuation = (
            trend_strength >= Decimal("70")
            and (order_flow_continuity or Decimal("0")) >= Decimal("0.65")
            and research.market_regime is MarketRegimeKind.TREND_UP
        )
        if durable_event and continuation:
            return TradeHorizon.EXTENDED_INTRADAY, 90
        return TradeHorizon.INTRADAY_30_60_MIN, 45

    def _no_trade(
        self,
        research: CoinResearchResult,
        *,
        mission_id: str,
        created_at: datetime,
        gross_bps: Decimal,
        entry_cost_bps: Decimal | None,
        exit_cost_bps: Decimal | None,
        net_bps: Decimal | None,
        uncertainty_bps: Decimal,
        reasons: tuple[str, ...],
        strategy_basis: TradeStrategyBasis,
        cost_source: CostSource,
    ) -> TradePlan:
        return TradePlan(
            mission_id=mission_id,
            symbol=research.symbol.upper(),
            side="no_trade",
            thesis="NO_TRADE: " + "; ".join(reasons),
            supporting_evidence=research.evidence,
            contradicting_evidence=reasons,
            market_reaction=research.market_reaction,
            strategy_basis=strategy_basis,
            created_at=created_at,
            valid_until=created_at + timedelta(seconds=self.plan_validity_seconds),
            horizon=TradeHorizon.NO_TRADE,
            expected_holding_minutes=0,
            entry_condition="NO_TRADE: " + "; ".join(reasons),
            preferred_order_type=PreferredOrderType.NONE,
            take_profit_policy="not applicable",
            trailing_policy="not applicable",
            thesis_invalidation="not applicable",
            expected_gross_return_bps=gross_bps,
            expected_entry_cost_bps=entry_cost_bps,
            expected_exit_cost_bps=exit_cost_bps,
            estimated_round_trip_cost_bps=(
                entry_cost_bps + exit_cost_bps
                if entry_cost_bps is not None and exit_cost_bps is not None
                else None
            ),
            expected_net_return_bps=net_bps,
            uncertainty_buffer_bps=uncertainty_bps,
            confidence=research.confidence,
            news_evidence=tuple(item for item in research.evidence if item.startswith("news:")),
            technical_evidence=tuple(
                item for item in research.evidence if not item.startswith("news:")
            ),
            risk_flags=reasons,
            rejection_reasons=reasons,
            no_trade_reasons=reasons,
            freshness="stale" if created_at >= research.valid_until else "no_data",
            guard_result="blocked",
            cost_source=cost_source,
            live_eligible=False,
            strategy_version="multi-horizon-v1",
            research_id=research.research_id,
            last_reassessed_at=created_at,
            data_status=DataStatus.REJECTED,
            maximum_holding_time=0,
            generated_at=created_at,
            expires_at=created_at + timedelta(seconds=self.plan_validity_seconds),
            expected_gross_edge=gross_bps,
            expected_net_edge=net_bps,
        )

    def reassess(
        self,
        previous: TradePlan,
        research: CoinResearchResult,
        *,
        expected_entry_price: Decimal,
        stop_price: Decimal | None,
        expected_gross_return_bps: Decimal,
        expected_entry_cost_bps: Decimal | None,
        expected_exit_cost_bps: Decimal | None,
        uncertainty_buffer_bps: Decimal,
        news_event_type: NewsEventType = NewsEventType.UNKNOWN,
        news_age_minutes: int | None = None,
        trend_strength: Decimal = Decimal("0"),
        volume_ratio: Decimal | None = None,
        volatility_pct: Decimal | None = None,
        order_flow_continuity: Decimal | None = None,
        strategy_basis: TradeStrategyBasis | None = None,
        cost_source: CostSource = CostSource.NO_DATA,
        now: datetime | None = None,
    ) -> TradePlan:
        at = now or utcnow()
        reference = previous.last_reassessed_at or previous.created_at
        if (at - reference).total_seconds() < self.reevaluation_cooldown_seconds:
            return previous
        replacement = self.plan(
            research,
            mission_id=previous.mission_id,
            expected_entry_price=expected_entry_price,
            stop_price=stop_price,
            expected_gross_return_bps=expected_gross_return_bps,
            expected_entry_cost_bps=expected_entry_cost_bps,
            expected_exit_cost_bps=expected_exit_cost_bps,
            uncertainty_buffer_bps=uncertainty_buffer_bps,
            news_event_type=news_event_type,
            news_age_minutes=news_age_minutes,
            trend_strength=trend_strength,
            volume_ratio=volume_ratio,
            volatility_pct=volatility_pct,
            order_flow_continuity=order_flow_continuity,
            strategy_basis=strategy_basis,
            cost_source=cost_source,
            now=at,
        )
        previous_edge = previous.expected_net_return_bps
        replacement_edge = replacement.expected_net_return_bps
        material_edge_change = (
            previous_edge is None
            or replacement_edge is None
            or abs(replacement_edge - previous_edge) >= self.meaningful_edge_change_bps
        )
        if replacement.horizon is previous.horizon and not material_edge_change:
            return previous.model_copy(update={"last_reassessed_at": at})
        return replacement

    @staticmethod
    def expired(plan: TradePlan, *, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= plan.valid_until


__all__ = ["MultiHorizonTradePlanner"]
