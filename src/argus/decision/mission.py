"""Trading mission state machine and capital envelope operations.

Pure/deterministic functions make no exchange or network calls. After a restart an active
mission is first moved to ARMING; it cannot return to RUNNING until reconciliation completes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from argus.core.errors import RiskRejectedError
from argus.core.intelligence import (
    DailyMissionAction,
    DailyMissionEvaluation,
    MissionDailyCheckpoint,
)
from argus.core.schemas import utcnow
from argus.core.trading import (
    ArmPreflight,
    MissionInventoryItem,
    MissionLedgerSnapshot,
    MissionStatus,
    TradingMission,
    TradingMode,
)


class MissionStateMachine:
    """Enforces permitted mission transitions and fail-safe restart behaviour."""

    def __init__(
        self,
        *,
        shadow_available: bool = False,
        testnet_execution_available: bool = False,
        live_execution_available: bool = False,
    ) -> None:
        self.shadow_available = shadow_available
        self.testnet_execution_available = testnet_execution_available
        self.live_execution_available = live_execution_available

    @staticmethod
    def _at(now: datetime | None) -> datetime:
        value = now or utcnow()
        if value.tzinfo is None:
            raise ValueError("mission transition time must be tz-aware")
        return value

    def begin_arming(
        self, mission: TradingMission, *, now: datetime | None = None
    ) -> TradingMission:
        at = self._at(now)
        if mission.status not in {
            MissionStatus.CREATED,
            MissionStatus.DISARMED,
            MissionStatus.HALTED,
        }:
            raise RiskRejectedError(f"arming cannot start from status {mission.status.value}")
        if at >= mission.scheduled_end_at:
            return mission.model_copy(
                update={
                    "status": MissionStatus.FAILED_SAFE,
                    "actual_end_at": at,
                    "halt_reason": "mission duration expired before arming",
                    "revision": mission.revision + 1,
                }
            )
        return mission.model_copy(
            update={
                "status": MissionStatus.ARMING,
                "armed_at": None,
                "actual_end_at": None,
                "halt_reason": None,
                "revision": mission.revision + 1,
            }
        )

    def complete_arming(
        self,
        mission: TradingMission,
        preflight: ArmPreflight,
        *,
        now: datetime | None = None,
    ) -> TradingMission:
        at = self._at(now)
        if mission.status is not MissionStatus.ARMING:
            raise RiskRejectedError("mission must be ARMING to complete arming")
        reasons = self._preflight_failures(mission.mode, preflight)
        if reasons:
            return mission.model_copy(
                update={
                    "status": MissionStatus.FAILED_SAFE,
                    "actual_end_at": at,
                    "halt_reason": "; ".join(reasons),
                    "revision": mission.revision + 1,
                }
            )
        return mission.model_copy(
            update={
                "status": MissionStatus.ARMED,
                "armed_at": at,
                "last_reconciled_at": at if preflight.reconciliation_ok else None,
                "revision": mission.revision + 1,
            }
        )

    def start(self, mission: TradingMission, *, now: datetime | None = None) -> TradingMission:
        at = self._at(now)
        if mission.status is not MissionStatus.ARMED:
            raise RiskRejectedError("only an ARMED mission can be started")
        if at >= mission.scheduled_end_at:
            return mission.model_copy(
                update={
                    "status": MissionStatus.COMPLETED,
                    "actual_end_at": at,
                    "halt_reason": "mission duration expired before start",
                    "revision": mission.revision + 1,
                }
            )
        return mission.model_copy(
            update={
                "status": MissionStatus.RUNNING,
                "started_at": mission.started_at or at,
                "revision": mission.revision + 1,
            }
        )

    def guard_restart(self, mission: TradingMission) -> TradingMission:
        """Move an active mission back to the reconciliation gate, where no orders can be made."""
        if mission.status not in {
            MissionStatus.ARMED,
            MissionStatus.RUNNING,
            MissionStatus.DAILY_TARGET_REACHED,
            MissionStatus.DAILY_LOSS_LIMIT,
            MissionStatus.EXIT_ONLY,
            MissionStatus.HALTING,
            MissionStatus.COMPLETING,
        }:
            return mission
        return mission.model_copy(
            update={
                "status": MissionStatus.ARMING,
                "last_reconciled_at": None,
                "halt_reason": "reconciliation required after restart",
                "revision": mission.revision + 1,
            }
        )

    def resume_after_reconciliation(
        self,
        mission: TradingMission,
        *,
        successful: bool,
        now: datetime | None = None,
    ) -> TradingMission:
        at = self._at(now)
        if mission.status is not MissionStatus.ARMING or mission.armed_at is None:
            raise RiskRejectedError("restart resume requires an armed ARMING mission")
        if not successful:
            return mission.model_copy(
                update={
                    "status": MissionStatus.FAILED_SAFE,
                    "actual_end_at": at,
                    "halt_reason": "exchange/local ledger reconciliation failed",
                    "revision": mission.revision + 1,
                }
            )
        return mission.model_copy(
            update={
                "status": MissionStatus.RUNNING,
                "last_reconciled_at": at,
                "halt_reason": None,
                "revision": mission.revision + 1,
            }
        )

    def request_halt(
        self, mission: TradingMission, reason: str, *, now: datetime | None = None
    ) -> TradingMission:
        self._at(now)
        if mission.status in {MissionStatus.COMPLETED, MissionStatus.FAILED_SAFE}:
            return mission
        if not reason.strip():
            raise ValueError("halt reason cannot be empty")
        return mission.model_copy(
            update={
                "status": MissionStatus.HALTING,
                "halt_reason": reason.strip(),
                "revision": mission.revision + 1,
            }
        )

    def finish_halt(
        self, mission: TradingMission, *, now: datetime | None = None
    ) -> TradingMission:
        at = self._at(now)
        if mission.status is not MissionStatus.HALTING:
            raise RiskRejectedError("only a HALTING mission can be finished")
        return mission.model_copy(
            update={
                "status": MissionStatus.HALTED,
                "actual_end_at": at,
                "halted_at": at,
                "revision": mission.revision + 1,
            }
        )

    def apply_time_boundary(
        self,
        mission: TradingMission,
        *,
        open_position_count: int,
        now: datetime | None = None,
    ) -> TradingMission:
        at = self._at(now)
        if open_position_count < 0:
            raise ValueError("open_position_count cannot be negative")
        if mission.status is not MissionStatus.RUNNING or at < mission.scheduled_end_at:
            return mission
        status = MissionStatus.COMPLETING if open_position_count else MissionStatus.COMPLETED
        return mission.model_copy(
            update={
                "status": status,
                "actual_end_at": at if status is MissionStatus.COMPLETED else None,
                "halt_reason": "mission duration expired; new entries closed",
                "revision": mission.revision + 1,
            }
        )

    def _preflight_failures(self, mode: TradingMode, preflight: ArmPreflight) -> list[str]:
        reasons: list[str] = []
        if not preflight.explicit_user_arm:
            reasons.append("no explicit user arming approval")
        if not preflight.storage_healthy:
            reasons.append("storage is not healthy")
        if not preflight.leader_lease_held:
            reasons.append("no leader lease")
        if mode is TradingMode.SHADOW and not self.shadow_available:
            reasons.append("shadow engine disabled by config")
        if mode in {TradingMode.TESTNET, TradingMode.LIVE_SPOT}:
            available = (
                self.testnet_execution_available
                if mode is TradingMode.TESTNET
                else self.live_execution_available
            )
            if not available:
                reasons.append(f"{mode.value} execution adapter is not available")
            required = {
                "account_readable": preflight.account_readable,
                "spot_trade_permission": preflight.spot_trade_permission,
                "withdrawals_disabled_verified": preflight.withdrawals_disabled_verified,
                "transfers_disabled_verified": preflight.transfers_disabled_verified,
                "expected_account_verified": preflight.expected_account_verified,
                "reconciliation_ok": preflight.reconciliation_ok,
                "commission_current": preflight.commission_current,
                "user_stream_healthy": preflight.user_stream_healthy,
                "clock_skew_ok": preflight.clock_skew_ok,
            }
            reasons.extend(f"{name} failed" for name, value in required.items() if not value)
        if mode is TradingMode.LIVE_SPOT:
            if not preflight.live_execution_enabled:
                reasons.append("live execution disabled by config")
            if not preflight.live_environment_confirmed:
                reasons.append("no live environment opt-in")
        return reasons


class DailyMissionPolicy:
    """Applies the daily target using only realized PnL net of all costs."""

    @staticmethod
    def checkpoint(
        mission: TradingMission,
        *,
        realized_gross_pnl_usdt: Decimal = Decimal("0"),
        buy_commission_usdt: Decimal = Decimal("0"),
        sell_commission_usdt: Decimal = Decimal("0"),
        realized_slippage_usdt: Decimal = Decimal("0"),
        spread_impact_usdt: Decimal = Decimal("0"),
        commission_conversion_cost_usdt: Decimal = Decimal("0"),
        dust_rounding_cost_usdt: Decimal = Decimal("0"),
        display_conversion_cost_usdt: Decimal = Decimal("0"),
        unrealized_pnl_usdt: Decimal = Decimal("0"),
        now: datetime | None = None,
    ) -> MissionDailyCheckpoint:
        at = now or utcnow()
        if at.tzinfo is None:
            raise ValueError("daily checkpoint time must be tz-aware")
        timezone = ZoneInfo(mission.timezone)
        starting = mission.daily_starting_equity_usdt or mission.starting_equity
        return MissionDailyCheckpoint(
            mission_id=mission.mission_id,
            trading_date=at.astimezone(timezone).date(),
            timezone=mission.timezone,
            as_of=at,
            starting_equity_usdt=starting,
            realized_gross_pnl_usdt=realized_gross_pnl_usdt,
            buy_commission_usdt=buy_commission_usdt,
            sell_commission_usdt=sell_commission_usdt,
            realized_slippage_usdt=realized_slippage_usdt,
            spread_impact_usdt=spread_impact_usdt,
            commission_conversion_cost_usdt=commission_conversion_cost_usdt,
            dust_rounding_cost_usdt=dust_rounding_cost_usdt,
            display_conversion_cost_usdt=display_conversion_cost_usdt,
            unrealized_pnl_usdt=unrealized_pnl_usdt,
            daily_profit_stop_pct=mission.daily_profit_stop_pct,
            daily_loss_stop_pct=mission.daily_loss_stop_pct,
        )

    @staticmethod
    def evaluate(
        checkpoint: MissionDailyCheckpoint,
        *,
        estimated_exit_net_pnl_usdt: Decimal | None = None,
        open_position_count: int = 0,
        now: datetime | None = None,
    ) -> DailyMissionEvaluation:
        at = now or utcnow()
        if open_position_count < 0:
            raise ValueError("open_position_count cannot be negative")
        realized_return = checkpoint.realized_net_return_pct
        action = DailyMissionAction.CONTINUE
        reasons = ["unrealized PnL is not counted toward the daily target"]
        entry_allowed = True
        cancel = False
        if realized_return >= checkpoint.daily_profit_stop_pct:
            action = DailyMissionAction.DAILY_TARGET_REACHED
            entry_allowed = False
            cancel = True
            reasons.append("realized after-cost daily net target reached")
        elif realized_return <= -checkpoint.daily_loss_stop_pct:
            action = DailyMissionAction.DAILY_LOSS_LIMIT
            entry_allowed = False
            cancel = True
            reasons.append("realized daily loss limit reached")
        else:
            target_pnl = (
                checkpoint.starting_equity_usdt * checkpoint.daily_profit_stop_pct / Decimal("100")
            )
            if (
                open_position_count > 0
                and estimated_exit_net_pnl_usdt is not None
                and checkpoint.realized_net_pnl_usdt + estimated_exit_net_pnl_usdt >= target_pnl
            ):
                action = DailyMissionAction.DAILY_PROFIT_LOCK
                entry_allowed = False
                cancel = True
                reasons.append(
                    "estimated net target may be exceeded if all open risk is closed under control"
                )
        return DailyMissionEvaluation(
            mission_id=checkpoint.mission_id,
            assessed_at=at,
            action=action,
            realized_net_pnl_usdt=checkpoint.realized_net_pnl_usdt,
            realized_net_return_pct=realized_return,
            estimated_exit_net_pnl_usdt=estimated_exit_net_pnl_usdt,
            entry_allowed=entry_allowed,
            cancel_pending_entries=cancel,
            reasons=tuple(reasons),
        )

    @staticmethod
    def apply_to_mission(
        mission: TradingMission,
        checkpoint: MissionDailyCheckpoint,
        evaluation: DailyMissionEvaluation,
    ) -> TradingMission:
        if (
            mission.mission_id != checkpoint.mission_id
            or mission.mission_id != evaluation.mission_id
        ):
            raise ValueError("daily mission ids do not match")
        status = mission.status
        if evaluation.action is DailyMissionAction.DAILY_TARGET_REACHED:
            status = MissionStatus.DAILY_TARGET_REACHED
        elif evaluation.action is DailyMissionAction.DAILY_LOSS_LIMIT:
            status = MissionStatus.DAILY_LOSS_LIMIT
        elif evaluation.action is DailyMissionAction.DAILY_PROFIT_LOCK:
            status = MissionStatus.EXIT_ONLY
        return mission.model_copy(
            update={
                "status": status,
                "daily_starting_equity_usdt": checkpoint.starting_equity_usdt,
                "daily_realized_net_pnl": checkpoint.realized_net_pnl_usdt,
                "revision": mission.revision + (status is not mission.status),
            }
        )


class MissionCapitalLedger:
    """Decimal accounting that keeps the mission budget apart from the external balance."""

    @staticmethod
    def create(mission: TradingMission, *, now: datetime | None = None) -> MissionLedgerSnapshot:
        at = now or utcnow()
        return MissionLedgerSnapshot(
            mission_id=mission.mission_id,
            as_of=at,
            capital_limit=mission.capital_limit,
            available_cash=mission.available_mission_cash,
        )

    @staticmethod
    def reserve(
        ledger: MissionLedgerSnapshot,
        amount: Decimal,
        *,
        now: datetime | None = None,
    ) -> MissionLedgerSnapshot:
        if amount <= 0:
            raise ValueError("reserve amount must be positive")
        if amount > ledger.available_cash:
            raise RiskRejectedError("insufficient available mission cash")
        return ledger.model_copy(
            update={
                "as_of": now or utcnow(),
                "available_cash": ledger.available_cash - amount,
                "reserved_cash": ledger.reserved_cash + amount,
            }
        )

    @staticmethod
    def release(
        ledger: MissionLedgerSnapshot,
        amount: Decimal,
        *,
        now: datetime | None = None,
    ) -> MissionLedgerSnapshot:
        if amount <= 0 or amount > ledger.reserved_cash:
            raise ValueError("release amount must be positive and within reserved cash")
        return ledger.model_copy(
            update={
                "as_of": now or utcnow(),
                "available_cash": ledger.available_cash + amount,
                "reserved_cash": ledger.reserved_cash - amount,
            }
        )

    @staticmethod
    def record_buy_fill(
        ledger: MissionLedgerSnapshot,
        *,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        commission_quote: Decimal,
        fill_id: str,
        now: datetime | None = None,
    ) -> MissionLedgerSnapshot:
        if symbol in ledger.external_inventory_symbols:
            raise RiskRejectedError("external inventory cannot be separated from mission inventory")
        if quantity <= 0 or price <= 0 or commission_quote < 0:
            raise ValueError("invalid fill quantity/price/commission values")
        cost = quantity * price + commission_quote
        if cost > ledger.reserved_cash:
            raise RiskRejectedError("fill cost exceeds reserved mission cash")
        existing = next((item for item in ledger.mission_inventory if item.symbol == symbol), None)
        inventory = [item for item in ledger.mission_inventory if item.symbol != symbol]
        old_quantity = existing.quantity if existing is not None else Decimal("0")
        old_cost = (
            existing.average_cost * existing.quantity if existing is not None else Decimal("0")
        )
        new_quantity = old_quantity + quantity
        inventory.append(
            MissionInventoryItem(
                symbol=symbol,
                quantity=new_quantity,
                average_cost=(old_cost + quantity * price) / new_quantity,
                mark_price=price,
                provenance_fill_ids=(
                    *(existing.provenance_fill_ids if existing is not None else ()),
                    fill_id,
                ),
            )
        )
        return ledger.model_copy(
            update={
                "as_of": now or utcnow(),
                "reserved_cash": ledger.reserved_cash - cost,
                "deployed_capital": ledger.deployed_capital + cost,
                "commissions": ledger.commissions + commission_quote,
                "mission_inventory": tuple(inventory),
            }
        )
