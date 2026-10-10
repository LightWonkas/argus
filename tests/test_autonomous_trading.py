"""Fail-closed, Decimal and spot-only tests for the autonomous research foundation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from argus.config.models import PortfolioPolicyConfig, RuntimeConfig, UniverseDiscoveryConfig
from argus.core.crypto import OrderBookLevel, OrderBookSnapshot
from argus.core.enums import MarketType, OrderSide
from argus.core.errors import RiskRejectedError
from argus.core.intelligence import (
    PreferredOrderType,
    TradeHorizon,
    TradePlan,
    TradeStrategyBasis,
)
from argus.core.schemas import DataStatus
from argus.core.trading import (
    AllocationProposal,
    AllocationStatus,
    ArmPreflight,
    CommissionSchedule,
    ExchangeFilterSet,
    FeeSource,
    MissionGuardDecision,
    MissionLedgerSnapshot,
    MissionStatus,
    OrderIntent,
    ResearchStatus,
    TradingMission,
    TradingMode,
    UniverseEligibilitySnapshot,
    UniverseMarketObservation,
    UniverseRejectReason,
)
from argus.decision.autonomous_guard import AutonomousGuard, AutonomousGuardState
from argus.decision.costs import FeeAndCostEngine
from argus.decision.mission import MissionCapitalLedger, MissionStateMachine
from argus.decision.portfolio import PortfolioAllocator
from argus.platform.universe import UniverseDiscoveryEngine

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _filters() -> ExchangeFilterSet:
    return ExchangeFilterSet(
        price_tick=Decimal("0.01"),
        lot_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def _observation(**updates: object) -> UniverseMarketObservation:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "exchange_status": "TRADING",
        "spot_allowed": True,
        "observed_at": NOW,
        "listing_time": NOW - timedelta(days=365),
        "listing_time_source": "first-kline",
        "quote_volume_24h": Decimal("100000000"),
        "trade_count_24h": 100000,
        "bid": Decimal("99.95"),
        "ask": Decimal("100.05"),
        "estimated_depth_quote": Decimal("500000"),
        "estimated_slippage_bps": Decimal("2"),
        "realized_volatility_pct": Decimal("50"),
        "history_bars": 500,
        "filters": _filters(),
        "data_status": DataStatus.OK,
    }
    data.update(updates)
    return UniverseMarketObservation(**data)


def _mission(*, mode: TradingMode = TradingMode.PUBLIC_MONITOR) -> TradingMission:
    return TradingMission(
        mission_id="mission-1",
        status=MissionStatus.RUNNING,
        mode=mode,
        capital_limit=Decimal("10000"),
        starting_equity=Decimal("10000"),
        available_mission_cash=Decimal("10000"),
        created_at=NOW,
        started_at=NOW + timedelta(seconds=2),
        armed_at=NOW + timedelta(seconds=1),
        scheduled_end_at=NOW + timedelta(days=7),
    )


def _book(
    *,
    bids: tuple[tuple[str, str], ...],
    asks: tuple[tuple[str, str], ...],
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange="binance",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        event_timestamp=NOW,
        ingestion_timestamp=NOW,
        source="test:public",
        bids=tuple(OrderBookLevel(price=price, quantity=quantity) for price, quantity in bids),
        asks=tuple(OrderBookLevel(price=price, quantity=quantity) for price, quantity in asks),
    )


def _eligibility() -> UniverseEligibilitySnapshot:
    return UniverseDiscoveryEngine(UniverseDiscoveryConfig()).evaluate(
        _observation(), discovered_at=NOW, maximum_trade_notional=Decimal("1000")
    )


def _commission() -> CommissionSchedule:
    return CommissionSchedule(
        symbol="BTCUSDT",
        maker_rate=Decimal("0.001"),
        taker_rate=Decimal("0.001"),
        source=FeeSource.ACCOUNT_VERIFIED,
        fetched_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def _trade_plan() -> TradePlan:
    return TradePlan(
        mission_id="mission-1",
        symbol="BTCUSDT",
        strategy_basis=TradeStrategyBasis.TECHNICAL_INTRADAY,
        created_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        horizon=TradeHorizon.INTRADAY_30_60_MIN,
        expected_holding_minutes=45,
        entry_condition="closed-bar confirmation",
        preferred_order_type=PreferredOrderType.LIMIT,
        expected_entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        take_profit_policy="structure",
        trailing_policy="disabled",
        time_stop=NOW + timedelta(minutes=45),
        thesis_invalidation="trend breaks",
        expected_gross_return_bps=Decimal("200"),
        expected_entry_cost_bps=Decimal("20"),
        expected_exit_cost_bps=Decimal("20"),
        expected_net_return_bps=Decimal("140"),
        uncertainty_buffer_bps=Decimal("20"),
        confidence=Decimal("80"),
        strategy_version="test",
        data_status=DataStatus.OK,
    )


def test_runtime_defaults_are_public_disarmed_and_live_config_is_rejected() -> None:
    config = RuntimeConfig()
    assert config.trading.mode is TradingMode.PUBLIC_MONITOR
    assert config.trading.live_execution_enabled is False
    assert config.trading.execution_backend == "disabled"
    assert config.shadow.enabled is False

    with pytest.raises(ValidationError, match="execution_backend=disabled"):
        RuntimeConfig(trading={"mode": "live_spot"})
    with pytest.raises(ValidationError):
        RuntimeConfig(trading={"live_execution_enabled": True})


def test_order_intent_cannot_authorize_submission() -> None:
    from argus.core.trading import ExitPlan

    exit_plan = ExitPlan(
        initial_stop=Decimal("95"),
        take_profit_policy="deterministic target",
        trailing_policy="disabled",
        time_stop=NOW + timedelta(hours=2),
        thesis_invalidation="close below support",
        maximum_holding_seconds=7200,
        emergency_exit_policy="fail closed",
    )
    with pytest.raises(ValidationError):
        OrderIntent(
            client_order_id="argus-1",
            mission_id="mission-1",
            candidate_id="candidate-1",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.1"),
            mode=TradingMode.LIVE_SPOT,
            owner_id="worker-1",
            fencing_token=1,
            guard_result_id="guard-1",
            exit_plan=exit_plan,
            submission_authorized=True,
        )


def test_mission_requires_preflight_and_restart_reconciliation() -> None:
    created = _mission().model_copy(
        update={
            "status": MissionStatus.DISARMED,
            "started_at": None,
            "armed_at": None,
        }
    )
    machine = MissionStateMachine()
    arming = machine.begin_arming(created, now=NOW + timedelta(seconds=1))
    failed = machine.complete_arming(arming, ArmPreflight(), now=NOW + timedelta(seconds=2))
    assert failed.status is MissionStatus.FAILED_SAFE
    assert "no explicit user arming approval" in (failed.halt_reason or "")

    arming = machine.begin_arming(created, now=NOW + timedelta(seconds=1))
    armed = machine.complete_arming(
        arming,
        ArmPreflight(explicit_user_arm=True, storage_healthy=True, leader_lease_held=True),
        now=NOW + timedelta(seconds=2),
    )
    running = machine.start(armed, now=NOW + timedelta(seconds=3))
    restart = machine.guard_restart(running)
    assert restart.status is MissionStatus.ARMING
    assert restart.last_reconciled_at is None
    resumed = machine.resume_after_reconciliation(
        restart, successful=True, now=NOW + timedelta(seconds=4)
    )
    assert resumed.status is MissionStatus.RUNNING


def test_universe_rejects_unknown_listing_stablecoin_and_missing_metrics() -> None:
    engine = UniverseDiscoveryEngine(UniverseDiscoveryConfig())
    result = engine.evaluate(
        _observation(
            symbol="USDCUSDT",
            base_asset="USDC",
            listing_time=None,
            quote_volume_24h=None,
        ),
        discovered_at=NOW,
    )
    assert not result.eligible
    assert UniverseRejectReason.STABLECOIN in result.rejection_reasons
    assert UniverseRejectReason.LISTING_AGE_UNKNOWN in result.rejection_reasons
    assert UniverseRejectReason.METRIC_NO_DATA in result.rejection_reasons


@pytest.mark.parametrize("asset", ["USDC", "USD1", "RLUSD", "USDE", "USDS"])
def test_explicit_stablecoin_registry_blocks_observed_usdt_pairs(asset: str) -> None:
    result = UniverseDiscoveryEngine(UniverseDiscoveryConfig()).evaluate(
        _observation(symbol=f"{asset}USDT", base_asset=asset),
        discovered_at=NOW,
    )
    assert not result.eligible
    assert UniverseRejectReason.STABLECOIN in result.rejection_reasons


def test_universe_scores_only_eligible_complete_research() -> None:
    engine = UniverseDiscoveryEngine(UniverseDiscoveryConfig())
    eligibility = engine.evaluate(_observation(), discovered_at=NOW)
    score = engine.score(
        eligibility,
        liquidity=Decimal("90"),
        trend=Decimal("80"),
        momentum=Decimal("70"),
        order_flow=Decimal("60"),
        regime_compatibility=Decimal("80"),
        execution_quality=Decimal("90"),
        risk=Decimal("20"),
        manipulation_risk=Decimal("10"),
        cost_adjusted_edge=Decimal("75"),
        portfolio_fit=Decimal("70"),
        evidence=("market:book", "market:ohlcv"),
        data_sources=("binance:public",),
    )
    assert eligibility.eligible
    assert score.status is ResearchStatus.OK
    assert score.final_research_score == Decimal("79.5")

    rejected = engine.evaluate(_observation(history_bars=2), discovered_at=NOW)
    rejected_score = engine.score(
        rejected,
        liquidity=Decimal("100"),
        trend=Decimal("100"),
        momentum=Decimal("100"),
        order_flow=Decimal("100"),
        regime_compatibility=Decimal("100"),
        execution_quality=Decimal("100"),
        risk=Decimal("0"),
        manipulation_risk=Decimal("0"),
        cost_adjusted_edge=Decimal("100"),
        portfolio_fit=Decimal("100"),
        evidence=(),
        data_sources=("binance:public",),
    )
    assert rejected_score.status is ResearchStatus.REJECT
    assert rejected_score.final_research_score is None


def test_cost_engine_consumes_depth_and_does_not_double_subtract_spread() -> None:
    entry = _book(bids=(("99", "5"),), asks=(("101", "1"), ("102", "1")))
    exit_book = _book(bids=(("110", "2"),), asks=(("112", "5"),))
    estimate = FeeAndCostEngine().estimate_round_trip(
        symbol="BTCUSDT",
        quantity=Decimal("2"),
        entry_book=entry,
        expected_exit_book=exit_book,
        commission=_commission(),
        minimum_net_edge_bps=Decimal("10"),
        uncertainty_buffer_bps=Decimal("5"),
        additional_execution_buffer_bps=Decimal("0"),
        now=NOW,
    )
    assert estimate.expected_buy_fill_price == Decimal("101.5")
    assert estimate.expected_entry_cost == Decimal("203.203")
    assert estimate.expected_exit_proceeds == Decimal("219.780")
    assert estimate.expected_net_pnl == Decimal("16.577")
    assert estimate.spread_cost_quote > 0  # informational; not deducted from PnL again
    assert estimate.passes_edge


def test_cost_engine_fails_closed_on_no_data_or_insufficient_depth() -> None:
    book = _book(bids=(("99", "1"),), asks=(("101", "1"),))
    no_data = CommissionSchedule(
        symbol="BTCUSDT",
        source=FeeSource.NO_DATA,
        fetched_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )
    with pytest.raises(RiskRejectedError, match="NO_DATA"):
        FeeAndCostEngine().estimate_round_trip(
            symbol="BTCUSDT",
            quantity=Decimal("1"),
            entry_book=book,
            expected_exit_book=book,
            commission=no_data,
            minimum_net_edge_bps=Decimal("1"),
            uncertainty_buffer_bps=Decimal("1"),
            additional_execution_buffer_bps=Decimal("1"),
            now=NOW,
        )
    fill = FeeAndCostEngine.simulate_market_fill(book, side=OrderSide.BUY, quantity=Decimal("2"))
    assert not fill.fully_fillable
    assert fill.filled_quantity == Decimal("1")


def test_allocator_uses_mission_cash_risk_filters_and_external_inventory() -> None:
    config = PortfolioPolicyConfig(
        cash_reserve_pct=25,
        risk_per_trade_pct=1,
        maximum_symbol_weight_pct=20,
        liquidity_participation_pct=1,
    )
    ledger = MissionLedgerSnapshot(
        mission_id="mission-1",
        as_of=NOW,
        capital_limit=Decimal("10000"),
        available_cash=Decimal("10000"),
    )
    allocation = PortfolioAllocator(config).allocate_spot_buy(
        ledger=ledger,
        symbol="BTCUSDT",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        filters=_filters(),
        estimated_depth_quote=Decimal("500000"),
        round_trip_fee_rate=Decimal("0.002"),
        open_position_count=0,
    )
    assert allocation.status is AllocationStatus.PROPOSED
    assert allocation.quantity == Decimal("20")
    assert allocation.risk_amount == Decimal("100")

    external = ledger.model_copy(update={"external_inventory_symbols": ("BTCUSDT",)})
    rejected = PortfolioAllocator(config).allocate_spot_buy(
        ledger=external,
        symbol="BTCUSDT",
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        filters=_filters(),
        estimated_depth_quote=Decimal("500000"),
        round_trip_fee_rate=Decimal("0.002"),
        open_position_count=0,
    )
    assert rejected.status is AllocationStatus.REJECTED
    assert "external inventory" in rejected.reasons[0]


def test_mission_ledger_never_spends_external_or_unreserved_capital() -> None:
    ledger = MissionCapitalLedger.create(_mission(), now=NOW)
    reserved = MissionCapitalLedger.reserve(ledger, Decimal("1000"), now=NOW)
    filled = MissionCapitalLedger.record_buy_fill(
        reserved,
        symbol="BTCUSDT",
        quantity=Decimal("9"),
        price=Decimal("100"),
        commission_quote=Decimal("1"),
        fill_id="fill-1",
        now=NOW,
    )
    assert filled.deployed_capital == Decimal("901")
    assert filled.reserved_cash == Decimal("99")
    with pytest.raises(RiskRejectedError):
        MissionCapitalLedger.record_buy_fill(
            reserved,
            symbol="BTCUSDT",
            quantity=Decimal("11"),
            price=Decimal("100"),
            commission_quote=Decimal("1"),
            fill_id="fill-2",
        )


def test_autonomous_guard_blocks_public_and_defaults_then_only_allows_intent() -> None:
    eligibility = _eligibility()
    assert hasattr(eligibility, "eligible")
    allocation = AllocationProposal(
        mission_id="mission-1",
        symbol="BTCUSDT",
        status=AllocationStatus.PROPOSED,
        quantity=Decimal("1"),
        reference_price=Decimal("100"),
        stop_price=Decimal("95"),
        notional=Decimal("100"),
        risk_amount=Decimal("5"),
        fee_reserve=Decimal("1"),
        policy_version="test",
    )
    blocked = AutonomousGuard().assess(
        _mission(),
        eligibility,
        _commission(),
        allocation,
        AutonomousGuardState(),
        expected_net_edge_bps=Decimal("100"),
        minimum_required_edge_bps=Decimal("50"),
        now=NOW,
    )
    assert blocked.decision is MissionGuardDecision.BLOCK_NEW_ENTRIES
    assert blocked.checks["entry_mode"] is False

    good_state = AutonomousGuardState(
        exchange_connection_healthy=True,
        market_data_fresh=True,
        order_book_sequence_healthy=True,
        clock_skew_healthy=True,
        duplicate_order_absent=True,
        exit_plan_present=True,
        rate_limit_healthy=True,
        storage_healthy=True,
        retention_healthy=True,
        leader_lease_healthy=True,
        commission_policy_acceptable=True,
        daily_trade_limit_healthy=True,
        portfolio_limits_healthy=True,
        news_reliability_healthy=True,
        news_reaction_allowed=True,
        technical_confluence_healthy=True,
    )
    allowed = AutonomousGuard().assess(
        _mission(mode=TradingMode.SHADOW),
        eligibility,
        _commission(),
        allocation,
        good_state,
        expected_net_edge_bps=Decimal("100"),
        minimum_required_edge_bps=Decimal("50"),
        plan=_trade_plan(),
        now=NOW,
    )
    assert allowed.decision is MissionGuardDecision.ALLOW_INTENT
    assert "no submission authority" in allowed.reasons[0]


def test_autonomous_guard_kill_and_unknown_order_fail_closed() -> None:
    eligibility = _eligibility()
    allocation = AllocationProposal(
        mission_id="mission-1",
        symbol="BTCUSDT",
        status=AllocationStatus.REJECTED,
        reference_price=Decimal("100"),
        stop_price=Decimal("95"),
        reasons=("test",),
        policy_version="test",
    )
    killed = AutonomousGuard().assess(
        _mission(mode=TradingMode.SHADOW),
        eligibility,
        _commission(),
        allocation,
        AutonomousGuardState(kill_switch=True),
        expected_net_edge_bps=Decimal("0"),
        minimum_required_edge_bps=Decimal("50"),
        now=NOW,
    )
    unknown = AutonomousGuard().assess(
        _mission(mode=TradingMode.SHADOW),
        eligibility,
        _commission(),
        allocation,
        AutonomousGuardState(unknown_order_state=True),
        expected_net_edge_bps=Decimal("0"),
        minimum_required_edge_bps=Decimal("50"),
        now=NOW,
    )
    assert killed.decision is MissionGuardDecision.HALTED
    assert unknown.decision is MissionGuardDecision.FAILED_SAFE
