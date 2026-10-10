"""Crypto REST reconciliation + WebSocket recording orchestration.

This layer keeps the provider SDK out of the platform/decision layers. Each symbol is
processed independently; when the quality gate closes, no feature, agent, TradeCandidate
or GUARD output is produced. There is no live order path, and the shadow engine is not
run automatically here.
"""

from __future__ import annotations

import asyncio
import json
import time
from bisect import bisect_left
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Protocol

from argus.brain.crypto_agents import DeterministicCryptoExperts, synthesize_manager
from argus.config.models import RuntimeConfig
from argus.core.contracts import OHLCVSeries
from argus.core.crypto import (
    BasisSnapshot,
    CryptoAnalysisContext,
    CryptoMarketRecord,
    FundingRate,
    FuturesTicker,
    LiquidationEvent,
    LongShortRatio,
    OpenInterest,
    OrderBookDelta,
    OrderBookSnapshot,
    SpotTicker,
    Trade,
    TradeCandidate,
)
from argus.core.enums import MarketType, OrderSide, Stance, Timeframe
from argus.core.errors import DataQualityError, SequenceGapError
from argus.core.schemas import DataStatus, utcnow
from argus.decision.guard import GuardRiskEngine, GuardState
from argus.ingestion.binance import OrderBookReconciler
from argus.interface.intraday_research import IntradayResearchCoordinator
from argus.observability.logging import get_logger
from argus.platform.crypto_features import classify_market_regime, compute_crypto_features
from argus.platform.crypto_triggers import CryptoTriggerEngine, qualifies_trade_candidate
from argus.platform.intraday_features import closed_bars
from argus.platform.quality import CryptoQualityGate, QualityDecision, QualityGate
from argus.storage.sqlite import SqliteStore

log = get_logger(__name__)


class CryptoRESTClient(Protocol):
    def check_clock_skew(self, market_type: MarketType = MarketType.SPOT) -> int: ...

    def fetch_spot_ticker(self, symbol: str) -> SpotTicker: ...

    def fetch_futures_ticker(self, symbol: str) -> FuturesTicker: ...

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 500,
    ) -> OHLCVSeries: ...

    def fetch_order_book(
        self,
        symbol: str,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 100,
    ) -> OrderBookSnapshot: ...

    def fetch_funding_rate(self, symbol: str) -> FundingRate: ...

    def fetch_open_interest(self, symbol: str) -> OpenInterest: ...

    def fetch_long_short_ratio(self, symbol: str, *, period: str = "5m") -> LongShortRatio: ...


class CryptoWebSocketClient(Protocol):
    def stream(
        self, symbols: tuple[str, ...], market_type: MarketType
    ) -> AsyncIterator[CryptoMarketRecord]: ...


class UniverseCycle(Protocol):
    def run_once(self) -> object: ...


@dataclass(frozen=True)
class CryptoCycleResult:
    symbol: str
    data_status: DataStatus
    quality_decision: QualityDecision
    records_saved: int = 0
    series_saved: int = 0
    candidate_id: str | None = None
    errors: tuple[str, ...] = ()


@dataclass
class _FixedHistogram:
    """Constant-memory latency distribution for whole-process percentiles."""

    bounds: tuple[float, ...] = (
        -30_000,
        -10_000,
        -5_000,
        -2_000,
        -1_000,
        -500,
        -250,
        -100,
        0,
        10,
        25,
        50,
        100,
        250,
        500,
        1_000,
        2_000,
        5_000,
        10_000,
        30_000,
        60_000,
    )
    counts: list[int] = field(default_factory=lambda: [0] * 22)
    total: int = 0
    maximum: float | None = None

    def observe(self, value: float) -> None:
        index = min(bisect_left(self.bounds, value), len(self.bounds))
        self.counts[index] += 1
        self.total += 1
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def percentile(self, fraction: float) -> float | None:
        if self.total == 0:
            return None
        target = max(1, int(self.total * fraction + 0.999999))
        seen = 0
        for index, count in enumerate(self.counts):
            seen += count
            if seen >= target:
                if index < len(self.bounds):
                    return self.bounds[index]
                return round(self.maximum or 0.0, 3)
        return round(self.maximum or 0.0, 3)


