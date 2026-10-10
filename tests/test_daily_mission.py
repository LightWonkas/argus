"""Realised net daily target, profit-lock and point-in-time checkpoint tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.intelligence import DailyMissionAction
from argus.core.trading import MissionStatus, TradingMission, TradingMode
from argus.decision.mission import DailyMissionPolicy
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _mission() -> TradingMission:
    return TradingMission(
        mission_id="mission-daily",
        status=MissionStatus.RUNNING,
        mode=TradingMode.SHADOW,
        capital_limit=Decimal("1000"),
        starting_equity=Decimal("1000"),
        available_mission_cash=Decimal("1000"),
        capital_limit_usdt=Decimal("1000"),
        starting_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        available_cash_usdt=Decimal("1000"),
        daily_starting_equity_usdt=Decimal("1000"),
        daily_profit_stop_pct=Decimal("10"),
        daily_loss_stop_pct=Decimal("2"),
        created_at=NOW - timedelta(minutes=5),
        started_at=NOW - timedelta(minutes=4),
        armed_at=NOW - timedelta(minutes=4),
        scheduled_end_at=NOW + timedelta(days=7),
    )


def test_daily_target_uses_realized_net_after_all_costs_not_unrealized() -> None:
    checkpoint = DailyMissionPolicy.checkpoint(
        _mission(),
        realized_gross_pnl_usdt=Decimal("115"),
        buy_commission_usdt=Decimal("3"),
        sell_commission_usdt=Decimal("3"),
        realized_slippage_usdt=Decimal("4"),
        spread_impact_usdt=Decimal("2"),
        commission_conversion_cost_usdt=Decimal("1"),
        dust_rounding_cost_usdt=Decimal("1"),
        display_conversion_cost_usdt=Decimal("1"),
        unrealized_pnl_usdt=Decimal("500"),
        now=NOW,
    )
    assert checkpoint.realized_net_pnl_usdt == Decimal("100")
    assert checkpoint.realized_net_return_pct == Decimal("10.0")
    result = DailyMissionPolicy.evaluate(checkpoint, now=NOW)
    assert result.action is DailyMissionAction.DAILY_TARGET_REACHED
    assert not result.entry_allowed
    assert result.cancel_pending_entries
    updated = DailyMissionPolicy.apply_to_mission(_mission(), checkpoint, result)
    assert updated.status is MissionStatus.DAILY_TARGET_REACHED


def test_unrealized_profit_is_not_target_but_estimated_exit_can_lock() -> None:
    checkpoint = DailyMissionPolicy.checkpoint(
        _mission(), unrealized_pnl_usdt=Decimal("500"), now=NOW
    )
    normal = DailyMissionPolicy.evaluate(checkpoint, open_position_count=1, now=NOW)
    lock = DailyMissionPolicy.evaluate(
        checkpoint,
        open_position_count=1,
        estimated_exit_net_pnl_usdt=Decimal("101"),
        now=NOW,
    )
    assert normal.action is DailyMissionAction.CONTINUE
    assert lock.action is DailyMissionAction.DAILY_PROFIT_LOCK
    assert (
        DailyMissionPolicy.apply_to_mission(_mission(), checkpoint, lock).status
        is MissionStatus.EXIT_ONLY
    )


def test_daily_loss_limit_and_checkpoint_persistence_are_point_in_time() -> None:
    checkpoint = DailyMissionPolicy.checkpoint(
        _mission(), realized_gross_pnl_usdt=Decimal("-20"), now=NOW
    )
    result = DailyMissionPolicy.evaluate(checkpoint, now=NOW)
    assert result.action is DailyMissionAction.DAILY_LOSS_LIMIT
    assert (
        DailyMissionPolicy.apply_to_mission(_mission(), checkpoint, result).status
        is MissionStatus.DAILY_LOSS_LIMIT
    )

    store = SqliteStore()
    try:
        assert store.save_mission_daily_checkpoint(checkpoint)
        assert not store.save_mission_daily_checkpoint(checkpoint)
        later = checkpoint.model_copy(update={"as_of": NOW + timedelta(minutes=1)})
        assert store.save_mission_daily_checkpoint(later)
        assert store.latest_mission_daily_checkpoint(checkpoint.mission_id) == later
    finally:
        store.close()
