"""Durable, fail-closed mission control service (simulation/research only).

The service reads recorded point-in-time state for the UI/API. This build has no private
account or spot execution adapter, so those are reported explicitly as ``NO_DATA`` and no
order submission call is offered: missions are simulated ("shadow") or public monitoring,
with no live orders.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from argus.api.trading_models import (
    KillSwitchResponse,
    MissionCreateRequest,
    MissionDetailResponse,
    MissionListResponse,
    MissionMutationResponse,
    TradingAccountResponse,
    TradingCostsResponse,
    TradingFillsResponse,
    TradingHealthResponse,
    TradingNewsResponse,
    TradingOrdersResponse,
    TradingPerformanceResponse,
    TradingPlansResponse,
    TradingPositionsResponse,
    TradingResearchResponse,
    TradingRiskResponse,
    TradingStatusResponse,
    TradingStorageResponse,
    TradingUniverseResponse,
    TradingWatchlistResponse,
)
from argus.config.models import RuntimeConfig
from argus.core.errors import RiskRejectedError
from argus.core.schemas import DataStatus, utcnow
from argus.core.trading import (
    ArmPreflight,
    LedgerEntryKind,
    MissionAuditEvent,
    MissionLedgerEntry,
    MissionStatus,
    ResearchStatus,
    TradingMission,
    TradingMode,
)
from argus.decision.mission import DailyMissionPolicy, MissionCapitalLedger, MissionStateMachine
from argus.observability.readiness import MemoryHealthSnapshot
from argus.storage.sqlite import SqliteStore

_ACTIVE = {
    MissionStatus.ARMING,
    MissionStatus.ARMED,
    MissionStatus.RUNNING,
    MissionStatus.DAILY_TARGET_REACHED,
    MissionStatus.DAILY_LOSS_LIMIT,
    MissionStatus.EXIT_ONLY,
    MissionStatus.HALTING,
    MissionStatus.COMPLETING,
}


class TradingControlService:
    """Mission management; safely closed because there is no execution capability."""

    def __init__(
        self,
        store: SqliteStore,
        config_provider: Callable[[], RuntimeConfig],
        *,
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.config_provider = config_provider
        self.owner_id = owner_id or f"{socket.gethostname()}-{uuid4().hex[:12]}"

    def _config(self) -> RuntimeConfig:
        return self.config_provider()

    def _missions(self) -> list[TradingMission]:
        return self.store.list_trading_missions(limit=500)

    def _active_mission(self) -> TradingMission | None:
        return next((mission for mission in self._missions() if mission.status in _ACTIVE), None)

    def _kill_switch(self) -> bool:
        raw = self.store.get_checkpoint("trading:kill_switch")
        if raw is None:
            return False
        try:
            return bool(json.loads(raw).get("enabled"))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            # A corrupt safety checkpoint is never treated as "switch off".
            return True

    @staticmethod
    def _event(mission_id: str, event_type: str, actor: str, **details: str) -> MissionAuditEvent:
        at = utcnow()
        return MissionAuditEvent(
            mission_id=mission_id,
            event_type=event_type,
            occurred_at=at,
            idempotency_key=f"{event_type}:{mission_id}:{uuid4().hex}",
            details={"actor": actor, **details},
        )

    def recover_after_restart(self) -> tuple[str, ...]:
        """Move active missions to ARMING, which is closed to submission."""
        recovered: list[str] = []
        machine = MissionStateMachine()
        for mission in self._missions():
            guarded = machine.guard_restart(mission)
            if guarded.revision == mission.revision:
                continue
            event = self._event(
                mission.mission_id,
                "restart_reconciliation_required",
                self.owner_id,
                previous_status=mission.status.value,
            )
            self.store.transition_trading_mission(
                guarded, event, expected_revision=mission.revision
            )
            recovered.append(mission.mission_id)
        return tuple(recovered)

    def status(self) -> TradingStatusResponse:
        config = self._config()
        active = self._active_mission()
        kill_switch = self._kill_switch()
        reasons = [
            "private account adapter not connected",
            "user data stream not connected",
            "no spot execution adapter in this build",
        ]
        if kill_switch:
            reasons.insert(0, "kill switch enabled")
        return TradingStatusResponse(
            mode=config.trading.mode,
            armed=active is not None
            and active.status in {MissionStatus.ARMED, MissionStatus.RUNNING},
            kill_switch=kill_switch,
            active_mission_id=active.mission_id if active is not None else None,
            active_mission_status=active.status.value if active is not None else None,
            public_market_data_available=any(
                exchange.enabled and exchange.public_data_only for exchange in config.exchanges
            ),
            live_execution_enabled=config.trading.live_execution_enabled,
            execution_backend=config.trading.execution_backend,
            data_status=DataStatus.QUARANTINED if kill_switch else DataStatus.PENDING,
            reasons=tuple(reasons),
        )

    @staticmethod
    def account() -> TradingAccountResponse:
        return TradingAccountResponse(
            status=ResearchStatus.NO_DATA,
            reasons=("private account adapter is not connected in this build",),
        )

    def universe(self, *, limit: int = 100, cursor: str | None = None) -> TradingUniverseResponse:
        page, total, eligible, as_of, next_cursor = self.store.latest_universe_snapshot_page(
            limit=limit,
            cursor=cursor,
        )
        snapshots = tuple(page)
        return TradingUniverseResponse(
            as_of=as_of,
            scanned_count=total,
            eligible_count=eligible,
            returned_count=len(snapshots),
            page_limit=max(1, min(limit, 500)),
            next_cursor=next_cursor,
            snapshots=snapshots,
            data_status=DataStatus.OK if total else DataStatus.PENDING,
        )

    def research(self) -> TradingResearchResponse:
        scores = tuple(self.store.latest_research_scores(limit=500))
        coin_results = tuple(self.store.latest_coin_research_results(limit=500))
        status = (
            DataStatus.OK
            if any(row.status is ResearchStatus.OK for row in scores)
            or any(row.data_status is DataStatus.OK for row in coin_results)
            else (DataStatus.PENDING)
        )
        return TradingResearchResponse(
            scores=scores,
            coin_results=coin_results,
            data_status=status,
        )

    def news(self) -> TradingNewsResponse:
        records = tuple(self.store.latest_crypto_news(limit=500))
        reactions = tuple(self.store.latest_news_market_reactions(limit=500))
        raw_health = self.store.get_checkpoint("news_intelligence:health")
        health: dict[str, object] | None = None
        if raw_health is not None:
            try:
                parsed = json.loads(raw_health)
                if isinstance(parsed, dict):
                    health = {str(key): value for key, value in parsed.items()}
            except (TypeError, ValueError, json.JSONDecodeError):
                health = {"data_status": "rejected", "reason": "corrupt health checkpoint"}
        health_status = DataStatus.PENDING
        if health is not None:
            try:
                health_status = DataStatus(str(health.get("data_status", "pending")))
            except ValueError:
                health_status = DataStatus.REJECTED
        status = (
            DataStatus.OK
            if any(row.data_status is DataStatus.OK for row in records)
            else health_status
        )
        return TradingNewsResponse(
            records=records,
            reactions=reactions,
            provider_health=health,
            data_status=status,
        )

    def plans(self) -> TradingPlansResponse:
        active = self._active_mission()
        plans = tuple(
            self.store.latest_trade_plans(
                mission_id=active.mission_id if active is not None else None,
                limit=500,
            )
        )
        return TradingPlansResponse(
            plans=plans,
            data_status=DataStatus.OK if plans else DataStatus.PENDING,
        )

    def watchlist(self) -> TradingWatchlistResponse:
        config = self._config()
        raw = self.store.get_checkpoint("dynamic_universe:watchlist")
        if raw is not None:
            try:
                payload = json.loads(raw)
                symbols = tuple(str(value).upper() for value in payload["symbols"])
                changed_at = payload.get("changed_at")
                return TradingWatchlistResponse(
                    symbols=symbols,
                    changed_at=changed_at,
                    dynamic_discovery_enabled=config.universe_discovery.enabled,
                    data_status=DataStatus.OK,
                    source="stored_checkpoint",
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        return TradingWatchlistResponse(
            symbols=tuple(config.universe.symbols),
            dynamic_discovery_enabled=config.universe_discovery.enabled,
            data_status=DataStatus.PENDING,
            source="configured_fallback",
        )

    def missions(self) -> MissionListResponse:
        return MissionListResponse(missions=tuple(self._missions()))

    def mission(self, mission_id: str) -> MissionDetailResponse | None:
        mission = self.store.load_trading_mission(mission_id)
        if mission is None:
            return None
        return MissionDetailResponse(
            mission=mission,
            ledger=self.store.load_mission_ledger(mission_id),
            events=tuple(self.store.mission_events(mission_id)),
            order_intents=tuple(self.store.order_intents(mission_id)),
            daily_checkpoint=self.store.latest_mission_daily_checkpoint(mission_id),
            plans=tuple(self.store.latest_trade_plans(mission_id=mission_id)),
        )

    def positions(self) -> TradingPositionsResponse:
        active = self._active_mission()
        ledger = self.store.load_mission_ledger(active.mission_id) if active is not None else None
        inventory = (
            tuple(item.model_dump(mode="json") for item in ledger.mission_inventory)
            if ledger is not None
            else ()
        )
        shadow = tuple(self.store.shadow_positions()) if self._config().shadow.enabled else ()
        has_data = bool(inventory or shadow)
        return TradingPositionsResponse(
            mission_id=active.mission_id if active is not None else None,
            mission_inventory=inventory,
            shadow_positions=shadow,
            data_status=DataStatus.OK if has_data else DataStatus.PENDING,
            reason=("recorded positions" if has_data else "no position/fill data"),
        )

    def orders(self) -> TradingOrdersResponse:
        active = self._active_mission()
        intents = tuple(self.store.order_intents(active.mission_id)) if active is not None else ()
        return TradingOrdersResponse(
            mission_id=active.mission_id if active is not None else None,
            intents=intents,
            data_status=DataStatus.OK if intents else DataStatus.PENDING,
            reason=(
                "durable order intent records only; no submission adapter"
                if intents
                else "no order intent or exchange order data"
            ),
        )

    def fills(self) -> TradingFillsResponse:
        active = self._active_mission()
        return TradingFillsResponse(
            mission_id=active.mission_id if active is not None else None,
            data_status=DataStatus.PENDING,
            reason="private exchange fill source not connected",
        )

    def performance(self) -> TradingPerformanceResponse:
        active = self._active_mission()
        if active is None:
            return TradingPerformanceResponse(
                data_status=DataStatus.PENDING,
                reason="no active mission and no realized fill data",
            )
        ledger = self.store.load_mission_ledger(active.mission_id)
        entries = self.store.mission_ledger_entries(active.mission_id)
        has_fill = any(
            entry.kind in {LedgerEntryKind.BUY_FILL, LedgerEntryKind.SELL_FILL} for entry in entries
        )
        if ledger is None or not has_fill:
            return TradingPerformanceResponse(
                mission_id=active.mission_id,
                data_status=DataStatus.PENDING,
                reason="no realized mission fill data; zero performance is not fabricated",
            )
        net_pnl = ledger.realized_pnl + ledger.unrealized_pnl
        gross_pnl = net_pnl + ledger.commissions + ledger.slippage_cost
        return TradingPerformanceResponse(
            mission_id=active.mission_id,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            realized_pnl=ledger.realized_pnl,
            unrealized_pnl=ledger.unrealized_pnl,
            total_commission=ledger.commissions,
            estimated_slippage=ledger.slippage_cost,
            data_status=DataStatus.OK,
            reason="verified fill entries in the mission ledger",
        )

    def costs(self) -> TradingCostsResponse:
        config = self._config()
        schedules = tuple(self.store.commission_schedules(limit=500))
        verified = [schedule for schedule in schedules if schedule.taker_rate is not None]
        return TradingCostsResponse(
            schedules=schedules,
            conservative_estimate_enabled=config.fees.allow_conservative_config_fallback,
            conservative_maker_bps=Decimal(str(config.fees.conservative_maker_bps)),
            conservative_taker_bps=Decimal(str(config.fees.conservative_taker_bps)),
            uncertainty_buffer_bps=Decimal(str(config.fees.model_uncertainty_buffer_bps)),
            live_eligible=False,
            data_status=(
                DataStatus.OK
                if verified or config.fees.allow_conservative_config_fallback
                else DataStatus.PENDING
            ),
            reason=(
                "durable commission schedules"
                if verified
                else (
                    "conservative PUBLIC_MONITOR/SHADOW estimate; not suitable for live trading"
                    if config.fees.allow_conservative_config_fallback
                    else "no verified current commission rate; trading is blocked"
                )
            ),
        )

    def risk(self) -> TradingRiskResponse:
        results = tuple(self.store.latest_mission_guard_results(limit=200))
        return TradingRiskResponse(
            kill_switch=self._kill_switch(),
            guard_results=results,
            data_status=DataStatus.OK if results else DataStatus.PENDING,
            reason=("durable GUARD decisions" if results else "no GUARD evaluation"),
        )

    def storage(self) -> TradingStorageResponse:
        health = self.store.storage_health(self._config().retention)
        return TradingStorageResponse(
            health=health,
            last_retention=self.store.latest_retention_report(),
        )

    def health(self) -> TradingHealthResponse:
        at = utcnow()
        config = self._config()
        storage = self.store.storage_health(config.retention)
        last_market = self.store.latest_market_timestamp()
        market_age = (at - last_market).total_seconds() if last_market is not None else None
        market_fresh = market_age is not None and 0 <= market_age <= config.quality.crypto_max_age_s
        raw_news = self.store.get_checkpoint("news_intelligence:health")
        news_status = DataStatus.PENDING
        memory: MemoryHealthSnapshot | None = None
        if raw_news is not None:
            try:
                payload = json.loads(raw_news)
                news_status = DataStatus(str(payload.get("data_status", "pending")))
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                news_status = DataStatus.REJECTED
        raw_memory = self.store.get_checkpoint("diagnostics:memory")
        if raw_memory is not None:
            try:
                memory = MemoryHealthSnapshot.model_validate_json(raw_memory)
            except ValueError:
                memory = None
        reasons = [
            "private account/user-stream/reconciliation NO_DATA",
            "execution backend disabled",
        ]
        if not market_fresh:
            reasons.append("public market data missing or stale")
        if news_status is not DataStatus.OK:
            reasons.append("news providers NO_DATA/degraded")
        if not storage.healthy:
            reasons.extend(storage.reasons)
        overall = (
            "ok"
            if market_fresh and news_status is DataStatus.OK and storage.healthy
            else ("fail" if not storage.healthy else "degraded")
        )
        return TradingHealthResponse(
            checked_at=at,
            overall=overall,
            last_market_data_at=last_market,
            public_market_data=DataStatus.OK if market_fresh else DataStatus.PENDING,
            news_providers=news_status,
            storage=storage,
            memory=memory,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def create_mission(
        self, request: MissionCreateRequest, *, actor: str
    ) -> MissionMutationResponse:
        config = self._config()
        if self._kill_switch():
            raise RiskRejectedError("kill switch enabled; a new mission cannot be created")
        if self._active_mission() is not None:
            raise RiskRejectedError("only one active mission is supported at a time")
        if request.duration_days > config.trading.maximum_mission_days:
            raise RiskRejectedError("mission duration exceeds the configured limit")
        if request.mode is not config.trading.mode:
            raise RiskRejectedError("requested mode does not match the active configuration mode")
        if request.mode in {TradingMode.TESTNET, TradingMode.LIVE_SPOT}:
            raise RiskRejectedError("no private execution adapter in this build")
        if request.mode is TradingMode.SHADOW and not config.shadow.enabled:
            raise RiskRejectedError("shadow is disabled by config")
        now = utcnow()
        capital_limit = request.canonical_capital_usdt
        conversion_age = (
            (now - request.conversion_rate_at).total_seconds()
            if request.conversion_rate_at is not None
            else None
        )
        if conversion_age is not None and (
            conversion_age < -config.quality.max_clock_skew_s
            or conversion_age > config.mission_policy.maximum_conversion_age_s
        ):
            raise RiskRejectedError("TRY/USDT conversion data is stale or in the future")
        mission = TradingMission(
            mode=request.mode,
            display_currency=request.display_currency,
            input_budget_try=request.input_budget_try,
            try_per_usdt=request.try_per_usdt,
            conversion_rate_source=request.conversion_rate_source,
            conversion_rate_at=request.conversion_rate_at,
            capital_limit=capital_limit,
            starting_equity=capital_limit,
            available_mission_cash=capital_limit,
            capital_limit_usdt=capital_limit,
            starting_equity_usdt=capital_limit,
            current_equity_usdt=capital_limit,
            available_cash_usdt=capital_limit,
            daily_starting_equity_usdt=capital_limit,
            daily_profit_stop_pct=request.daily_profit_stop_pct
            or Decimal(str(config.mission_policy.daily_profit_stop_pct)),
            daily_loss_stop_pct=request.daily_loss_stop_pct
            or Decimal(str(config.mission_policy.daily_loss_stop_pct)),
            maximum_drawdown_pct=request.maximum_drawdown_pct
            or Decimal(str(config.mission_policy.maximum_drawdown_pct)),
            timezone=request.timezone or config.mission_policy.timezone,
            created_at=now,
            scheduled_end_at=now + timedelta(days=request.duration_days),
            end_policy=request.end_policy,
        )
        ledger = MissionCapitalLedger.create(mission, now=now)
        event = self._event(
            mission.mission_id,
            "mission_created_disarmed",
            actor,
            mode=mission.mode.value,
            duration_days=str(request.duration_days),
        )
        entry = MissionLedgerEntry(
            mission_id=mission.mission_id,
            kind=LedgerEntryKind.CAPITAL_ALLOCATED,
            occurred_at=now,
            amount_quote=capital_limit,
            idempotency_key=f"capital:{mission.mission_id}",
            metadata={"actor": actor},
        )
        daily_checkpoint = DailyMissionPolicy.checkpoint(mission, now=now)
        if not self.store.create_trading_mission(
            mission,
            event,
            ledger=ledger,
            ledger_entry=entry,
            daily_checkpoint=daily_checkpoint,
        ):
            raise RiskRejectedError("mission id collision")
        return MissionMutationResponse(
            mission=mission,
            message="mission created DISARMED; no order authority was granted",
        )

    def arm_mission(
        self, mission_id: str, *, expected_revision: int, actor: str
    ) -> MissionMutationResponse:
        config = self._config()
        if self._kill_switch():
            raise RiskRejectedError("kill switch enabled; the mission cannot be armed")
        mission = self.store.load_trading_mission(mission_id)
        if mission is None:
            raise KeyError(mission_id)
        if mission.revision != expected_revision:
            raise RiskRejectedError("mission revision changed; re-read the current state")
        if mission.mode is not config.trading.mode:
            raise RiskRejectedError("mission mode no longer matches the active config")
        lease = self.store.acquire_leader_lease(mission.mission_id, self.owner_id, lease_seconds=60)
        storage_health = self.store.storage_health(config.retention)
        machine = MissionStateMachine(shadow_available=config.shadow.enabled)
        arming = machine.begin_arming(mission)
        self.store.transition_trading_mission(
            arming,
            self._event(mission_id, "mission_arming", actor),
            expected_revision=mission.revision,
        )
        preflight = ArmPreflight(
            explicit_user_arm=True,
            live_execution_enabled=False,
            leader_lease_held=lease is not None,
            storage_healthy=storage_health.healthy,
        )
        armed = machine.complete_arming(arming, preflight)
        self.store.transition_trading_mission(
            armed,
            self._event(
                mission_id,
                "mission_armed" if armed.status is MissionStatus.ARMED else "arming_failed_safe",
                actor,
                reason=armed.halt_reason or "preflight passed",
            ),
            expected_revision=arming.revision,
        )
        if armed.status is not MissionStatus.ARMED:
            return MissionMutationResponse(
                mission=armed,
                message=f"arming fail-safe: {armed.halt_reason}",
            )
        running = machine.start(armed)
        self.store.transition_trading_mission(
            running,
            self._event(mission_id, "mission_started", actor, mode=running.mode.value),
            expected_revision=armed.revision,
        )
        return MissionMutationResponse(
            mission=running,
            message=(
                "public monitoring mission started; this mode produces no order intents/submissions"
                if running.mode is TradingMode.PUBLIC_MONITOR
                else "mission started"
            ),
        )

    def halt_mission(
        self, mission_id: str, *, expected_revision: int, reason: str, actor: str
    ) -> MissionMutationResponse:
        mission = self.store.load_trading_mission(mission_id)
        if mission is None:
            raise KeyError(mission_id)
        if mission.revision != expected_revision:
            raise RiskRejectedError("mission revision changed; re-read the current state")
        machine = MissionStateMachine()
        halting = machine.request_halt(mission, reason)
        self.store.transition_trading_mission(
            halting,
            self._event(mission_id, "mission_halt_requested", actor, reason=reason),
            expected_revision=mission.revision,
        )
        # This build has no live execution; with empty mission inventory the halt is immediate.
        ledger = self.store.load_mission_ledger(mission_id)
        has_inventory = ledger is not None and any(
            item.quantity > Decimal("0") for item in ledger.mission_inventory
        )
        if has_inventory:
            return MissionMutationResponse(
                mission=halting,
                message="mission HALTING; waiting for the recorded inventory exit policy",
            )
        halted = machine.finish_halt(halting)
        self.store.transition_trading_mission(
            halted,
            self._event(mission_id, "mission_halted", actor, reason=reason),
            expected_revision=halting.revision,
        )
        return MissionMutationResponse(mission=halted, message="mission halted safely")

    def enable_kill_switch(self, *, reason: str, actor: str) -> KillSwitchResponse:
        at = utcnow()
        self.store.set_checkpoint(
            "trading:kill_switch",
            json.dumps(
                {
                    "enabled": True,
                    "reason": reason,
                    "actor": actor,
                    "at": at.isoformat(),
                },
                sort_keys=True,
            ),
        )
        halted: list[str] = []
        for mission in self._missions():
            if mission.status not in _ACTIVE:
                continue
            machine = MissionStateMachine()
            halting = machine.request_halt(mission, f"kill switch: {reason}", now=at)
            self.store.transition_trading_mission(
                halting,
                self._event(mission.mission_id, "kill_switch_halt", actor, reason=reason),
                expected_revision=mission.revision,
            )
            ledger = self.store.load_mission_ledger(mission.mission_id)
            has_inventory = ledger is not None and any(
                item.quantity > Decimal("0") for item in ledger.mission_inventory
            )
            if not has_inventory:
                stopped = machine.finish_halt(halting, now=at)
                self.store.transition_trading_mission(
                    stopped,
                    self._event(mission.mission_id, "kill_switch_halted", actor, reason=reason),
                    expected_revision=halting.revision,
                )
            halted.append(mission.mission_id)
        return KillSwitchResponse(
            enabled=True,
            halted_mission_ids=tuple(halted),
            message="kill switch is permanently enabled; it cannot be disabled through the API",
        )


__all__ = ["TradingControlService"]