@dataclass
class StreamRuntimeMetrics:
    """Durable checkpoint view of the compact, process-local WS counters."""

    started_at: datetime
    total_records: int = 0
    ws_events_received_total: int = 0
    ws_events_dropped_total: int = 0
    ws_parse_errors_total: int = 0
    ws_invalid_price_total: int = 0
    ws_invalid_quantity_total: int = 0
    ws_status_events_total: int = 0
    ws_reconnects_total: int = 0
    ws_connection_closures_total: int = 0
    reconnect_reasons: dict[str, int] = field(default_factory=dict)
    reconnect_by_worker: dict[str, int] = field(default_factory=dict)
    connection_closure_reasons: dict[str, int] = field(default_factory=dict)
    reconnect_burst_max_60s: int = 0
    reconnect_without_backoff_total: int = 0
    active_connections: int = 0
    active_connections_max: int = 0
    by_record_type: dict[str, int] = field(default_factory=dict)
    by_stream: dict[str, int] = field(default_factory=dict)
    snapshot_reconciliations: int = 0
    sequence_gaps: int = 0
    resyncs: int = 0
    resync_failures: int = 0
    resync_aborted_during_shutdown: int = 0
    pending_resyncs: int = 0
    stale_book_continuations: int = 0
    batch_write_failures: int = 0
    latency_samples: int = 0
    latency_sum_ms: float = 0.0
    latency_max_abs_ms: float = 0.0
    last_event_at: datetime | None = None
    queue_backlog_current: int = 0
    queue_backlog_max: int = 0
    batch_flushes: int = 0
    batch_latency_sum_ms: float = 0.0
    batch_latency_max_ms: float = 0.0
    batch_wait_samples: int = 0
    batch_wait_sum_ms: float = 0.0
    batch_wait_max_ms: float = 0.0
    active_books: int = 0
    active_books_max: int = 0
    active_workers: int = 0
    active_workers_max: int = 0
    maximum_stream_labels: int = 2048
    evicted_stream_labels: int = 0
    clock_skew_ms: dict[str, float] = field(default_factory=dict)
    _raw_latency_ms: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    _adjusted_latency_ms: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    _queue_wait_ms: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    _raw_latency_histogram: _FixedHistogram = field(default_factory=_FixedHistogram)
    _adjusted_latency_histogram: _FixedHistogram = field(default_factory=_FixedHistogram)
    _queue_wait_histogram: _FixedHistogram = field(default_factory=_FixedHistogram)
    _batch_wait_histogram: _FixedHistogram = field(default_factory=_FixedHistogram)
    _connection_lifetimes_s: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    _unexplained_connection_lifetimes_s: deque[float] = field(
        default_factory=lambda: deque(maxlen=4096)
    )
    _reconnect_events: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=2048))
    _recent_reconnect_monotonic: dict[str, deque[float]] = field(default_factory=dict)

    def observe(self, record: CryptoMarketRecord) -> None:
        record_type = type(record).__name__
        stream = f"{record.market_type.value}:{record.symbol}:{record_type}"
        self.total_records += 1
        self.by_record_type[record_type] = self.by_record_type.get(record_type, 0) + 1
        if stream in self.by_stream or len(self.by_stream) < self.maximum_stream_labels:
            self.by_stream[stream] = self.by_stream.get(stream, 0) + 1
        else:
            self.evicted_stream_labels += 1
        latency_ms = (record.ingestion_timestamp - record.event_timestamp).total_seconds() * 1000
        skew_ms = self.clock_skew_ms.get(record.market_type.value, 0.0)
        adjusted_latency_ms = latency_ms + skew_ms
        queue_wait_ms = max(
            0.0,
            (self.last_event_clock() - record.ingestion_timestamp).total_seconds() * 1000,
        )
        self.latency_samples += 1
        self.latency_sum_ms += latency_ms
        self.latency_max_abs_ms = max(self.latency_max_abs_ms, abs(latency_ms))
        self._raw_latency_ms.append(latency_ms)
        self._adjusted_latency_ms.append(adjusted_latency_ms)
        self._queue_wait_ms.append(queue_wait_ms)
        self._raw_latency_histogram.observe(latency_ms)
        self._adjusted_latency_histogram.observe(adjusted_latency_ms)
        self._queue_wait_histogram.observe(queue_wait_ms)
        self.last_event_at = record.ingestion_timestamp

    @staticmethod
    def last_event_clock() -> datetime:
        return datetime.now(tz=UTC)

    def update_clock_skew(self, market_type: MarketType, skew_ms: float) -> None:
        self.clock_skew_ms[market_type.value] = skew_ms

    def ws_received(self) -> None:
        self.ws_events_received_total += 1

    def ws_dropped(
        self,
        *,
        invalid_price: bool = False,
        invalid_quantity: bool = False,
    ) -> None:
        self.ws_events_dropped_total += 1
        if invalid_price:
            self.ws_invalid_price_total += 1
        if invalid_quantity:
            self.ws_invalid_quantity_total += 1

    def ws_status_event(self) -> None:
        self.ws_status_events_total += 1

    def ws_parse_error(self) -> None:
        self.ws_parse_errors_total += 1

    def ws_reconnect(self, event: dict[str, object] | None = None) -> None:
        self.ws_reconnects_total += 1
        if event is None:
            event = {
                "worker_id": "unassigned",
                "reconnect_reason": "UNKNOWN",
                "connection_lifetime_seconds": 0.0,
                "backoff_seconds": 0.0,
            }
        self._record_connection_event(event, reconnect=True)

    def ws_connection_event(self, event: dict[str, object]) -> None:
        self._record_connection_event(event, reconnect=False)

    def _record_connection_event(self, event: dict[str, object], *, reconnect: bool) -> None:
        reason = str(event.get("reconnect_reason", "UNKNOWN"))
        worker_id = str(event.get("worker_id", "unassigned"))
        lifetime = event.get("connection_lifetime_seconds")
        if isinstance(lifetime, (int, float)):
            self._connection_lifetimes_s.append(max(0.0, float(lifetime)))
        self.ws_connection_closures_total += 1
        self.connection_closure_reasons[reason] = self.connection_closure_reasons.get(reason, 0) + 1
        if not reconnect:
            return
        if reason not in {"LOCAL_SHUTDOWN", "PROACTIVE_RENEWAL", "SEQUENCE_GAP"} and isinstance(
            lifetime, (int, float)
        ):
            self._unexplained_connection_lifetimes_s.append(max(0.0, float(lifetime)))
        self.reconnect_reasons[reason] = self.reconnect_reasons.get(reason, 0) + 1
        self.reconnect_by_worker[worker_id] = self.reconnect_by_worker.get(worker_id, 0) + 1
        backoff_value = event.get("backoff_seconds", 0.0)
        if not isinstance(backoff_value, (int, float)) or float(backoff_value) <= 0:
            self.reconnect_without_backoff_total += 1
        self._reconnect_events.append(dict(event))
        now = time.monotonic()
        recent = self._recent_reconnect_monotonic.setdefault(worker_id, deque(maxlen=128))
        recent.append(now)
        while recent and now - recent[0] > 60.0:
            recent.popleft()
        self.reconnect_burst_max_60s = max(self.reconnect_burst_max_60s, len(recent))

    def ws_transport_opened(self) -> None:
        self.active_connections += 1
        self.active_connections_max = max(self.active_connections_max, self.active_connections)

    def ws_transport_closed(self) -> None:
        self.active_connections = max(0, self.active_connections - 1)

    def gap_resync_started(self) -> None:
        self.sequence_gaps += 1
        self.pending_resyncs += 1

    def gap_resync_succeeded(self) -> None:
        self.pending_resyncs = max(0, self.pending_resyncs - 1)
        self.resyncs += 1

    def gap_resync_failed(self) -> None:
        self.pending_resyncs = max(0, self.pending_resyncs - 1)
        self.resync_failures += 1

    def gap_resync_aborted(self) -> None:
        self.pending_resyncs = max(0, self.pending_resyncs - 1)
        self.resync_aborted_during_shutdown += 1

    def update_queue_backlog(self, value: int) -> None:
        self.queue_backlog_current = max(0, value)
        self.queue_backlog_max = max(self.queue_backlog_max, self.queue_backlog_current)

    def observe_batch_latency(self, latency_ms: float) -> None:
        self.batch_flushes += 1
        self.batch_latency_sum_ms += latency_ms
        self.batch_latency_max_ms = max(self.batch_latency_max_ms, latency_ms)

    def observe_batch_wait(self, latency_ms: float) -> None:
        self.batch_wait_samples += 1
        self.batch_wait_sum_ms += latency_ms
        self.batch_wait_max_ms = max(self.batch_wait_max_ms, latency_ms)
        self._batch_wait_histogram.observe(latency_ms)

    def book_opened(self) -> None:
        self.active_books += 1
        self.active_books_max = max(self.active_books_max, self.active_books)

    def book_closed(self) -> None:
        self.active_books = max(0, self.active_books - 1)

    def worker_started(self) -> None:
        self.active_workers += 1
        self.active_workers_max = max(self.active_workers_max, self.active_workers)

    def worker_stopped(self) -> None:
        self.active_workers = max(0, self.active_workers - 1)

    def as_payload(self) -> dict[str, object]:
        average = self.latency_sum_ms / self.latency_samples if self.latency_samples else None
        batch_average = (
            self.batch_latency_sum_ms / self.batch_flushes if self.batch_flushes else None
        )
        batch_wait_average = (
            self.batch_wait_sum_ms / self.batch_wait_samples if self.batch_wait_samples else None
        )
        classified = (
            self.total_records
            + self.ws_events_dropped_total
            + self.ws_parse_errors_total
            + self.ws_status_events_total
        )
        connection_lifetimes = list(self._connection_lifetimes_s)
        unexplained_lifetimes = list(self._unexplained_connection_lifetimes_s)
        elapsed_hours = max(
            (datetime.now(tz=UTC) - self.started_at).total_seconds() / 3600.0,
            1 / 3600,
        )
        worker_denominator = max(1, self.active_workers_max)
        unexplained = sum(
            count
            for reason, count in self.reconnect_reasons.items()
            if reason not in {"LOCAL_SHUTDOWN", "PROACTIVE_RENEWAL", "SEQUENCE_GAP"}
        )
        gap_accounted = (
            self.resyncs
            + self.resync_failures
            + self.resync_aborted_during_shutdown
            + self.pending_resyncs
        )
        return {
            "started_at": self.started_at.isoformat(),
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "total_records": self.total_records,
            "ws_events_received_total": self.ws_events_received_total,
            "ws_events_accepted_total": self.total_records,
            "ws_events_dropped_total": self.ws_events_dropped_total,
            "ws_parse_errors_total": self.ws_parse_errors_total,
            "ws_invalid_price_total": self.ws_invalid_price_total,
            "ws_invalid_quantity_total": self.ws_invalid_quantity_total,
            "ws_status_events_total": self.ws_status_events_total,
            "ws_sequence_gaps_total": self.sequence_gaps,
            "ws_resync_total": self.resyncs,
            "ws_resync_failures_total": self.resync_failures,
            "ws_resync_aborted_during_shutdown_total": self.resync_aborted_during_shutdown,
            "ws_pending_resyncs": self.pending_resyncs,
            "ws_gap_accounted_total": gap_accounted,
            "ws_gap_accounting_reconciled": self.sequence_gaps == gap_accounted,
            "ws_stale_book_continuations_total": self.stale_book_continuations,
            "ws_reconnects_total": self.ws_reconnects_total,
            "ws_unexplained_reconnects_total": unexplained,
            "reconnect_reasons": dict(sorted(self.reconnect_reasons.items())),
            "reconnect_by_worker": dict(sorted(self.reconnect_by_worker.items())),
            "reconnect_burst_max_60s": self.reconnect_burst_max_60s,
            "reconnect_without_backoff_total": self.reconnect_without_backoff_total,
            "reconnect_per_worker_per_hour": round(
                unexplained / worker_denominator / elapsed_hours, 6
            ),
            "connection_closures_total": self.ws_connection_closures_total,
            "connection_closure_reasons": dict(sorted(self.connection_closure_reasons.items())),
            "connection_lifetime_samples": len(connection_lifetimes),
            "connection_lifetime_p50_s": (
                round(float(median(connection_lifetimes)), 3) if connection_lifetimes else None
            ),
            "connection_lifetime_p95_s": (
                _percentile_values(connection_lifetimes, 0.95) if connection_lifetimes else None
            ),
            "connection_lifetime_min_s": (
                round(min(connection_lifetimes), 3) if connection_lifetimes else None
            ),
            "connection_lifetime_max_s": (
                round(max(connection_lifetimes), 3) if connection_lifetimes else None
            ),
            "unexplained_connection_lifetime_samples": len(unexplained_lifetimes),
            "unexplained_connection_lifetime_p50_s": (
                round(float(median(unexplained_lifetimes)), 3) if unexplained_lifetimes else None
            ),
            "unexplained_connection_lifetime_p95_s": (
                _percentile_values(unexplained_lifetimes, 0.95) if unexplained_lifetimes else None
            ),
            "recent_reconnect_events": list(self._reconnect_events)[-50:],
            "ws_counter_classified_total": classified,
            "ws_counter_reconciled": self.ws_events_received_total == classified,
            "by_record_type": dict(sorted(self.by_record_type.items())),
            "by_stream": dict(sorted(self.by_stream.items())),
            "stream_label_count": len(self.by_stream),
            "maximum_stream_labels": self.maximum_stream_labels,
            "evicted_stream_labels": self.evicted_stream_labels,
            "snapshot_reconciliations": self.snapshot_reconciliations,
            "sequence_gaps": self.sequence_gaps,
            "resyncs": self.resyncs,
            "resync_failures": self.resync_failures,
            "resync_aborted_during_shutdown": self.resync_aborted_during_shutdown,
            "pending_resyncs": self.pending_resyncs,
            "gap_accounting_reconciled": self.sequence_gaps == gap_accounted,
            "batch_write_failures": self.batch_write_failures,
            "latency_average_ms": round(average, 3) if average is not None else None,
            "latency_max_abs_ms": round(self.latency_max_abs_ms, 3),
            "raw_latency_p50_ms": self._raw_latency_histogram.percentile(0.50),
            "raw_latency_p95_ms": self._raw_latency_histogram.percentile(0.95),
            "raw_latency_p99_ms": self._raw_latency_histogram.percentile(0.99),
            "raw_latency_window_max_ms": (
                round(max(self._raw_latency_ms), 3) if self._raw_latency_ms else None
            ),
            "clock_adjusted_latency_p50_ms": self._adjusted_latency_histogram.percentile(0.50),
            "clock_adjusted_latency_p95_ms": self._adjusted_latency_histogram.percentile(0.95),
            "clock_adjusted_latency_p99_ms": self._adjusted_latency_histogram.percentile(0.99),
            "clock_adjusted_latency_window_max_ms": (
                round(max(self._adjusted_latency_ms), 3) if self._adjusted_latency_ms else None
            ),
            "clock_skew_ms": dict(sorted(self.clock_skew_ms.items())),
            "queue_wait_p50_ms": self._queue_wait_histogram.percentile(0.50),
            "queue_wait_p95_ms": self._queue_wait_histogram.percentile(0.95),
            "queue_wait_p99_ms": self._queue_wait_histogram.percentile(0.99),
            "queue_backlog_current": self.queue_backlog_current,
            "queue_backlog_max": self.queue_backlog_max,
            "active_books": self.active_books,
            "active_books_max": self.active_books_max,
            "active_workers": self.active_workers,
            "active_workers_max": self.active_workers_max,
            "active_connections": self.active_connections,
            "active_connections_max": self.active_connections_max,
            "batch_flushes": self.batch_flushes,
            "batch_latency_average_ms": (
                round(batch_average, 3) if batch_average is not None else None
            ),
            "batch_latency_max_ms": round(self.batch_latency_max_ms, 3),
            "batch_wait_average_ms": (
                round(batch_wait_average, 3) if batch_wait_average is not None else None
            ),
            "batch_wait_max_ms": round(self.batch_wait_max_ms, 3),
            "batch_wait_p50_ms": self._batch_wait_histogram.percentile(0.50),
            "batch_wait_p95_ms": self._batch_wait_histogram.percentile(0.95),
            "batch_wait_p99_ms": self._batch_wait_histogram.percentile(0.99),
            # This is time from the first buffered event until the batch flush,
            # not time spent waiting in an asyncio queue.
            "batch_accumulation_average_ms": (
                round(batch_wait_average, 3) if batch_wait_average is not None else None
            ),
            "batch_accumulation_max_ms": round(self.batch_wait_max_ms, 3),
            "batch_accumulation_p50_ms": self._batch_wait_histogram.percentile(0.50),
            "batch_accumulation_p95_ms": self._batch_wait_histogram.percentile(0.95),
            "batch_accumulation_p99_ms": self._batch_wait_histogram.percentile(0.99),
            "metrics_buffer_length": (
                len(self._raw_latency_ms)
                + len(self._adjusted_latency_ms)
                + len(self._queue_wait_ms)
            ),
        }


