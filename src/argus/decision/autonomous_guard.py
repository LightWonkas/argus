"""Mission-aware, fail-closed autonomous GUARD, independent of execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from argus.core.intelligence import TradeHorizon, TradePlan
from argus.core.schemas import utcnow
from argus.core.trading import (
    AllocationProposal,
    AllocationStatus,
    CommissionSchedule,
    MissionGuardDecision,
    MissionGuardResult,
    MissionStatus,
    TradingMission,
    TradingMode,
    UniverseEligibilitySnapshot,
)


@dataclass(frozen=True)
class AutonomousGuardState:
    exchange_connection_healthy: bool = False
    market_data_fresh: bool = False
    account_data_fresh: bool = False
    order_stream_fresh: bool = False
    order_book_sequence_healthy: bool = False
    clock_skew_healthy: bool = False
    exchange_reconciled: bool = False
    duplicate_order_absent: bool = False
    exit_plan_present: bool = False
    rate_limit_healthy: bool = False
    storage_healthy: bool = False
    retention_healthy: bool = False
    leader_lease_healthy: bool = False
    cooldown_active: bool = False
    event_blackout: bool = False
    manipulation_quarantine: bool = False
    kill_switch: bool = False
    unknown_order_state: bool = False
    daily_loss_limit_hit: bool = False
    drawdown_limit_hit: bool = False
    daily_profit_stop_hit: bool = False
    daily_trade_limit_healthy: bool = False
    portfolio_limits_healthy: bool = False
    news_reliability_healthy: bool = False
    news_reaction_allowed: bool = False
    technical_confluence_healthy: bool = False
    live_execution_enabled: bool = False
    execution_adapter_available: bool = False
    commission_policy_acceptable: bool = False


class AutonomousGuard:
    """Collects every check; a single failed condition blocks new entries."""

    def assess(
        self,
        mission: TradingMission,
        eligibility: UniverseEligibilitySnapshot,
        commission: CommissionSchedule,
        allocation: AllocationProposal,
        state: AutonomousGuardState,
        *,
        expected_net_edge_bps: Decimal,
        minimum_required_edge_bps: Decimal,
        plan: TradePlan | None = None,
        now: datetime | None = None,
    ) -> MissionGuardResult:
        at = now or utcnow()
        checks: dict[str, bool | str | Decimal | None] = {}
        reasons: list[str] = []

        def require(name: str, condition: bool, reason: str) -> None:
            checks[name] = condition
            if not condition:
                reasons.append(reason)

        require(
            "mission_running", mission.status is MissionStatus.RUNNING, "mission is not RUNNING"
        )
        require("armed", mission.armed_at is not None, "mission is not armed")
        require(
            "entry_mode",
            mission.mode is not TradingMode.PUBLIC_MONITOR,
            "PUBLIC_MONITOR cannot produce new entry intents",
        )
        require("spot_usdt", mission.quote_asset == "USDT", "quote asset is not USDT")
        require("eligible", eligibility.eligible, "symbol is not eligible")
        require("quality", eligibility.quality_status.value == "ok", "quality is not ACCEPT")
        require("commission", commission.source.value != "no_data", "commission NO_DATA")
        require("commission_fresh", commission.valid_until >= at, "commission stale")
        require(
            "commission_policy",
            state.commission_policy_acceptable,
            "commission source not verified by policy",
        )
        require("allocation", allocation.status is AllocationStatus.PROPOSED, "allocation rejected")
        require(
            "capital",
            allocation.notional + allocation.fee_reserve <= mission.available_mission_cash,
            "mission capital limit",
        )
        require(
            "net_edge",
            expected_net_edge_bps > minimum_required_edge_bps,
            "insufficient edge after costs",
        )
        require("market_data", state.market_data_fresh, "market data stale")
        require(
            "exchange_connection",
            state.exchange_connection_healthy,
            "exchange connection unhealthy",
        )
        require("book_sequence", state.order_book_sequence_healthy, "order-book sequence unhealthy")
        require("clock_skew", state.clock_skew_healthy, "clock skew limit")
        require("duplicate_order", state.duplicate_order_absent, "duplicate/pending order present")
        require("exit_plan", state.exit_plan_present, "no exit plan")
        require("rate_limit", state.rate_limit_healthy, "rate limit unhealthy")
        require("storage", state.storage_healthy, "storage unhealthy")
        require("retention", state.retention_healthy, "retention backlog present")
        require("leader", state.leader_lease_healthy, "leader lease/fencing invalid")
        require("cooldown", not state.cooldown_active, "cooldown active")
        require("event_blackout", not state.event_blackout, "event blackout active")
        require(
            "manipulation", not state.manipulation_quarantine, "manipulation/anomaly quarantine"
        )
        require("daily_loss", not state.daily_loss_limit_hit, "daily loss limit")
        require("drawdown", not state.drawdown_limit_hit, "mission drawdown limit")
        require(
            "daily_profit_stop", not state.daily_profit_stop_hit, "daily net profit lock active"
        )
        require("daily_trade_limit", state.daily_trade_limit_healthy, "daily trade limit")
        require("portfolio_limits", state.portfolio_limits_healthy, "portfolio/position limit")
        require("news_reliability", state.news_reliability_healthy, "insufficient news reliability")
        require("news_reaction", state.news_reaction_allowed, "news-price reaction closed to entry")
        require(
            "technical_confluence",
            state.technical_confluence_healthy,
            "insufficient technical confluence",
        )
        plan_valid = (
            plan is not None
            and plan.mission_id == mission.mission_id
            and plan.symbol.upper() == eligibility.symbol.upper()
            and plan.horizon is not TradeHorizon.NO_TRADE
            and plan.created_at <= at < plan.valid_until
            and plan.submission_authorized is False
        )
        require("trade_plan", plan_valid, "no valid, entry-eligible TradePlan")

        if mission.mode in {TradingMode.TESTNET, TradingMode.LIVE_SPOT}:
            require("account_data", state.account_data_fresh, "account data stale")
            require("order_stream", state.order_stream_fresh, "user order stream stale")
            require(
                "reconciliation", state.exchange_reconciled, "exchange/local ledger not reconciled"
            )
            require("execution_adapter", state.execution_adapter_available, "no execution adapter")
        if mission.mode is TradingMode.LIVE_SPOT:
            require(
                "live_execution",
                state.live_execution_enabled,
                "live execution not separately enabled",
            )

        if state.kill_switch or state.unknown_order_state:
            decision = (
                MissionGuardDecision.FAILED_SAFE
                if state.unknown_order_state
                else MissionGuardDecision.HALTED
            )
            reasons.append(
                "unknown order state" if state.unknown_order_state else "kill switch active"
            )
        elif reasons:
            decision = MissionGuardDecision.BLOCK_NEW_ENTRIES
        else:
            decision = MissionGuardDecision.ALLOW_INTENT
            reasons.append("only persisted OrderIntent creation allowed; no submission authority")
        checks["expected_net_edge_bps"] = expected_net_edge_bps
        checks["minimum_required_edge_bps"] = minimum_required_edge_bps
        return MissionGuardResult(
            mission_id=mission.mission_id,
            symbol=eligibility.symbol,
            decision=decision,
            checks=checks,
            reasons=tuple(reasons),
        )
