"""Public-only news/technical/cost replay acceptance scenarios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import MarketRegimeKind, Timeframe
from argus.core.intelligence import (
    CoinResearchResult,
    CostSource,
    MarketReactionStatus,
    NewsCoverageStatus,
    NewsEventType,
    NewsVerificationStatus,
    ProviderNewsItem,
    TradeHorizon,
    TradeStrategyBasis,
)
from argus.core.schemas import DataStatus, Provenance
from argus.decision.trade_planner import MultiHorizonTradePlanner
from argus.platform.intraday_features import analyze_closed_timeframe
from argus.platform.news_intelligence import NewsIntelligenceEngine
from argus.platform.news_reaction import NewsMarketReactionAnalyzer
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


def _research(
    basis: TradeStrategyBasis,
    *,
    coverage: NewsCoverageStatus = NewsCoverageStatus.RELEVANT_NEWS,
    reaction: MarketReactionStatus = MarketReactionStatus.PARTIALLY_PRICED,
    reasons: tuple[str, ...] = (),
) -> CoinResearchResult:
    catalyst = basis is TradeStrategyBasis.NEWS_CATALYST
    return CoinResearchResult(
        symbol="BTCUSDT",
        as_of=NOW,
        eligibility=True,
        strategy_basis=basis,
        news_coverage_status=coverage,
        market_regime=MarketRegimeKind.TREND_UP,
        technical_score=Decimal("82"),
        news_score=Decimal("80") if catalyst else None,
        news_reliability=Decimal("0.90") if catalyst else None,
        market_reaction=reaction,
        liquidity_score=Decimal("92"),
        manipulation_risk=Decimal("10"),
        execution_quality=Decimal("90"),
        confidence=Decimal("82"),
        evidence=(
            *(("news:verified",) if catalyst else ()),
            "market:1m:up",
            "market:5m:up",
            "market:15m:up",
            "book:depth",
        ),
        rejection_reasons=reasons,
        valid_until=NOW + timedelta(minutes=10),
        data_status=DataStatus.OK,
    )


def _plan(
    research: CoinResearchResult,
    *,
    gross: str = "220",
    entry_cost: str = "15",
    exit_cost: str = "15",
    event_type: NewsEventType = NewsEventType.UNKNOWN,
    now: datetime = NOW,
):
    return MultiHorizonTradePlanner().plan(
        research,
        mission_id="PUBLIC_REPLAY",
        expected_entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        expected_gross_return_bps=Decimal(gross),
        expected_entry_cost_bps=Decimal(entry_cost),
        expected_exit_cost_bps=Decimal(exit_cost),
        uncertainty_buffer_bps=Decimal("20"),
        news_event_type=event_type,
        news_age_minutes=5,
        trend_strength=Decimal("82"),
        volume_ratio=Decimal("3"),
        strategy_basis=research.strategy_basis,
        cost_source=CostSource.CONSERVATIVE_ESTIMATE,
        now=now,
    )


def _item(
    title: str,
    *,
    summary: str = "Bitcoin network upgrade",
    published_at: datetime = NOW - timedelta(minutes=5),
    provider: str = "feed-a",
) -> ProviderNewsItem:
    return ProviderNewsItem(
        provider_item_id=f"{provider}:{title}",
        canonical_url=f"https://example.com/{provider}/{title.replace(' ', '-')}",
        provider=provider,
        source_name=provider,
        source_reliability=Decimal("0.9"),
        title=title,
        short_summary=summary,
        published_at=published_at,
    )


def _bar(minutes: int, close: str, volume: str = "10") -> OHLCVBar:
    price = Decimal(close)
    return OHLCVBar(
        timestamp=NOW + timedelta(minutes=minutes),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
    )


def test_replay_news_catalyst_produces_non_live_scalp_plan() -> None:
    news = NewsIntelligenceEngine(())._record(
        (_item("Bitcoin upgrade released"),),
        duplicate_group="replay",
        allowed_symbols={"BTCUSDT"},
        now=NOW,
    )
    reaction = NewsMarketReactionAnalyzer().analyze(
        news,
        coin_closed_bars=(_bar(-2, "100"), _bar(-1, "100"), _bar(1, "100.1", "30")),
        btc_closed_bars=(_bar(-1, "100"), _bar(1, "100")),
        as_of=NOW + timedelta(minutes=2),
    )
    assert reaction.status is MarketReactionStatus.NOT_PRICED
    plan = _plan(
        _research(TradeStrategyBasis.NEWS_CATALYST, reaction=reaction.status),
        event_type=NewsEventType.NETWORK_UPGRADE,
    )
    assert plan.horizon is TradeHorizon.SCALP_15_30_MIN
    assert plan.strategy_basis is TradeStrategyBasis.NEWS_CATALYST
    assert plan.cost_source is CostSource.CONSERVATIVE_ESTIMATE
    assert plan.estimated_round_trip_cost_bps == Decimal("30")
    assert not plan.live_eligible and not plan.submission_authorized


def test_replay_no_relevant_news_allows_technical_intraday() -> None:
    research = _research(
        TradeStrategyBasis.TECHNICAL_INTRADAY,
        coverage=NewsCoverageStatus.NO_RELEVANT_NEWS,
        reaction=MarketReactionStatus.NO_DATA,
    )
    plan = _plan(research)
    assert plan.horizon is TradeHorizon.INTRADAY_30_60_MIN
    assert plan.strategy_basis is TradeStrategyBasis.TECHNICAL_INTRADAY


def test_replay_priced_or_pump_news_is_no_trade() -> None:
    for status in (MarketReactionStatus.ALREADY_PRICED, MarketReactionStatus.PUMP_RISK):
        plan = _plan(_research(TradeStrategyBasis.NEWS_CATALYST, reaction=status))
        assert plan.horizon is TradeHorizon.NO_TRADE
        assert any(
            "news-market reaction closed to entry" in reason for reason in plan.rejection_reasons
        )


def test_replay_exploit_and_delisting_block_entry() -> None:
    research = _research(TradeStrategyBasis.NEWS_CATALYST)
    for event_type in (NewsEventType.EXPLOIT, NewsEventType.DELISTING):
        plan = _plan(research, event_type=event_type)
        assert plan.horizon is TradeHorizon.NO_TRADE
        assert any(event_type.value in reason for reason in plan.rejection_reasons)


def test_replay_costs_can_consume_strong_gross_edge() -> None:
    plan = _plan(
        _research(TradeStrategyBasis.NEWS_CATALYST),
        gross="90",
        entry_cost="25",
        exit_cost="25",
    )
    assert plan.horizon is TradeHorizon.NO_TRADE
    assert plan.expected_net_return_bps == Decimal("20")
    assert any(
        "insufficient net edge after costs and uncertainty" in reason
        for reason in plan.rejection_reasons
    )


def test_replay_old_and_future_news_are_rejected() -> None:
    batch = NewsIntelligenceEngine(()).collect(universe_symbols=("BTCUSDT",), now=NOW)
    assert batch.coverage_status is NewsCoverageStatus.PROVIDER_UNAVAILABLE
    for item in (
        _item("Bitcoin old update", published_at=NOW - timedelta(days=2)),
        _item("Bitcoin future update", published_at=NOW + timedelta(minutes=10)),
    ):
        record = NewsIntelligenceEngine(())._record(
            (item,),
            duplicate_group=item.provider_item_id,
            allowed_symbols={"BTCUSDT"},
            now=NOW,
        )
        assert record.verification_status is NewsVerificationStatus.REJECTED
        assert record.data_status is DataStatus.REJECTED


def test_replay_ambiguous_ticker_cannot_be_trade_evidence() -> None:
    record = NewsIntelligenceEngine(())._record(
        (_item("SOL rises", summary="SOL momentum"),),
        duplicate_group="ambiguous",
        allowed_symbols={"SOLUSDT"},
        now=NOW,
    )
    assert record.related_symbols == ()
    assert record.entity_matches[0].ambiguous


def test_replay_conflicting_sources_block_both_strategies() -> None:
    positive = _item("Bitcoin protocol report", summary="Bitcoin partnership announced")
    negative = _item(
        "Bitcoin protocol report",
        summary="Bitcoin exploit drained funds",
        provider="feed-b",
    )
    batch = NewsIntelligenceEngine(
        (
            type(
                "Provider",
                (),
                {
                    "name": "conflict",
                    "fetch": lambda self, now=None: (positive, negative),
                },
            )(),
        )
    ).collect(universe_symbols=("BTCUSDT",), now=NOW)
    assert batch.coverage_status is NewsCoverageStatus.NEWS_CONFLICT
    plan = _plan(
        _research(
            TradeStrategyBasis.TECHNICAL_INTRADAY,
            coverage=NewsCoverageStatus.NEWS_CONFLICT,
            reaction=MarketReactionStatus.NO_DATA,
        )
    )
    assert plan.horizon is TradeHorizon.NO_TRADE


def test_replay_provisional_bar_is_excluded() -> None:
    bars = (
        _bar(-3, "100"),
        _bar(-2, "101"),
        _bar(-1, "102"),
        _bar(0, "999", "999"),
    )
    series = OHLCVSeries(
        symbol="BTCUSDT",
        timeframe=Timeframe.M1,
        bars=bars,
        provenance=Provenance(source="replay", timestamp=NOW, confidence=1),
    )
    analysis = analyze_closed_timeframe(series, as_of=NOW + timedelta(seconds=30))
    assert analysis.close_price == Decimal("102")
    assert analysis.provisional_bars_excluded == 1


def test_replay_plan_expiry_and_thesis_invalidation_create_no_execution() -> None:
    planner = MultiHorizonTradePlanner(reevaluation_cooldown_seconds=60)
    initial = _plan(_research(TradeStrategyBasis.NEWS_CATALYST))
    assert planner.expired(initial, now=initial.valid_until)
    invalidated = _research(
        TradeStrategyBasis.NEWS_CATALYST,
        reasons=("thesis invalidated: order-flow reversed",),
    )
    replacement = planner.reassess(
        initial,
        invalidated,
        expected_entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        expected_gross_return_bps=Decimal("220"),
        expected_entry_cost_bps=Decimal("15"),
        expected_exit_cost_bps=Decimal("15"),
        uncertainty_buffer_bps=Decimal("20"),
        strategy_basis=TradeStrategyBasis.NEWS_CATALYST,
        cost_source=CostSource.CONSERVATIVE_ESTIMATE,
        now=NOW + timedelta(minutes=6),
    )
    assert replacement.horizon is TradeHorizon.NO_TRADE

    store = SqliteStore()
    try:
        store.save_trade_plan(initial)
        store.save_trade_plan(replacement)
        assert store.order_intents("PUBLIC_REPLAY") == []
        assert store.shadow_positions(open_only=False) == []
    finally:
        store.close()