def _percentile_values(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return round(ordered[index], 3)


_DECISION_RANK = {
    QualityDecision.ACCEPT: 0,
    QualityDecision.ACCEPT_WITH_WARNING: 1,
    QualityDecision.QUARANTINE: 2,
    QualityDecision.REJECT: 3,
}


def _data_status(decision: QualityDecision) -> DataStatus:
    if decision is QualityDecision.REJECT:
        return DataStatus.REJECTED
    if decision is QualityDecision.QUARANTINE:
        return DataStatus.QUARANTINED
    return DataStatus.OK


class CryptoSurveillanceCycle:
    """Validates REST snapshots, derives features and writes to the single source journal."""

    def __init__(
        self,
        provider: CryptoRESTClient,
        store: SqliteStore,
        config: RuntimeConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
        owns_store: bool = False,
        universe_cycle: UniverseCycle | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.config = config
        self.clock = clock
        self.owns_store = owns_store
        self.universe_cycle = universe_cycle
        self.stream_metrics = StreamRuntimeMetrics(
            started_at=self.clock().astimezone(UTC),
            maximum_stream_labels=config.diagnostics.maximum_stream_labels,
            _raw_latency_ms=deque(maxlen=config.diagnostics.maximum_metric_samples),
            _adjusted_latency_ms=deque(maxlen=config.diagnostics.maximum_metric_samples),
            _queue_wait_ms=deque(maxlen=config.diagnostics.maximum_metric_samples),
        )
        # Spot and perpetual consumers share the same SQLite transaction boundary.
        # While the flush runs off the event loop, serialize the whole batch with one
        # async lock so transaction steps cannot interleave.
        self._stream_flush_lock = asyncio.Lock()
        q = config.quality
        self.ohlcv_gate = QualityGate(
            stale_factor=q.stale_factor,
            max_jump_pct=q.max_jump_pct,
            gap_factor=q.gap_factor,
            min_bars=q.min_bars,
            max_clock_skew_s=q.max_clock_skew_s,
            empty_action=QualityDecision(q.empty_action),
            stale_action=QualityDecision(q.stale_action),
            duplicate_action=QualityDecision(q.duplicate_action),
            gap_action=QualityDecision(q.gap_action),
            disorder_action=QualityDecision(q.disorder_action),
            outlier_action=QualityDecision(q.outlier_action),
            future_action=QualityDecision(q.future_action),
            insufficient_history_action=QualityDecision(q.insufficient_history_action),
        )
        self.crypto_gate = CryptoQualityGate(
            max_age_s=q.crypto_max_age_s,
            long_short_ratio_max_age_s=q.long_short_ratio_max_age_s,
            max_clock_skew_s=q.max_clock_skew_s,
            max_spread_bps=q.crypto_max_spread_bps,
            max_spot_perpetual_deviation_bps=q.max_spot_perpetual_deviation_bps,
            missing_required_action=QualityDecision(q.crypto_missing_required_action),
            stale_action=QualityDecision(q.stale_action),
            book_action=QualityDecision(q.crypto_book_action),
            divergence_action=QualityDecision(q.crypto_divergence_action),
        )

    def close(self) -> None:
        if self.owns_store:
            self.store.close()

    def run_once(self) -> list[CryptoCycleResult]:
        """Scan the allowed universe; one symbol's failure does not affect the others."""
        if self.universe_cycle is not None:
            try:
                self.universe_cycle.run_once()
            except Exception as exc:
                log.error(
                    "crypto-universe-cycle-failed",
                    error_type=type(exc).__name__,
                )
        results: list[CryptoCycleResult] = []
        for symbol in self.selected_symbols():
            try:
                results.append(self.refresh_symbol(symbol))
            except Exception as exc:
                results.append(
                    CryptoCycleResult(
                        symbol=symbol,
                        data_status=DataStatus.REJECTED,
                        quality_decision=QualityDecision.REJECT,
                        errors=(f"{type(exc).__name__}: {exc}",),
                    )
                )
        self.store.set_checkpoint("crypto:rest:last_cycle", self.clock().isoformat())
        if any(result.records_saved > 0 for result in results):
            self.store.set_checkpoint("health.exchange_rest", self.clock().isoformat())
        self._maybe_run_retention(self.clock())
        return results

    def selected_symbols(self) -> tuple[str, ...]:
        """Durable dynamic watchlist; the fixed safe bootstrap list until one exists."""
        fallback = tuple(self.config.universe.symbols)
        if not self.config.universe_discovery.enabled:
            return fallback
        raw = self.store.get_checkpoint("dynamic_universe:watchlist")
        if raw is None:
            return fallback
        try:
            payload = json.loads(raw)
            symbols = payload["symbols"]
            if not isinstance(symbols, list):
                return fallback
            return tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if symbol))
        except (KeyError, TypeError, json.JSONDecodeError):
            return fallback

    def refresh_symbol(self, symbol: str) -> CryptoCycleResult:
        now = self.clock().astimezone(UTC)
        errors: list[str] = []
        self.provider.check_clock_skew(MarketType.SPOT)
        self.provider.check_clock_skew(MarketType.PERPETUAL)

        previous_funding = self.store.latest_crypto_record(symbol, "FundingRate")
        previous_oi = self.store.latest_crypto_record(symbol, "OpenInterest")
        spot = self.provider.fetch_spot_ticker(symbol)

        def optional(
            name: str, load: Callable[[], CryptoMarketRecord]
        ) -> CryptoMarketRecord | None:
            try:
                return load()
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                return None

        futures_raw = optional("perpetual", lambda: self.provider.fetch_futures_ticker(symbol))
        funding_raw = optional("funding", lambda: self.provider.fetch_funding_rate(symbol))
        oi_raw = optional("open_interest", lambda: self.provider.fetch_open_interest(symbol))
        ratio_raw = optional(
            "long_short_ratio", lambda: self.provider.fetch_long_short_ratio(symbol)
        )
        book_raw = optional(
            "order_book",
            lambda: self.provider.fetch_order_book(
                symbol,
                market_type=MarketType.SPOT,
                limit=self.config.ingestion.order_book_depth,
            ),
        )
        futures = futures_raw if isinstance(futures_raw, FuturesTicker) else None
        funding = funding_raw if isinstance(funding_raw, FundingRate) else None
        open_interest = oi_raw if isinstance(oi_raw, OpenInterest) else None
        long_short_ratio = ratio_raw if isinstance(ratio_raw, LongShortRatio) else None
        order_book = book_raw if isinstance(book_raw, OrderBookSnapshot) else None
        basis = (
            BasisSnapshot(
                exchange=spot.exchange,
                symbol=spot.symbol,
                event_timestamp=max(spot.event_timestamp, futures.event_timestamp),
                source="argus:stored-spot-perpetual-basis",
                confidence=min(spot.confidence, futures.confidence),
                spot_price=spot.last_price,
                perpetual_price=futures.mark_price or futures.last_price,
            )
            if futures is not None
            else None
        )

        records = [
            record
            for record in (
                spot,
                futures,
                funding,
                open_interest,
                basis,
                long_short_ratio,
                order_book,
            )
            if record is not None
        ]
        records_saved = sum(1 for record in records if self.store.save_crypto_record(record))

        series_by_timeframe: dict[Timeframe, OHLCVSeries] = {}
        series_decisions: list[QualityDecision] = []
        series_saved = 0
        for raw_timeframe in self.config.timeframes:
            timeframe = Timeframe(raw_timeframe)
            try:
                series = self.provider.fetch_ohlcv(
                    symbol,
                    timeframe,
                    market_type=MarketType.SPOT,
                    limit=self.config.ingestion.backfill_limit,
                )
            except Exception as exc:
                errors.append(f"ohlcv:{timeframe.value}: {type(exc).__name__}: {exc}")
                series_decisions.append(QualityDecision.REJECT)
                continue
            # The last row of a Binance kline response is usually the open candle. Only
            # candles that are really closed at cycle time reach the feature/decision path.
            series = series.model_copy(update={"bars": closed_bars(series, as_of=now)})
            report = self.ohlcv_gate.check_ohlcv(series, now=now)
            status = _data_status(report.decision)
            cleaned = report.series.model_copy(update={"data_status": status})
            series_saved += self.store.save_series(cleaned)
            series_decisions.append(report.decision)
            if report.decision.allows_downstream:
                series_by_timeframe[timeframe] = cleaned

        context = CryptoAnalysisContext(
            symbol=symbol,
            as_of=now,
            spot=spot,
            futures=futures,
            series_by_timeframe=series_by_timeframe,
            order_book=order_book,
            funding=funding,
            open_interest=open_interest,
            basis=basis,
            long_short_ratio=long_short_ratio,
            data_status=DataStatus.OK,
        )
        crypto_quality = self.crypto_gate.check(context, now=now)
        decisions = [crypto_quality.decision, *series_decisions]
        final_decision = max(decisions, key=_DECISION_RANK.__getitem__)
        quality_status = _data_status(final_decision)
        self.store.save_crypto_artifact(
            artifact_type="quality",
            symbol=symbol,
            as_of=now,
            data_status=quality_status,
            payload={
                "decision": final_decision.value,
                "crypto_issues": [
                    {"kind": issue.kind, "detail": issue.detail, "action": issue.action.value}
                    for issue in crypto_quality.issues
                ],
                "ohlcv_decisions": [decision.value for decision in series_decisions],
                "errors": errors,
            },
        )
        if not final_decision.allows_downstream:
            return CryptoCycleResult(
                symbol=symbol,
                data_status=quality_status,
                quality_decision=final_decision,
                records_saved=records_saved,
                series_saved=series_saved,
                errors=tuple(errors),
            )

        primary = Timeframe(self.config.timeframes[0])
        primary_series = series_by_timeframe.get(primary)
        if primary_series is None:
            return CryptoCycleResult(
                symbol=symbol,
                data_status=DataStatus.REJECTED,
                quality_decision=QualityDecision.REJECT,
                records_saved=records_saved,
                series_saved=series_saved,
                errors=(*errors, f"primary timeframe missing: {primary.value}"),
            )

        trades = tuple(
            item
            for item in self.store.recent_crypto_records(symbol, "Trade", limit=500)
            if isinstance(item, Trade)
        )
        liquidations = tuple(
            item
            for item in self.store.recent_crypto_records(symbol, "LiquidationEvent", limit=200)
            if isinstance(item, LiquidationEvent)
        )
        book_history = tuple(
            item
            for item in self.store.recent_crypto_records(symbol, "OrderBookSnapshot", limit=5)
            if isinstance(item, OrderBookSnapshot)
        )
        trade_rollups = tuple(
            self.store.recent_trade_rollups(symbol, market_type=MarketType.SPOT.value, limit=500)
        )
        features = compute_crypto_features(
            primary_series,
            ema_fast=self.config.features.ema_fast,
            ema_slow=self.config.features.ema_slow,
            rsi_period=self.config.features.rsi_period,
            atr_period=self.config.features.atr_period,
            realized_vol_window=self.config.features.realized_vol_window,
            volume_zscore_window=self.config.features.volume_zscore_window,
            squeeze_funding_abs=Decimal(str(self.config.regime.squeeze_funding_abs)),
            order_book_depths=tuple(self.config.features.order_book_depths),
            higher_timeframes={
                timeframe: series
                for timeframe, series in series_by_timeframe.items()
                if timeframe is not primary
            },
            btc_series=(
                self.store.load_series("BTCUSDT", primary, limit=500)
                if symbol.upper() != "BTCUSDT"
                else primary_series
            ),
            funding=funding,
            previous_funding=(
                previous_funding if isinstance(previous_funding, FundingRate) else None
            ),
            open_interest=open_interest,
            previous_open_interest=(previous_oi if isinstance(previous_oi, OpenInterest) else None),
            basis=basis,
            long_short_ratio=long_short_ratio,
            order_book=order_book,
            book_history=book_history,
            trades=trades,
            trade_rollups=trade_rollups,
            liquidations=liquidations,
        )
        # The technical series' last bar time is an input of the feature computation; the
        # artifact's PIT identity is the cycle time at which the derivative/order-flow
        # evidence was actually observed. Otherwise later refreshes within the same hourly
        # candle would hit the UNIQUE key and silently keep the first feature/regime/trigger.
        features = features.model_copy(update={"as_of": now})
        regime = classify_market_regime(
            features,
            high_volatility_natr_pct=self.config.regime.high_volatility_natr_pct,
            low_liquidity_spread_bps=self.config.regime.low_liquidity_spread_bps,
        )
        enriched = context.model_copy(update={"features": features, "regime": regime})
        specialist_opinions = DeterministicCryptoExperts().assess(enriched)
        opinions = (
            *specialist_opinions,
            synthesize_manager(specialist_opinions, horizon=primary),
        )
        triggers = CryptoTriggerEngine().evaluate(features, regime, crypto_quality)
        for artifact_type, payload, status in (
            ("features", features.model_dump(mode="json"), features.data_status),
            ("regime", regime.model_dump(mode="json"), regime.data_status),
            (
                "experts",
                [opinion.model_dump(mode="json") for opinion in opinions],
                (
                    DataStatus.OK
                    if all(opinion.data_status is DataStatus.OK for opinion in opinions)
                    else DataStatus.PENDING
                ),
            ),
            (
                "triggers",
                [trigger.model_dump(mode="json") for trigger in triggers],
                DataStatus.OK,
            ),
        ):
            self.store.save_crypto_artifact(
                artifact_type=artifact_type,
                symbol=symbol,
                as_of=features.as_of,
                data_status=status,
                payload=payload,
            )

        try:
            research, _plan = IntradayResearchCoordinator(self.store, self.config).process(
                symbol=symbol,
                as_of=now,
                spot=spot,
                features=features,
                regime=regime,
                series_by_timeframe=series_by_timeframe,
            )
            if research is None:
                self.store.save_crypto_artifact(
                    artifact_type="news_coverage_pending",
                    symbol=symbol,
                    as_of=now,
                    data_status=DataStatus.PENDING,
                    payload={
                        "reason": "symbol not yet evaluated in the latest news coverage checkpoint"
                    },
                )
        except Exception as exc:
            # The side job never fails the market/quality path; the safe error class is PIT-visible.
            self.store.save_crypto_artifact(
                artifact_type="intraday_research_error",
                symbol=symbol,
                as_of=now,
                data_status=DataStatus.REJECTED,
                payload={"error_type": type(exc).__name__},
            )

        candidate_id: str | None = None
        bullish = [trigger for trigger in triggers if trigger.direction is Stance.BULLISH]
        if bullish and qualifies_trade_candidate(triggers):
            stop_value = features.values.get("support")
            candidate = TradeCandidate(
                symbol=symbol,
                side=OrderSide.BUY,
                horizon=primary,
                entry_reference=spot.last_price,
                stop_price=stop_value if isinstance(stop_value, Decimal) else None,
                invalidation="; ".join(sorted({trigger.invalidation for trigger in bullish})),
                evidence_source_ids=tuple(
                    sorted({source for trigger in bullish for source in trigger.evidence_sources})
                ),
                confidence_pct=min(90, max(55, regime.confidence_pct)),
            )
            self.store.save_trade_candidate(candidate)
            guard = GuardRiskEngine(self.config.risk).assess(
                candidate,
                self._guard_state(spot, features, crypto_quality, now),
            )
            self.store.save_risk_assessment(guard)
            candidate_id = candidate.candidate_id

        return CryptoCycleResult(
            symbol=symbol,
            data_status=DataStatus.OK,
            quality_decision=final_decision,
            records_saved=records_saved,
            series_saved=series_saved,
            candidate_id=candidate_id,
            errors=tuple(errors),
        )

    def _guard_state(
        self,
        spot: SpotTicker,
        features: object,
        quality: object,
        now: datetime,
    ) -> GuardState:
        """State that forces GUARD to a safe REJECT when portfolio/risk input is missing."""
        from argus.core.crypto import FeatureSnapshot
        from argus.platform.quality import CryptoQualityReport

        assert isinstance(features, FeatureSnapshot)
        assert isinstance(quality, CryptoQualityReport)
        spread = features.values.get("spread_bps")
        natr = features.values.get("normalized_atr_pct")
        return GuardState(
            portfolio_value=Decimal("0"),
            proposed_risk_amount=None,
            concurrent_positions=len(self.store.shadow_positions(open_only=True)),
            quote_volume_24h=spot.quote_volume_24h,
            spread_bps=float(spread) if isinstance(spread, (float, int, Decimal)) else None,
            estimated_slippage_bps=None,
            data_age_s=max(0.0, (now - spot.event_timestamp).total_seconds()),
            exchange_rest_healthy=True,
            exchange_websocket_healthy=self._websocket_is_recent(now),
            normalized_atr_pct=float(natr) if isinstance(natr, (float, int, Decimal)) else None,
            quality_decision=quality.decision,
            human_approved=False,
        )

    def _websocket_is_recent(self, now: datetime) -> bool:
        raw = self.store.get_checkpoint("crypto:websocket:last_event")
        if raw is None:
            return False
        try:
            observed = datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            return False
        return (now - observed).total_seconds() <= self.config.ingestion.disconnect_after_s * 2

    async def consume_websocket(
        self,
        provider: CryptoWebSocketClient,
        market_type: MarketType,
        *,
        symbols: tuple[str, ...] | None = None,
        max_events: int | None = None,
    ) -> int:
        """Write WS records idempotently; on a depth gap, resync from a REST snapshot."""
        reconcilers: dict[str, OrderBookReconciler] = {}
        last_materialized: dict[str, datetime] = {}
        pending_records: list[CryptoMarketRecord] = []
        pending_trade_rollups: list[Trade] = []
        pending_book_rollups: list[OrderBookSnapshot] = []
        pending_batch_started: float | None = None
        consumed = 0
        selected_symbols = symbols or tuple(self.config.universe.symbols)
        provider_stream = provider.stream(selected_symbols, market_type)
        try:
            async for record in provider_stream:
                if pending_batch_started is None:
                    pending_batch_started = time.monotonic()
                consumed += 1
                self.stream_metrics.observe(record)
                if isinstance(record, Trade):
                    pending_trade_rollups.append(record)
                    if self.config.ingestion.persist_raw_trades:
                        pending_records.append(record)
                elif isinstance(record, OrderBookDelta):
                    if self.config.ingestion.persist_raw_depth_deltas:
                        pending_records.append(record)
                else:
                    pending_records.append(record)
                if isinstance(record, OrderBookDelta):
                    reconciler = reconcilers.get(record.symbol)
                    if reconciler is None:
                        try:
                            snapshot = await asyncio.to_thread(
                                self.provider.fetch_order_book,
                                record.symbol,
                                market_type=market_type,
                                limit=self.config.ingestion.order_book_depth,
                            )
                            reconciler = OrderBookReconciler(snapshot)
                            reconcilers[record.symbol] = reconciler
                            self.stream_metrics.book_opened()
                            self.stream_metrics.snapshot_reconciliations += 1
                            pending_records.append(snapshot)
                            pending_book_rollups.append(snapshot)
                            last_materialized[record.symbol] = record.ingestion_timestamp
                        except Exception as exc:
                            log.warning(
                                "crypto-order-book-resync-failed",
                                symbol=record.symbol,
                                market_type=market_type.value,
                                error_type=type(exc).__name__,
                            )
                            if max_events is not None and consumed >= max_events:
                                break
                            continue
                    try:
                        materialized = reconciler.apply(record)
                        previous = last_materialized.get(record.symbol)
                        interval = self.config.ingestion.order_book_materialize_interval_s
                        if (
                            previous is None
                            or (record.ingestion_timestamp - previous).total_seconds() >= interval
                        ):
                            pending_records.append(materialized)
                            pending_book_rollups.append(materialized)
                            last_materialized[record.symbol] = record.ingestion_timestamp
                    except (SequenceGapError, DataQualityError) as exc:
                        sequence_gap = isinstance(exc, SequenceGapError)
                        if sequence_gap:
                            self.stream_metrics.gap_resync_started()
                        if reconcilers.pop(record.symbol, None) is not None:
                            self.stream_metrics.book_closed()
                        last_materialized.pop(record.symbol, None)
                        log.warning(
                            "crypto-order-book-resync",
                            symbol=record.symbol,
                            market_type=market_type.value,
                            reason=type(exc).__name__,
                        )
                        try:
                            snapshot = await asyncio.to_thread(
                                self.provider.fetch_order_book,
                                record.symbol,
                                market_type=market_type,
                                limit=self.config.ingestion.order_book_depth,
                            )
                            reconcilers[record.symbol] = OrderBookReconciler(snapshot)
                            self.stream_metrics.book_opened()
                            if sequence_gap:
                                self.stream_metrics.gap_resync_succeeded()
                            pending_records.append(snapshot)
                            pending_book_rollups.append(snapshot)
                            last_materialized[record.symbol] = record.ingestion_timestamp
                        except asyncio.CancelledError:
                            if sequence_gap:
                                self.stream_metrics.gap_resync_aborted()
                            raise
                        except Exception as resync_exc:
                            if sequence_gap:
                                self.stream_metrics.gap_resync_failed()
                            log.warning(
                                "crypto-order-book-resync-failed",
                                symbol=record.symbol,
                                market_type=market_type.value,
                                error_type=type(resync_exc).__name__,
                            )
                            if max_events is not None and consumed >= max_events:
                                break
                            continue
                buffered = (
                    len(pending_records) + len(pending_trade_rollups) + len(pending_book_rollups)
                )
                self.stream_metrics.update_queue_backlog(buffered)
                if buffered >= self.config.ingestion.stream_commit_batch_size:
                    if pending_batch_started is not None:
                        self.stream_metrics.observe_batch_wait(
                            (time.monotonic() - pending_batch_started) * 1000
                        )
                    await self._flush_stream_batch_async(
                        pending_records,
                        pending_trade_rollups,
                        pending_book_rollups,
                        market_type,
                    )
                    pending_batch_started = None
                if max_events is not None and consumed >= max_events:
                    break
        finally:
            try:
                aclose = getattr(provider_stream, "aclose", None)
                if aclose is not None:
                    await aclose()
            finally:
                try:
                    if pending_records or pending_trade_rollups or pending_book_rollups:
                        if pending_batch_started is not None:
                            self.stream_metrics.observe_batch_wait(
                                (time.monotonic() - pending_batch_started) * 1000
                            )
                        await self._flush_stream_batch_async(
                            pending_records,
                            pending_trade_rollups,
                            pending_book_rollups,
                            market_type,
                        )
                finally:
                    for _symbol in tuple(reconcilers):
                        self.stream_metrics.book_closed()
                    reconcilers.clear()
        return consumed

    async def _flush_stream_batch_async(
        self,
        records: list[CryptoMarketRecord],
        trade_rollups: list[Trade],
        book_rollups: list[OrderBookSnapshot],
        market_type: MarketType,
    ) -> None:
        async with self._stream_flush_lock:
            await asyncio.to_thread(
                self._flush_stream_batch,
                records,
                trade_rollups,
                book_rollups,
                market_type,
            )

    def _flush_stream_batch(
        self,
        records: list[CryptoMarketRecord],
        trade_rollups: list[Trade],
        book_rollups: list[OrderBookSnapshot],
        market_type: MarketType,
    ) -> None:
        """Write the in-memory batch in one short transaction; hold no lock between events."""
        started = time.monotonic()
        duplicate_count = 0
        try:
            for record in records:
                if not self.store.save_crypto_record(record, commit=False):
                    duplicate_count += 1
            self.store.save_stream_rollups(trade_rollups, book_rollups, commit=False)
            self.stream_metrics.update_queue_backlog(0)
            self.stream_metrics.observe_batch_latency((time.monotonic() - started) * 1000)
            observed = self.clock().isoformat()
            self.store.set_checkpoint("crypto:websocket:last_event", observed, commit=False)
            self.store.set_checkpoint("health.exchange_websocket", observed, commit=False)
            self.store.set_checkpoint("health.exchange_websocket.state", "running", commit=False)
            self.store.set_checkpoint(
                "crypto:websocket:metrics",
                json.dumps(self.stream_metrics.as_payload(), separators=(",", ":")),
                commit=False,
            )
            self.store.commit()
            if duplicate_count:
                log.debug(
                    "crypto-websocket-duplicates",
                    market_type=market_type.value,
                    count=duplicate_count,
                )
            self._maybe_run_retention(self.clock())
        except Exception as exc:
            self.store.rollback()
            self.stream_metrics.batch_write_failures += 1
            log.error(
                "crypto-websocket-batch-write-failed",
                market_type=market_type.value,
                record_count=len(records) + len(trade_rollups) + len(book_rollups),
                error_type=type(exc).__name__,
            )
        finally:
            records.clear()
            trade_rollups.clear()
            book_rollups.clear()
            self.stream_metrics.update_queue_backlog(0)

    def _maybe_run_retention(self, now: datetime) -> None:
        policy = self.config.retention
        if not policy.enabled:
            return
        checkpoint_key = "retention:last_attempt"
        previous = self.store.get_checkpoint(checkpoint_key)
        if previous is not None:
            try:
                elapsed = (now - datetime.fromisoformat(previous).astimezone(UTC)).total_seconds()
            except ValueError:
                elapsed = policy.maintenance_interval_s
            if elapsed < policy.maintenance_interval_s:
                return
        # After a failed maintenance run, do not retry on every WS batch and stall the system.
        self.store.set_checkpoint(checkpoint_key, now.isoformat())
        report = self.store.run_crypto_retention(policy, now=now)
        self.store.set_checkpoint(
            "retention:last_success" if report.success else "retention:last_failure",
            report.finished_at.isoformat(),
        )
        if not report.success:
            log.error(
                "crypto-retention-failed",
                error_type="RetentionError",
                reason=report.reason,
            )


