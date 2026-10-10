"""Offline million-event bounded-state stress probe for Argus stream metrics.

No network, database, mission, shadow or execution code is used.  This complements the
real ``consume_websocket`` queue-drain tests by proving event-count-independent diagnostic
buffers at much higher event counts.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
import tracemalloc
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from argus.core.crypto import Trade
from argus.core.enums import MarketType, OrderSide
from argus.interface.crypto_runtime import StreamRuntimeMetrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=2_000_000)
    parser.add_argument("--buffer-size", type=int, default=4096)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.events, args.buffer_size, args.batch_size) <= 0:
        parser.error("events/buffer-size/batch-size must be positive")

    now = datetime.now(tz=UTC)
    record = Trade(
        exchange="binance",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        event_timestamp=now,
        ingestion_timestamp=now,
        source="offline:synthetic",
        trade_id="bounded-state",
        side=OrderSide.BUY,
        price=Decimal("1"),
        quantity=Decimal("1"),
    )
    metrics = StreamRuntimeMetrics(started_at=now, maximum_stream_labels=128)
    for series in (
        metrics._raw_latency_ms,
        metrics._adjusted_latency_ms,
        metrics._queue_wait_ms,
    ):
        series.__init__(maxlen=args.buffer_size)

    tracemalloc.start(5)
    before_current, _before_peak = tracemalloc.get_traced_memory()
    started = time.monotonic()
    batch: list[Trade] = []
    maximum_queue = 0
    for _ in range(args.events):
        metrics.ws_received()
        metrics.observe(record)
        batch.append(record)
        metrics.update_queue_backlog(len(batch))
        maximum_queue = max(maximum_queue, len(batch))
        if len(batch) >= args.batch_size:
            batch.clear()
            metrics.update_queue_backlog(0)
    batch.clear()
    metrics.update_queue_backlog(0)
    elapsed = time.monotonic() - started
    after_current, peak = tracemalloc.get_traced_memory()
    gc.collect()  # diagnostic comparison only; not a production mitigation
    after_gc, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    payload = metrics.as_payload()
    result = {
        "events": args.events,
        "duration_seconds": round(elapsed, 3),
        "events_per_second": round(args.events / elapsed, 3),
        "received": payload["ws_events_received_total"],
        "accepted": payload["ws_events_accepted_total"],
        "counter_reconciled": payload["ws_counter_reconciled"],
        "maximum_queue": maximum_queue,
        "final_queue": payload["queue_backlog_current"],
        "metrics_buffer_length": payload["metrics_buffer_length"],
        "metrics_buffer_limit": args.buffer_size * 3,
        "stream_label_count": payload["stream_label_count"],
        "stream_label_limit": metrics.maximum_stream_labels,
        "python_heap_before_bytes": before_current,
        "python_heap_after_bytes": after_current,
        "python_heap_after_gc_bytes": after_gc,
        "python_heap_peak_bytes": peak,
    }
    result["passed"] = bool(
        result["counter_reconciled"]
        and result["final_queue"] == 0
        and result["metrics_buffer_length"] <= result["metrics_buffer_limit"]
        and result["stream_label_count"] <= result["stream_label_limit"]
    )
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=False)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
