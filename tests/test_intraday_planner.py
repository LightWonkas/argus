"""Closed-candle and multi-horizon TradePlan tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import MarketRegimeKind, Timeframe
from argus.core.intelligence import (
    CoinResearchResult,
    MarketReactionStatus,
    NewsCoverageStatus,
    NewsEventType,
    TradeHorizon,
)
from argus.core.schemas import DataStatus, Provenance
from argus.decision.trade_planner import MultiHorizonTradePlanner
from argus.platform.intraday_features import analyze_closed_timeframe, analyze_intraday_timeframes
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _series(timeframe: Timeframe, *, provisional: bool = False) -> OHLCVSeries:
    bars = []
    for index in range(4):
        timestamp = NOW - timedelta(seconds=timeframe.seconds * (4 - index))
        price = Decimal("100") + index
        bars.append(
            OHLCVBar(
                timestamp=timestamp,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("10") + index,
            )
        )
    if provisional:
        bars.append(
            OHLCVBar(
                timestamp=NOW - timedelta(seconds=timeframe.seconds // 2),
                open=Decimal("999"),
                high=Decimal("999"),
                low=Decimal("999"),
                close=Decimal("999"),
                volume=Decimal("999"),
            )
        )
    return OHLCVSeries(
        symbol="BTCUSDT",
        timeframe=timeframe,
        bars=tuple(bars),
        provenance=Provenance(source="test", timestamp=NOW, confidence=1),
    )


def _research(**updates: object) -> CoinResearchResult:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "as_of": NOW,
        "eligibility": True,
        "news_coverage_status": NewsCoverageStatus.RELEVANT_NEWS,
        "market_regime": MarketRegimeKind.TREND_UP,
        "technical_score": Decimal("80"),
        "news_score": Decimal("75"),
        "news_reliability": Decimal("0.9"),
        "market_reaction": MarketReactionStatus.PARTIALLY_PRICED,
        "liquidity_score": Decimal("90"),
        "manipulation_risk": Decimal("10"),
        "execution_quality": Decimal("90"),
        "confidence": Decimal("80"),
        "evidence": ("news:official", "market:closed-bars", "book:depth"),
        "valid_until": NOW + timedelta(minutes=10),
        "data_status": DataStatus.OK,
    }
    values.update(updates)
    return CoinResearchResult(**values)


def _plan(planner: MultiHorizonTradePlanner, research: CoinResearchResult, **extra: object):
    inputs: dict[str, object] = {
        "mission_id": "mission-1",
        "expected_entry_price": Decimal("100"),
        "stop_price": Decimal("98"),
        "expected_gross_return_bps": Decimal("200"),
        "expected_entry_cost_bps": Decimal("15"),
        "expected_exit_cost_bps": Decimal("15"),
        "uncertainty_buffer_bps": Decimal("20"),
        "now": NOW,
    }
    inputs.update(extra)
    return planner.plan(research, **inputs)  # type: ignore[arg-type]


def test_only_closed_bars_are_used_and_timeframes_align() -> None:
    m1 = _series(Timeframe.M1, provisional=True)
    analysis = analyze_closed_timeframe(m1, as_of=NOW)
    assert analysis.bars_used == 4
    assert analysis.provisional_bars_excluded == 1
    assert analysis.close_price == Decimal("103")

    series = {
        timeframe: _series(timeframe)
        for timeframe in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1)
    }
    aligned = analyze_intraday_timeframes("BTCUSDT", series, as_of=NOW)
    assert aligned.aligned
    assert aligned.data_status is DataStatus.OK

    missing = analyze_intraday_timeframes("BTCUSDT", {Timeframe.M1: m1}, as_of=NOW)
    assert not missing.aligned
    assert set(missing.missing_data) >= {"5m", "15m", "1h"}


def test_planner_selects_scalp_intraday_and_bounded_extended_horizons() -> None:
    planner = MultiHorizonTradePlanner()
    scalp = _plan(planner, _research(), news_age_minutes=5, volume_ratio=Decimal("3"))
    assert scalp.horizon is TradeHorizon.SCALP_15_30_MIN
    assert scalp.expected_holding_minutes == 20

    intraday = _plan(planner, _research())
    assert intraday.horizon is TradeHorizon.INTRADAY_30_60_MIN
    assert intraday.expected_holding_minutes == 45

    extended = _plan(
        planner,
        _research(),
        news_event_type=NewsEventType.NETWORK_UPGRADE,
        trend_strength=Decimal("80"),
        order_flow_continuity=Decimal("0.8"),
    )
    assert extended.horizon is TradeHorizon.EXTENDED_INTRADAY
    assert extended.expected_holding_minutes == 90
    assert extended.time_stop == NOW + timedelta(minutes=90)
    assert extended.submission_authorized is False


def test_planner_returns_no_trade_for_priced_news_or_consumed_edge() -> None:
    planner = MultiHorizonTradePlanner()
    priced = _plan(
        planner,
        _research(market_reaction=MarketReactionStatus.ALREADY_PRICED),
    )
    costly = _plan(
        planner,
        _research(),
        expected_gross_return_bps=Decimal("60"),
    )
    assert priced.horizon is TradeHorizon.NO_TRADE
    assert costly.horizon is TradeHorizon.NO_TRADE
    assert priced.data_status is DataStatus.REJECTED


def test_planner_keeps_unknown_costs_as_no_data_and_rejects_entry() -> None:
    plan = _plan(
        MultiHorizonTradePlanner(),
        _research(),
        expected_entry_cost_bps=None,
        expected_exit_cost_bps=None,
    )
    assert plan.horizon is TradeHorizon.NO_TRADE
    assert plan.expected_entry_cost_bps is None
    assert plan.expected_exit_cost_bps is None
    assert plan.expected_net_return_bps is None
    assert "verified entry/exit cost NO_DATA" in plan.risk_flags


def test_plan_expiry_and_reassessment_cooldown_prevent_churn() -> None:
    planner = MultiHorizonTradePlanner(reevaluation_cooldown_seconds=300)
    previous = _plan(planner, _research())
    unchanged = planner.reassess(
        previous,
        _research(),
        expected_entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        expected_gross_return_bps=Decimal("210"),
        expected_entry_cost_bps=Decimal("15"),
        expected_exit_cost_bps=Decimal("15"),
        uncertainty_buffer_bps=Decimal("20"),
        now=NOW + timedelta(minutes=1),
    )
    assert unchanged is previous
    assert planner.expired(previous, now=previous.valid_until)


def test_research_and_plan_storage_are_idempotent_and_submission_closed() -> None:
    research = _research()
    plan = _plan(MultiHorizonTradePlanner(), research)
    store = SqliteStore()
    try:
        assert store.save_coin_research_result(research)
        assert not store.save_coin_research_result(research)
        assert store.save_trade_plan(plan)
        assert not store.save_trade_plan(plan)
        assert store.latest_coin_research_results() == [research]
        assert store.latest_trade_plans(mission_id="mission-1") == [plan]
        assert store.latest_trade_plans()[0].submission_authorized is False
    finally:
        store.close()