class CryptoWebSocketService:
    """Carries spot and perpetual WebSocket streams to the durable journal in separate tasks."""

    def __init__(self, cycle: CryptoSurveillanceCycle, provider: CryptoWebSocketClient) -> None:
        self.cycle = cycle
        self.provider = provider

    @staticmethod
    async def _cancel_tasks(tasks: set[asyncio.Task[None]]) -> None:
        """Cancel, await and dereference a complete worker generation."""

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()

    async def run(self) -> None:
        """Run one combined-stream worker per market.

        Parser and order-book state stay isolated per symbol, while TLS, compression and
        heartbeat resources do not grow linearly with the number of symbols.
        """
        market_types = tuple(MarketType(value) for value in self.cycle.config.market_types)
        self.cycle.store.set_checkpoint("health.exchange_websocket.state", "starting")
        tasks: set[asyncio.Task[None]] = set()
        try:
            symbols: tuple[str, ...] | None = None
            while True:
                selected = self.cycle.selected_symbols()
                if selected != symbols:
                    await self._cancel_tasks(tasks)
                    tasks = {
                        asyncio.create_task(
                            self._run_worker(market, selected),
                            name=f"crypto-ws-{market.value}-combined",
                        )
                        for market in market_types
                    }
                    symbols = selected
                    self.cycle.store.set_checkpoint(
                        "crypto:websocket:subscribed_symbols", json.dumps(selected)
                    )
                if tasks:
                    done, _pending = await asyncio.wait(
                        tasks,
                        timeout=30.0,
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in done:
                        task.result()
                    if done == tasks:
                        return
                else:
                    await asyncio.sleep(30.0)
        finally:
            await self._cancel_tasks(tasks)
            self.cycle.store.set_checkpoint("health.exchange_websocket.state", "stopped")

    async def _run_worker(self, market_type: MarketType, symbols: tuple[str, ...]) -> None:
        failures = 0
        ingestion = self.cycle.config.ingestion
        self.cycle.stream_metrics.worker_started()
        try:
            while True:
                try:
                    await self.cycle.consume_websocket(
                        self.provider,
                        market_type,
                        symbols=symbols,
                    )
                    raise RuntimeError("WebSocket stream ended unexpectedly")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delay = min(
                        ingestion.reconnect_base_s * (2 ** min(failures, 16)),
                        ingestion.reconnect_max_s,
                    )
                    failures += 1
                    log.error(
                        "crypto-websocket-worker-restart",
                        symbols=symbols,
                        market_type=market_type.value,
                        restart_count=failures,
                        delay_s=delay,
                        error_type=type(exc).__name__,
                    )
                    await asyncio.sleep(delay)
        finally:
            self.cycle.stream_metrics.worker_stopped()

    def close(self) -> None:
        try:
            # While ``asyncio.gather`` is cancelled, the child workers' ``finally`` batch flush
            # may finish after the state write inside ``run``. A close called after the event
            # loop shut down has the final say, so health never shows a stopped stream as OK.
            self.cycle.store.set_checkpoint("health.exchange_websocket.state", "stopped")
        finally:
            self.cycle.close()
