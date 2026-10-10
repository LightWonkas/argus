"""Crypto orchestration: offline REST reconciliation, Data Quality Gate, error isolation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from argus.api.app import _crypto_dashboard_from_store
from argus.config.models import QualityConfig, RuntimeConfig, UniverseConfig
from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.crypto import (
    FundingRate,
    FuturesTicker,
    LongShortRatio,
    OpenInterest,
    OrderBookDelta,
    OrderBookLevel,
    OrderBookSnapshot,
    SpotTicker,
    Trade,
)
from argus.core.enums import MarketType, OrderSide, RiskDecision, Timeframe
from argus.core.schemas import DataStatus, Provenance
from argus.interface.crypto_runtime import (
    CryptoSurveillanceCycle,
    CryptoWebSocketService,
    StreamRuntimeMetrics,
)
from argus.platform.quality import QualityDecision
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_stream_batch_flush_does_not_block_event_loop() -> None:
    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(FakeREST(), store, _config("BTCUSDT"), clock=lambda: NOW)
        original = cycle._flush_stream_batch

        def slow_flush(
            records: list[object],
            trade_rollups: list[object],
            book_rollups: list[object],
            market_type: MarketType,
        ) -> None:
            del records, trade_rollups, book_rollups, market_type
            time.sleep(0.1)

        cycle._flush_stream_batch = slow_flush  # type: ignore[method-assign,assignment]
        started = time.monotonic()
        task = asyncio.create_task(cycle._flush_stream_batch_async([], [], [], MarketType.SPOT))
        await asyncio.sleep(0.01)
        heartbeat_elapsed = time.monotonic() - started
        await task

        assert heartbeat_elapsed < 0.05
        cycle._flush_stream_batch = original  # type: ignore[method-assign]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancel_during_record_processing_explicitly_closes_provider_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosableStream:
        def __init__(self) -> None:
            self.closed = False
            self.yielded = False

        def __aiter__(self) -> ClosableStream:
            return self

        async def __anext__(self) -> Trade:
            if self.yielded:
                await asyncio.Event().wait()
            self.yielded = True
            return Trade(
                exchange="binance",
                market_type=MarketType.SPOT,
                symbol="BTCUSDT",
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                trade_id="close-regression",
                side=OrderSide.BUY,
                price="60000",
                quantity="1",
            )

        async def aclose(self) -> None:
            self.closed = True

    class Provider:
        def __init__(self, stream: ClosableStream) -> None:
            self._stream = stream

        def stream(self, symbols: tuple[str, ...], market_type: MarketType) -> ClosableStream:
            assert symbols == ("BTCUSDT",)
            assert market_type is MarketType.SPOT
            return self._stream

    store = SqliteStore()
    try:
        config = _config("BTCUSDT")
        config.ingestion.stream_commit_batch_size = 1
        cycle = CryptoSurveillanceCycle(FakeREST(), store, config, clock=lambda: NOW)
        entered_flush = asyncio.Event()
        flush_calls = 0

        async def blocking_flush(
            records: list[object],
            trade_rollups: list[object],
            book_rollups: list[object],
            market_type: MarketType,
        ) -> None:
            nonlocal flush_calls
            del market_type
            flush_calls += 1
            if flush_calls == 1:
                entered_flush.set()
                await asyncio.Event().wait()
            records.clear()
            trade_rollups.clear()
            book_rollups.clear()

        monkeypatch.setattr(cycle, "_flush_stream_batch_async", blocking_flush)
        closable = ClosableStream()
        consume = asyncio.create_task(
            cycle.consume_websocket(Provider(closable), MarketType.SPOT)  # type: ignore[arg-type]
        )
        await entered_flush.wait()
        consume.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consume

        assert closable.closed is True
        assert flush_calls == 2
    finally:
        store.close()


class FakeREST:
    def __init__(self, *, fail_symbol: str | None = None, stale: bool = False) -> None:
        self.fail_symbol = fail_symbol
        self.event_time = NOW - timedelta(minutes=5) if stale else NOW

    def check_clock_skew(self, market_type: MarketType = MarketType.SPOT) -> int:
        return 0

    def fetch_spot_ticker(self, symbol: str) -> SpotTicker:
        if symbol == self.fail_symbol:
            raise RuntimeError("spot unavailable")
        return SpotTicker(
            exchange="binance",
            symbol=symbol,
            event_timestamp=self.event_time,
            ingestion_timestamp=NOW,
            source="fake:rest",
            last_price=Decimal("60000"),
            bid=Decimal("59999"),
            ask=Decimal("60001"),
            quote_volume_24h=Decimal("100000000"),
        )

    def fetch_futures_ticker(self, symbol: str) -> FuturesTicker:
        return FuturesTicker(
            exchange="binance",
            symbol=symbol,
            event_timestamp=self.event_time,
            ingestion_timestamp=NOW,
            source="fake:rest",
            last_price=Decimal("60010"),
            mark_price=Decimal("60006"),
        )

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 500,
    ) -> OHLCVSeries:
        bars = []
        for index in range(40):
            price = Decimal("59000") + index
            bars.append(
                OHLCVBar(
                    timestamp=NOW - timedelta(seconds=timeframe.seconds * (39 - index)),
                    open=price,
                    high=price + Decimal("0.5"),
                    low=price - Decimal("0.5"),
                    close=price + Decimal("0.4"),
                    volume=Decimal(100 + index),
                )
            )
        return OHLCVSeries(
            symbol=symbol,
            timeframe=timeframe,
            bars=tuple(bars),
            provenance=Provenance(source="fake:rest", timestamp=NOW, confidence=1),
            exchange="binance",
            market_type=market_type,
        )

    def fetch_order_book(
        self,
        symbol: str,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 100,
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            exchange="binance",
            market_type=market_type,
            symbol=symbol,
            event_timestamp=self.event_time,
            ingestion_timestamp=NOW,
            source="fake:rest",
            sequence=10,
            bids=(OrderBookLevel(price=Decimal("59999"), quantity=Decimal("10")),),
            asks=(OrderBookLevel(price=Decimal("60001"), quantity=Decimal("1")),),
        )

    def fetch_funding_rate(self, symbol: str) -> FundingRate:
        return FundingRate(
            exchange="binance",
            symbol=symbol,
            event_timestamp=self.event_time,
            ingestion_timestamp=NOW,
            source="fake:rest",
            rate=Decimal("0.0001"),
        )

    def fetch_open_interest(self, symbol: str) -> OpenInterest:
        return OpenInterest(
            exchange="binance",
            symbol=symbol,
            event_timestamp=self.event_time,
            ingestion_timestamp=NOW,
            source="fake:rest",
            contracts=Decimal("1000"),
        )

    def fetch_long_short_ratio(self, symbol: str, *, period: str = "5m") -> LongShortRatio:
        return LongShortRatio(
            exchange="binance",
            symbol=symbol,
            event_timestamp=self.event_time,
            ingestion_timestamp=NOW,
            source="fake:rest",
            long_ratio=Decimal("0.55"),
            short_ratio=Decimal("0.45"),
            account_ratio=Decimal("1.2"),
        )


def _config(*symbols: str) -> RuntimeConfig:
    config = RuntimeConfig(
        universe=UniverseConfig(symbols=list(symbols), strict=True),
        timeframes=["1h"],
        quality=QualityConfig(min_bars=30),
    )
    # Tests that verify the raw-record behaviour opt in explicitly.
    config.ingestion.persist_raw_trades = True
    config.ingestion.persist_raw_depth_deltas = True
    config.ingestion.order_book_materialize_interval_s = 1.0
    return config


def test_reconnect_rate_burst_lifetime_and_transport_cleanup_metrics() -> None:
    metrics = StreamRuntimeMetrics(started_at=datetime.now(tz=UTC) - timedelta(hours=1))
    metrics.worker_started()
    metrics.ws_transport_opened()
    for lifetime in (10.0, 20.0, 30.0, 40.0):
        metrics.ws_reconnect(
            {
                "worker_id": "spot:combined",
                "reconnect_reason": "REMOTE_CLOSE",
                "connection_lifetime_seconds": lifetime,
                "backoff_seconds": 1.0,
            }
        )
    metrics.ws_transport_closed()
    payload = metrics.as_payload()

    assert payload["reconnect_burst_max_60s"] == 4
    assert payload["reconnect_per_worker_per_hour"] == pytest.approx(4, rel=0.01)
    assert payload["connection_lifetime_p50_s"] == 25
    assert payload["unexplained_connection_lifetime_samples"] == 4
    assert payload["unexplained_connection_lifetime_p50_s"] == 25
    assert payload["reconnect_without_backoff_total"] == 0
    assert payload["active_connections"] == 0


def test_local_shutdown_lifetime_is_not_an_unexplained_reconnect_sample() -> None:
    metrics = StreamRuntimeMetrics(started_at=datetime.now(tz=UTC) - timedelta(hours=1))
    metrics.ws_connection_event(
        {
            "worker_id": "spot:combined",
            "reconnect_reason": "LOCAL_SHUTDOWN",
            "connection_lifetime_seconds": 5.0,
            "backoff_seconds": 0.0,
        }
    )

    payload = metrics.as_payload()
    assert payload["connection_lifetime_p50_s"] == 5.0
    assert payload["unexplained_connection_lifetime_samples"] == 0
    assert payload["unexplained_connection_lifetime_p50_s"] is None


def test_crypto_cycle_persists_features_candidate_and_guard_without_execution() -> None:
    store = SqliteStore()
    try:
        store.save_crypto_record(
            Trade(
                exchange="binance",
                market_type=MarketType.SPOT,
                symbol="BTCUSDT",
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                trade_id="buy-1",
                side=OrderSide.BUY,
                price=Decimal("60000"),
                quantity=Decimal("2"),
            )
        )
        cycle = CryptoSurveillanceCycle(FakeREST(), store, _config("BTCUSDT"), clock=lambda: NOW)
        result = cycle.run_once()[0]

        assert result.data_status is DataStatus.OK
        assert result.quality_decision is QualityDecision.ACCEPT
        assert store.latest_crypto_artifact("BTCUSDT", "quality") is not None
        assert store.latest_crypto_artifact("BTCUSDT", "features") is not None
        assert store.latest_crypto_artifact("BTCUSDT", "regime") is not None
        experts = store.latest_crypto_artifact("BTCUSDT", "experts")
        assert experts is not None
        assert any(item["role"] == "manager" for item in experts[2])
        candidate = store.latest_trade_candidate("BTCUSDT")
        assert candidate is not None
        assert candidate.execution_enabled is False
        guard = store.latest_risk_assessment("BTCUSDT")
        assert guard is not None
        assert guard.decision is RiskDecision.REJECT
        assert store.shadow_positions() == []
        dashboard = _crypto_dashboard_from_store(store, _config("BTCUSDT"), "BTCUSDT")
        assert dashboard.data_status is DataStatus.OK
        assert "backtest" in dashboard.missing_data
    finally:
        store.close()


def test_crypto_cycle_versions_features_with_cycle_time_inside_same_bar() -> None:
    store = SqliteStore()
    clock = {"now": NOW}
    try:
        cycle = CryptoSurveillanceCycle(
            FakeREST(), store, _config("BTCUSDT"), clock=lambda: clock["now"]
        )
        assert cycle.run_once()[0].quality_decision is QualityDecision.ACCEPT
        first = store.latest_crypto_artifact("BTCUSDT", "features")
        assert first is not None and "trades" in first[2]["missing_data"]

        clock["now"] = NOW + timedelta(seconds=1)
        store.save_crypto_record(
            Trade(
                exchange="binance",
                market_type=MarketType.SPOT,
                symbol="BTCUSDT",
                event_timestamp=clock["now"],
                ingestion_timestamp=clock["now"],
                source="fake:websocket",
                trade_id="new-order-flow",
                side=OrderSide.BUY,
                price="60000",
                quantity="1",
            )
        )
        assert cycle.run_once()[0].quality_decision is QualityDecision.ACCEPT
        second = store.latest_crypto_artifact("BTCUSDT", "features")

        assert second is not None
        assert second[0] == clock["now"]
        assert "trades" not in second[2]["missing_data"]
        assert (
            store._conn.execute(
                "SELECT COUNT(*) FROM crypto_artifact "
                "WHERE symbol='BTCUSDT' AND artifact_type='features'"
            ).fetchone()[0]
            == 2
        )
    finally:
        store.close()


def test_new_universe_symbol_waits_for_news_coverage_without_news_error() -> None:
    store = SqliteStore()
    try:
        store.set_checkpoint(
            "news_intelligence:health",
            json.dumps(
                {
                    "coverage_by_symbol": {
                        "ETHUSDT": "no_relevant_news",
                    }
                }
            ),
        )
        cycle = CryptoSurveillanceCycle(FakeREST(), store, _config("BTCUSDT"), clock=lambda: NOW)

        result = cycle.run_once()[0]

        assert result.quality_decision is QualityDecision.ACCEPT
        assert store.latest_coin_research_results() == []
        assert store.latest_trade_plans() == []
        assert store.latest_crypto_artifact("BTCUSDT", "intraday_research_error") is None
        pending = store.latest_crypto_artifact("BTCUSDT", "news_coverage_pending")
        assert pending is not None
        assert pending[1] is DataStatus.PENDING
    finally:
        store.close()


def test_crypto_cycle_blocks_stale_context_before_features() -> None:
    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(
            FakeREST(stale=True), store, _config("BTCUSDT"), clock=lambda: NOW
        )
        result = cycle.run_once()[0]
        assert result.quality_decision is QualityDecision.QUARANTINE
        assert result.data_status is DataStatus.QUARANTINED
        assert store.latest_crypto_artifact("BTCUSDT", "quality") is not None
        assert store.latest_crypto_artifact("BTCUSDT", "features") is None
        assert store.latest_trade_candidate("BTCUSDT") is None
    finally:
        store.close()


def test_crypto_cycle_isolates_symbol_failure() -> None:
    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(
            FakeREST(fail_symbol="ETHUSDT"),
            store,
            _config("BTCUSDT", "ETHUSDT"),
            clock=lambda: NOW,
        )
        results = cycle.run_once()
        assert results[0].symbol == "BTCUSDT" and results[0].data_status is DataStatus.OK
        assert results[1].symbol == "ETHUSDT" and results[1].data_status is DataStatus.REJECTED
        assert "spot unavailable" in results[1].errors[0]
    finally:
        store.close()


async def test_websocket_resync_failure_isolated_from_next_symbol() -> None:
    class FailingSnapshotREST(FakeREST):
        def fetch_order_book(
            self,
            symbol: str,
            *,
            market_type: MarketType = MarketType.SPOT,
            limit: int = 100,
        ) -> OrderBookSnapshot:
            if symbol == "BTCUSDT":
                raise RuntimeError("snapshot unavailable")
            return super().fetch_order_book(symbol, market_type=market_type, limit=limit)

    class TwoSymbolStream:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[OrderBookDelta | Trade]:
            assert symbols == ("BTCUSDT", "ETHUSDT")
            yield OrderBookDelta(
                exchange="binance",
                market_type=market_type,
                symbol="BTCUSDT",
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                sequence=11,
                first_sequence=11,
                final_sequence=11,
                bids=(OrderBookLevel(price="59999", quantity="1"),),
                asks=(OrderBookLevel(price="60001", quantity="1"),),
            )
            yield Trade(
                exchange="binance",
                market_type=market_type,
                symbol="ETHUSDT",
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                trade_id="eth-after-btc-failure",
                side=OrderSide.BUY,
                price="4000",
                quantity="1",
            )

    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(
            FailingSnapshotREST(),
            store,
            _config("BTCUSDT", "ETHUSDT"),
            clock=lambda: NOW,
        )
        consumed = await cycle.consume_websocket(
            TwoSymbolStream(),
            MarketType.SPOT,
            symbols=("BTCUSDT", "ETHUSDT"),
        )

        assert consumed == 2
        assert store.latest_crypto_record("BTCUSDT", "OrderBookDelta") is not None
        assert store.latest_crypto_record("ETHUSDT", "Trade") is not None
    finally:
        store.close()


async def test_websocket_materializes_reconstructed_book_at_bounded_interval() -> None:
    class DepthStream:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[OrderBookDelta]:
            assert symbols == ("BTCUSDT",)
            for offset, elapsed in enumerate((0.0, 0.5, 1.1), start=11):
                timestamp = NOW + timedelta(seconds=elapsed)
                yield OrderBookDelta(
                    exchange="binance",
                    market_type=market_type,
                    symbol="BTCUSDT",
                    event_timestamp=timestamp,
                    ingestion_timestamp=timestamp,
                    source="fake:websocket",
                    sequence=offset,
                    first_sequence=offset,
                    final_sequence=offset,
                    bids=(OrderBookLevel(price="59999", quantity=str(offset)),),
                    asks=(OrderBookLevel(price="60001", quantity="1"),),
                )

    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(FakeREST(), store, _config("BTCUSDT"), clock=lambda: NOW)
        consumed = await cycle.consume_websocket(
            DepthStream(), MarketType.SPOT, symbols=("BTCUSDT",)
        )

        assert consumed == 3
        assert store.crypto_event_count(record_type="OrderBookDelta") == 3
        # One authoritative REST snapshot plus one 1-second sampled reconstruction.
        assert store.crypto_event_count(record_type="OrderBookSnapshot") == 2
        assert store.get_checkpoint("health.exchange_websocket.state") == "running"
    finally:
        store.close()


async def test_cancelled_stream_drains_queue_and_releases_order_book_state() -> None:
    release = asyncio.Event()

    class BlockingDepthStream:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[OrderBookDelta]:
            yield OrderBookDelta(
                exchange="binance",
                market_type=market_type,
                symbol=symbols[0],
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                sequence=11,
                first_sequence=11,
                final_sequence=11,
                bids=(OrderBookLevel(price="59999", quantity="2"),),
                asks=(OrderBookLevel(price="60001", quantity="1"),),
            )
            await release.wait()

    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(FakeREST(), store, _config("BTCUSDT"), clock=lambda: NOW)
        task = asyncio.create_task(
            cycle.consume_websocket(BlockingDepthStream(), MarketType.SPOT, symbols=("BTCUSDT",))
        )
        for _ in range(100):
            if cycle.stream_metrics.active_books == 1:
                break
            await asyncio.sleep(0)
        assert cycle.stream_metrics.active_books == 1

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert cycle.stream_metrics.active_books == 0
        assert cycle.stream_metrics.queue_backlog_current == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_gap_cancelled_during_shutdown_is_fully_accounted() -> None:
    import threading

    release = threading.Event()

    class BlockingSecondSnapshot(FakeREST):
        calls = 0

        def fetch_order_book(
            self,
            symbol: str,
            *,
            market_type: MarketType = MarketType.SPOT,
            limit: int = 100,
        ) -> OrderBookSnapshot:
            self.calls += 1
            if self.calls == 2:
                release.wait(timeout=5)
            return super().fetch_order_book(symbol, market_type=market_type, limit=limit)

    class GappedStream:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[OrderBookDelta]:
            for sequence in (11, 13):
                yield OrderBookDelta(
                    exchange="binance",
                    market_type=market_type,
                    symbol=symbols[0],
                    event_timestamp=NOW,
                    ingestion_timestamp=NOW,
                    source="fake:websocket",
                    sequence=sequence,
                    first_sequence=sequence,
                    final_sequence=sequence,
                    bids=(OrderBookLevel(price="59999", quantity="2"),),
                    asks=(OrderBookLevel(price="60001", quantity="1"),),
                )

    store = SqliteStore()
    try:
        rest = BlockingSecondSnapshot()
        cycle = CryptoSurveillanceCycle(rest, store, _config("BTCUSDT"), clock=lambda: NOW)
        task = asyncio.create_task(
            cycle.consume_websocket(GappedStream(), MarketType.SPOT, symbols=("BTCUSDT",))
        )
        for _ in range(1000):
            if rest.calls == 2:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()

        metrics = cycle.stream_metrics.as_payload()
        assert metrics["ws_sequence_gaps_total"] == 1
        assert metrics["ws_resync_aborted_during_shutdown_total"] == 1
        assert metrics["ws_pending_resyncs"] == 0
        assert metrics["ws_gap_accounting_reconciled"] is True
    finally:
        release.set()
        store.close()


async def test_worker_task_registry_cleanup_survives_one_hundred_rotations() -> None:
    async def worker() -> None:
        await asyncio.Event().wait()

    for _ in range(100):
        tasks = {asyncio.create_task(worker()) for _worker in range(4)}
        await asyncio.sleep(0)
        await CryptoWebSocketService._cancel_tasks(tasks)
        assert tasks == set()


@pytest.mark.asyncio
async def test_service_uses_one_combined_worker_per_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteStore()
    cycle = CryptoSurveillanceCycle(
        FakeREST(), store, _config("BTCUSDT", "ETHUSDT"), clock=lambda: NOW
    )
    service = CryptoWebSocketService(cycle, FakeREST())  # type: ignore[arg-type]
    calls: list[tuple[MarketType, tuple[str, ...]]] = []

    async def finish(market: MarketType, symbols: tuple[str, ...]) -> None:
        calls.append((market, symbols))

    monkeypatch.setattr(service, "_run_worker", finish)
    try:
        await service.run()
        assert sorted(market.value for market, _symbols in calls) == ["perpetual", "spot"]
        assert all(symbols == ("BTCUSDT", "ETHUSDT") for _market, symbols in calls)
    finally:
        service.close()


async def test_websocket_snapshot_fetch_does_not_block_event_loop() -> None:
    class BlockingSnapshotREST(FakeREST):
        def fetch_order_book(
            self,
            symbol: str,
            *,
            market_type: MarketType = MarketType.SPOT,
            limit: int = 100,
        ) -> OrderBookSnapshot:
            time.sleep(0.1)
            return super().fetch_order_book(symbol, market_type=market_type, limit=limit)

    class OneDepthEvent:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[OrderBookDelta]:
            yield OrderBookDelta(
                exchange="binance",
                market_type=market_type,
                symbol=symbols[0],
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                sequence=11,
                first_sequence=11,
                final_sequence=11,
                bids=(OrderBookLevel(price="59999", quantity="2"),),
                asks=(OrderBookLevel(price="60001", quantity="1"),),
            )

    store = SqliteStore()
    try:
        cycle = CryptoSurveillanceCycle(
            BlockingSnapshotREST(), store, _config("BTCUSDT"), clock=lambda: NOW
        )
        consume = asyncio.create_task(
            cycle.consume_websocket(OneDepthEvent(), MarketType.SPOT, symbols=("BTCUSDT",))
        )
        await asyncio.sleep(0.03)
        assert not consume.done()
        assert await consume == 1
    finally:
        store.close()


async def test_websocket_compact_mode_keeps_rollups_without_raw_trade_or_delta() -> None:
    class CompactStream:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[Trade | OrderBookDelta]:
            assert symbols == ("BTCUSDT",)
            for trade_id in ("100", "101"):
                yield Trade(
                    exchange="binance",
                    market_type=market_type,
                    symbol="BTCUSDT",
                    event_timestamp=NOW,
                    ingestion_timestamp=NOW,
                    source="fake:websocket",
                    trade_id=trade_id,
                    side=OrderSide.BUY,
                    price="60000",
                    quantity="1",
                )
            yield OrderBookDelta(
                exchange="binance",
                market_type=market_type,
                symbol="BTCUSDT",
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="fake:websocket",
                sequence=11,
                first_sequence=11,
                final_sequence=11,
                bids=(OrderBookLevel(price="59999", quantity="2"),),
                asks=(OrderBookLevel(price="60001", quantity="1"),),
            )

    store = SqliteStore()
    try:
        config = _config("BTCUSDT")
        config.ingestion.persist_raw_trades = False
        config.ingestion.persist_raw_depth_deltas = False
        cycle = CryptoSurveillanceCycle(FakeREST(), store, config, clock=lambda: NOW)

        assert (
            await cycle.consume_websocket(CompactStream(), MarketType.SPOT, symbols=("BTCUSDT",))
            == 3
        )
        assert store.crypto_event_count(record_type="Trade") == 0
        assert store.crypto_event_count(record_type="OrderBookDelta") == 0
        assert store.crypto_event_count(record_type="OrderBookSnapshot") == 1
        assert store.trade_rollups("BTCUSDT")[0].trade_count == 2
        assert store.book_metric_rollups("BTCUSDT")[0].samples == 1
        metrics_raw = store.get_checkpoint("crypto:websocket:metrics")
        assert metrics_raw is not None
        metrics = json.loads(metrics_raw)
        assert metrics["total_records"] == 3
        assert metrics["by_record_type"] == {"OrderBookDelta": 1, "Trade": 2}
        assert metrics["snapshot_reconciliations"] == 1
        assert metrics["sequence_gaps"] == 0
        assert metrics["batch_write_failures"] == 0
    finally:
        store.close()


async def test_websocket_service_marks_graceful_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "stream-state.db"
    store = SqliteStore(database)
    cycle = CryptoSurveillanceCycle(FakeREST(), store, _config("BTCUSDT"), clock=lambda: NOW)
    service = CryptoWebSocketService(cycle, FakeREST())  # type: ignore[arg-type]

    async def stop_immediately(_market_type: MarketType, _symbol: str) -> None:
        return None

    monkeypatch.setattr(service, "_run_worker", stop_immediately)
    await service.run()
    service.close()

    reopened = SqliteStore(database)
    try:
        assert reopened.get_checkpoint("health.exchange_websocket.state") == "stopped"
    finally:
        reopened.close()


async def test_websocket_sqlite_write_failure_does_not_drop_following_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TradeStream:
        async def stream(
            self, symbols: tuple[str, ...], market_type: MarketType
        ) -> AsyncIterator[Trade]:
            assert symbols == ("BTCUSDT",)
            for trade_id in ("fails", "persists"):
                yield Trade(
                    exchange="binance",
                    market_type=market_type,
                    symbol="BTCUSDT",
                    event_timestamp=NOW,
                    ingestion_timestamp=NOW,
                    source="fake:websocket",
                    trade_id=trade_id,
                    side=OrderSide.BUY,
                    price="60000",
                    quantity="1",
                )

    store = SqliteStore()
    original_save = store.save_crypto_record
    calls = 0

    def flaky_save(record: object, *, commit: bool = True) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("simulated write failure")
        return original_save(record, commit=commit)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "save_crypto_record", flaky_save)
    try:
        config = _config("BTCUSDT")
        config.ingestion.stream_commit_batch_size = 1
        cycle = CryptoSurveillanceCycle(FakeREST(), store, config, clock=lambda: NOW)
        consumed = await cycle.consume_websocket(
            TradeStream(), MarketType.SPOT, symbols=("BTCUSDT",)
        )

        assert consumed == 2
        persisted = store.latest_crypto_record("BTCUSDT", "Trade")
        assert isinstance(persisted, Trade)
        assert persisted.trade_id == "persists"
    finally:
        store.close()
