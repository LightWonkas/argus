"""Regression tests for mission audit/fencing and bounded SQLite retention."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from argus.config.models import RetentionConfig
from argus.core.crypto import OrderBookDelta, OrderBookLevel, OrderBookSnapshot, SpotTicker, Trade
from argus.core.enums import MarketType, OrderSide
from argus.core.errors import ConcurrentStateError
from argus.core.trading import (
    ExitPlan,
    LedgerEntryKind,
    MissionAuditEvent,
    MissionLedgerEntry,
    MissionLedgerSnapshot,
    MissionStatus,
    OrderIntent,
    TradingMission,
    TradingMode,
)
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _mission() -> TradingMission:
    return TradingMission(
        mission_id="mission-1",
        status=MissionStatus.DISARMED,
        mode=TradingMode.PUBLIC_MONITOR,
        capital_limit=Decimal("1000"),
        starting_equity=Decimal("1000"),
        available_mission_cash=Decimal("1000"),
        created_at=NOW,
        scheduled_end_at=NOW + timedelta(days=7),
    )


def _event(event_type: str, revision: int) -> MissionAuditEvent:
    return MissionAuditEvent(
        mission_id="mission-1",
        event_type=event_type,
        occurred_at=NOW + timedelta(seconds=revision),
        idempotency_key=f"mission-1:{event_type}:{revision}",
    )


def _meta(event_at: datetime) -> dict[str, object]:
    return {
        "exchange": "binance",
        "market_type": MarketType.SPOT,
        "symbol": "BTCUSDT",
        "event_timestamp": event_at,
        "ingestion_timestamp": event_at + timedelta(milliseconds=10),
        "source": "binance:public-websocket",
    }


def _book(event_at: datetime, *, crossed: bool = False) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        **_meta(event_at),
        sequence=100,
        bids=(OrderBookLevel(price="101" if crossed else "99", quantity="2"),),
        asks=(OrderBookLevel(price="100", quantity="3"),),
    )


def test_mission_transition_is_atomic_revisioned_and_append_only(tmp_path: Path) -> None:
    path = tmp_path / "mission.db"
    mission = _mission()
    with SqliteStore(path) as store:
        assert store.create_trading_mission(mission, _event("created", 1))
        assert not store.create_trading_mission(mission, _event("created", 1))
        arming = mission.model_copy(update={"status": MissionStatus.ARMING, "revision": 2})
        store.transition_trading_mission(arming, _event("arming", 2), expected_revision=1)
        assert store.load_trading_mission("mission-1") == arming
        assert [event.event_type for event in store.mission_events("mission-1")] == [
            "created",
            "arming",
        ]
        stale = arming.model_copy(update={"status": MissionStatus.ARMED, "revision": 2})
        with pytest.raises(ConcurrentStateError):
            store.transition_trading_mission(stale, _event("stale", 3), expected_revision=1)

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE mission_event SET event_type='tampered'")
    finally:
        connection.close()


def test_ledger_entry_idempotency_does_not_apply_second_snapshot(tmp_path: Path) -> None:
    initial = MissionLedgerSnapshot(
        mission_id="mission-1",
        as_of=NOW,
        capital_limit=Decimal("1000"),
        available_cash=Decimal("900"),
        reserved_cash=Decimal("100"),
    )
    entry = MissionLedgerEntry(
        mission_id="mission-1",
        kind=LedgerEntryKind.CASH_RESERVED,
        occurred_at=NOW,
        amount_quote=Decimal("100"),
        idempotency_key="reserve-1",
    )
    with SqliteStore(tmp_path / "ledger.db") as store:
        assert store.save_mission_ledger(initial, entry=entry)
        altered = initial.model_copy(
            update={"available_cash": Decimal("800"), "reserved_cash": Decimal("200")}
        )
        assert not store.save_mission_ledger(altered, entry=entry)
        loaded = store.load_mission_ledger("mission-1")
        assert loaded is not None
        assert loaded.available_cash == Decimal("900")


def test_leader_lease_fences_second_process_and_increments_after_expiry(tmp_path: Path) -> None:
    path = tmp_path / "lease.db"
    with SqliteStore(path) as first, SqliteStore(path) as second:
        lease = first.acquire_leader_lease("mission-1", "worker-a", lease_seconds=30, now=NOW)
        assert lease is not None and lease.fencing_token == 1
        assert (
            second.acquire_leader_lease(
                "mission-1",
                "worker-b",
                lease_seconds=30,
                now=NOW + timedelta(seconds=10),
            )
            is None
        )
        takeover = second.acquire_leader_lease(
            "mission-1",
            "worker-b",
            lease_seconds=30,
            now=NOW + timedelta(seconds=31),
        )
        assert takeover is not None and takeover.fencing_token == 2
        assert (
            first.renew_leader_lease(lease, lease_seconds=30, now=NOW + timedelta(seconds=32))
            is None
        )


def test_order_intent_client_id_is_idempotent_but_collision_is_rejected(tmp_path: Path) -> None:
    exit_plan = ExitPlan(
        initial_stop=Decimal("95"),
        take_profit_policy="fixed",
        trailing_policy="off",
        time_stop=NOW + timedelta(hours=1),
        thesis_invalidation="support lost",
        maximum_holding_seconds=3600,
        emergency_exit_policy="fail closed",
    )
    intent = OrderIntent(
        intent_id="intent-1",
        client_order_id="argus-mission-1-1",
        mission_id="mission-1",
        candidate_id="candidate-1",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        mode=TradingMode.SHADOW,
        created_at=NOW,
        owner_id="worker-a",
        fencing_token=1,
        guard_result_id="guard-1",
        exit_plan=exit_plan,
    )
    with SqliteStore(tmp_path / "intent.db") as store:
        assert store.save_order_intent(intent)
        assert not store.save_order_intent(intent)
        collision = intent.model_copy(update={"intent_id": "intent-2"})
        with pytest.raises(ConcurrentStateError):
            store.save_order_intent(collision)
        assert store.order_intents("mission-1") == [intent]


def test_retention_rolls_up_before_bounded_delete_and_is_idempotent(tmp_path: Path) -> None:
    old = NOW - timedelta(hours=2)
    config = RetentionConfig(
        enabled=True,
        raw_trade_hours=1,
        raw_depth_hours=1,
        raw_book_snapshot_hours=1,
        raw_ticker_hours=1,
        maintenance_batch_size=100,
        minimum_free_disk_mb=256,
    )
    with SqliteStore(tmp_path / "retention.db") as store:
        for index, (price, side) in enumerate(
            (("100", OrderSide.BUY), ("102", OrderSide.SELL), ("101", OrderSide.BUY))
        ):
            store.save_crypto_record(
                Trade(
                    **_meta(old + timedelta(seconds=index)),
                    trade_id=f"trade-{index}",
                    side=side,
                    price=price,
                    quantity="2",
                )
            )
        store.save_crypto_record(
            Trade(
                **_meta(NOW),
                trade_id="current",
                side=OrderSide.BUY,
                price="110",
                quantity="1",
            )
        )
        store.save_crypto_record(_book(old))
        store.save_crypto_record(SpotTicker(**_meta(old), last_price="100"))
        store.save_crypto_record(SpotTicker(**_meta(NOW), last_price="110"))
        store.save_crypto_record(
            OrderBookDelta(
                **_meta(old + timedelta(milliseconds=100)),
                sequence=101,
                first_sequence=101,
                final_sequence=101,
                previous_final_sequence=100,
                bids=(OrderBookLevel(price="99", quantity="1"),),
            )
        )

        report = store.run_crypto_retention(config, now=NOW)
        assert report.success
        assert report.trade_events_deleted == 3
        assert report.book_snapshots_deleted == 1
        assert report.book_deltas_deleted == 1
        assert report.ticker_events_deleted == 1
        assert store.crypto_event_count(record_type="Trade") == 1
        trade_rollup = store.trade_rollups("BTCUSDT")[0]
        assert trade_rollup.trade_count == 3
        assert trade_rollup.open_price == Decimal("100")
        assert trade_rollup.high_price == Decimal("102")
        assert trade_rollup.close_price == Decimal("101")
        assert trade_rollup.buy_quantity == Decimal("4")
        assert store.book_metric_rollups("BTCUSDT")[0].samples == 1
        assert store.crypto_event_count(record_type="SpotTicker") == 1

        repeated = store.run_crypto_retention(config, now=NOW)
        assert repeated.success
        assert repeated.trade_events_deleted == 0
        assert store.trade_rollups("BTCUSDT")[0] == trade_rollup
        assert store.storage_health(config, now=NOW).healthy


def test_live_stream_rollup_cursor_makes_replayed_batch_idempotent(tmp_path: Path) -> None:
    trade = Trade(
        **_meta(NOW),
        trade_id="12345",
        side=OrderSide.BUY,
        price="100",
        quantity="2",
    )
    book = _book(NOW)
    with SqliteStore(tmp_path / "stream-rollup.db") as store:
        assert store.save_stream_rollups([trade], [book]) == (1, 1)
        assert store.save_stream_rollups([trade], [book]) == (0, 0)

        trade_rollup = store.trade_rollups("BTCUSDT")[0]
        book_rollup = store.book_metric_rollups("BTCUSDT")[0]
        assert trade_rollup.trade_count == 1
        assert trade_rollup.source_event_count == 1
        assert trade_rollup.buy_quantity == Decimal("2")
        assert book_rollup.samples == 1
        assert book_rollup.source_event_count == 1


def test_retention_failure_rolls_back_without_deleting_raw_event(tmp_path: Path) -> None:
    config = RetentionConfig(
        enabled=True,
        raw_trade_hours=1,
        raw_depth_hours=1,
        raw_book_snapshot_hours=1,
        maintenance_batch_size=100,
        minimum_free_disk_mb=256,
    )
    with SqliteStore(tmp_path / "rollback.db") as store:
        store.save_crypto_record(_book(NOW - timedelta(hours=2), crossed=True))
        report = store.run_crypto_retention(config, now=NOW)
        assert not report.success
        assert "crossed" in (report.reason or "")
        assert store.crypto_event_count(record_type="OrderBookSnapshot") == 1
        assert store.book_metric_rollups("BTCUSDT") == []


def test_disabled_retention_is_visible_and_never_deletes(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "disabled.db") as store:
        store.save_crypto_record(
            Trade(
                **_meta(NOW - timedelta(days=2)),
                trade_id="old",
                side=OrderSide.BUY,
                price="100",
                quantity="1",
            )
        )
        config = RetentionConfig(enabled=False)
        report = store.run_crypto_retention(config, now=NOW)
        health = store.storage_health(config, now=NOW)
        assert report.success and not report.enabled
        assert report.trade_events_deleted == 0
        assert store.crypto_event_count(record_type="Trade") == 1
        assert not health.healthy
        assert "retention disabled" in health.reasons[0]
