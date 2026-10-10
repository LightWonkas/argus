"""Binance REST/WS ingestion tests — fully injected, offline."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from argus.core.crypto import LiquidationEvent, OrderBookDelta, Trade
from argus.core.enums import MarketType, OrderSide, Timeframe
from argus.core.errors import (
    AdapterError,
    ClockSkewError,
    ConfigError,
    DataQualityError,
    SequenceGapError,
    UnsupportedEndpointError,
)
from argus.ingestion.binance import (
    BinanceRESTProvider,
    BinanceWebSocketParser,
    BinanceWebSocketProvider,
    OrderBookReconciler,
    RateLimiter,
    ReconnectPolicy,
    WebSocketSupervisor,
    _reconnect_reason,
    ensure_websocket_runtime,
)
from argus.interface.crypto_runtime import StreamRuntimeMetrics

MS = 1_788_278_400_000  # 2026-09-01T00:00:00Z
NOW = datetime.fromtimestamp(MS / 1000, tz=UTC)


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str | int]]] = []

    def get_json(self, base_url: str, path: str, params: dict[str, str | int]) -> object:
        self.calls.append((base_url, path, params))
        return self.responses[(base_url, path)]


def _provider(responses: dict[tuple[str, str], object]) -> BinanceRESTProvider:
    return BinanceRESTProvider(
        FakeTransport(responses),
        limiter=RateLimiter(1000, clock=lambda: 1.0, sleep=lambda _seconds: None),
        wall_clock=lambda: MS / 1000,
    )


def test_rest_spot_futures_and_basis_transformations() -> None:
    transport = FakeTransport(
        {
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/ticker/24hr"): {
                "lastPrice": "60000.1",
                "bidPrice": "60000",
                "askPrice": "60000.2",
                "volume": "100",
                "quoteVolume": "6000000",
                "closeTime": MS,
            },
            (BinanceRESTProvider.FUTURES_BASE, "/fapi/v1/ticker/24hr"): {
                "lastPrice": "60010",
                "bidPrice": "60009",
                "askPrice": "60011",
                "closeTime": MS,
            },
            (BinanceRESTProvider.FUTURES_BASE, "/fapi/v1/premiumIndex"): {
                "markPrice": "60008",
                "indexPrice": "60000",
                "lastFundingRate": "0.0001",
                "nextFundingTime": MS + 28_800_000,
                "time": MS,
            },
        }
    )
    provider = BinanceRESTProvider(
        transport,
        limiter=RateLimiter(1000, clock=lambda: 1.0, sleep=lambda _seconds: None),
    )

    spot = provider.fetch_spot_ticker("BTC/USDT")
    futures = provider.fetch_futures_ticker("BTCUSDT")
    basis = provider.fetch_basis("BTCUSDT")

    assert spot.symbol == futures.symbol == basis.symbol == "BTCUSDT"
    assert spot.market_type is MarketType.SPOT
    assert futures.market_type is MarketType.PERPETUAL
    assert futures.funding_rate is not None
    assert basis.basis == Decimal("7.9")
    assert all("key" not in str(call).lower() for call in transport.calls)


def test_rest_ohlcv_book_and_derivative_records() -> None:
    provider = _provider(
        {
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/klines"): [
                [MS, "10", "12", "9", "11", "50", MS + 3_599_999]
            ],
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/depth"): {
                "lastUpdateId": 10,
                "bids": [["10", "2"]],
                "asks": [["11", "3"]],
            },
            (BinanceRESTProvider.FUTURES_BASE, "/fapi/v1/premiumIndex"): {
                "lastFundingRate": "-0.0002",
                "nextFundingTime": MS + 1,
                "time": MS,
            },
            (BinanceRESTProvider.FUTURES_BASE, "/fapi/v1/openInterest"): {
                "openInterest": "123.4",
                "time": MS,
            },
            (
                BinanceRESTProvider.FUTURES_BASE,
                "/futures/data/globalLongShortAccountRatio",
            ): [
                {
                    "longAccount": "0.55",
                    "shortAccount": "0.45",
                    "longShortRatio": "1.222",
                    "timestamp": MS,
                }
            ],
        }
    )

    series = provider.fetch_ohlcv("ETHUSDT", Timeframe.H1)
    book = provider.fetch_order_book("ETHUSDT")
    funding = provider.fetch_funding_rate("ETHUSDT")
    oi = provider.fetch_open_interest("ETHUSDT")
    ratio = provider.fetch_long_short_ratio("ETHUSDT")

    assert series.exchange == "binance" and series.market_type is MarketType.SPOT
    assert series.bars[0].close == 11
    assert book.sequence == 10 and not book.crossed
    assert funding.rate < 0 and oi.contracts == Decimal("123.4")
    assert ratio.account_ratio == Decimal("1.222")
    with pytest.raises(UnsupportedEndpointError, match="WebSocket"):
        provider.fetch_liquidations("ETHUSDT")


def test_public_spot_universe_uses_exchange_ticker_book_and_explicit_filters() -> None:
    provider = _provider(
        {
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/exchangeInfo"): {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "isSpotTradingAllowed": True,
                        "filters": [
                            {
                                "filterType": "PRICE_FILTER",
                                "minPrice": "0.01",
                                "maxPrice": "1000000",
                                "tickSize": "0.01",
                            },
                            {
                                "filterType": "LOT_SIZE",
                                "minQty": "0.00001",
                                "maxQty": "100",
                                "stepSize": "0.00001",
                            },
                            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                        ],
                    },
                    {
                        # The real exchangeInfo lists every quote asset. A non-USDT
                        # symbol must not make the normaliser fail the wide scan.
                        "symbol": "VENBNB",
                        "baseAsset": "VEN",
                        "quoteAsset": "BNB",
                        "status": "BREAK",
                        "isSpotTradingAllowed": False,
                        "filters": [],
                    },
                ]
            },
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/ticker/24hr"): [
                {"symbol": "BTCUSDT", "quoteVolume": "100000000", "count": 12345}
            ],
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/ticker/bookTicker"): [
                {"symbol": "BTCUSDT", "bidPrice": "59999", "askPrice": "60001"}
            ],
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/klines"): [[MS, "1", "1", "1", "1", "1"]],
        }
    )

    observations = provider.fetch_spot_universe_observations()
    assert len(observations) == 1
    observation = observations[0]
    assert observation.symbol == "BTCUSDT"
    assert observation.quote_volume_24h == Decimal("100000000")
    assert observation.trade_count_24h == 12345
    assert observation.spread_bps is not None
    assert observation.filters is not None
    assert observation.filters.complete_for_spot_order
    assert observation.data_status.value == "pending"
    assert provider.fetch_first_spot_kline_time("BTC/USDT") == NOW


def test_clock_skew_blocks_excessive_exchange_time_difference() -> None:
    ok = _provider({(BinanceRESTProvider.SPOT_BASE, "/api/v3/time"): {"serverTime": MS + 500}})
    assert ok.check_clock_skew() == 500

    bad = BinanceRESTProvider(
        FakeTransport(
            {(BinanceRESTProvider.SPOT_BASE, "/api/v3/time"): {"serverTime": MS + 60_000}}
        ),
        wall_clock=lambda: MS / 1000,
        max_clock_skew_ms=1_000,
        limiter=RateLimiter(1000, clock=lambda: 1.0, sleep=lambda _seconds: None),
    )
    with pytest.raises(ClockSkewError):
        bad.check_clock_skew()


def test_rate_limiter_waits_for_remaining_interval() -> None:
    state = {"now": 10.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        state["now"] += seconds

    limiter = RateLimiter(2, clock=lambda: state["now"], sleep=sleep)
    limiter.acquire()
    state["now"] += 0.1
    limiter.acquire()
    assert waits == pytest.approx([0.4])


def test_rest_provider_retries_transient_transport_error() -> None:
    calls = 0
    waits: list[float] = []

    class FlakyTransport:
        def get_json(self, base_url: str, path: str, params: dict[str, str | int]) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary")
            return {"serverTime": MS}

    provider = BinanceRESTProvider(
        FlakyTransport(),
        wall_clock=lambda: MS / 1000,
        limiter=RateLimiter(1000, clock=lambda: 1.0, sleep=lambda _seconds: None),
        retry_policy=ReconnectPolicy(base_s=1, max_s=2, jitter_ratio=0),
        sleep=waits.append,
    )
    assert provider.check_clock_skew() == 0
    assert calls == 2 and waits == [1]


def test_rest_provider_bounds_rate_limit_retries() -> None:
    calls = 0
    waits: list[float] = []

    class RateLimitedTransport:
        def get_json(self, base_url: str, path: str, params: dict[str, str | int]) -> object:
            nonlocal calls
            calls += 1
            raise AdapterError("HTTP 429")

    provider = BinanceRESTProvider(
        RateLimitedTransport(),
        request_attempts=3,
        wall_clock=lambda: MS / 1000,
        limiter=RateLimiter(1000, clock=lambda: 1.0, sleep=lambda _seconds: None),
        retry_policy=ReconnectPolicy(base_s=1, max_s=2, jitter_ratio=0),
        sleep=waits.append,
    )

    with pytest.raises(AdapterError, match="retry limit exceeded"):
        provider.check_clock_skew()
    assert calls == 3
    assert waits == [1, 2]


TRADE_EVENT: dict[str, Any] = {
    "e": "trade",
    "E": MS,
    "s": "BTCUSDT",
    "t": 7,
    "p": "60000",
    "q": "0.1",
    "m": False,
}


def test_websocket_parser_maps_trade_depth_liquidation_and_ticker() -> None:
    parser = BinanceWebSocketParser(MarketType.PERPETUAL)
    trade = parser.parse({"stream": "x", "data": TRADE_EVENT})
    depth = parser.parse(
        {
            "e": "depthUpdate",
            "E": MS,
            "s": "BTCUSDT",
            "U": 11,
            "u": 12,
            "pu": 10,
            "b": [["60000", "2"]],
            "a": [["60001", "3"]],
        }
    )
    liquidation = parser.parse(
        {
            "e": "forceOrder",
            "E": MS,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "T": MS,
                "p": "59900",
                "ap": "59890",
                "q": "0.5",
                "z": "0.4",
            },
        }
    )
    ticker = parser.parse(
        {
            "e": "24hrTicker",
            "E": MS,
            "s": "BTCUSDT",
            "c": "60000",
            "b": "59999",
            "a": "60001",
        }
    )

    assert isinstance(trade, Trade) and trade.side is OrderSide.BUY
    assert isinstance(depth, OrderBookDelta) and depth.previous_final_sequence == 10
    assert isinstance(liquidation, LiquidationEvent)
    assert liquidation.quantity == Decimal("0.4")
    assert ticker.market_type is MarketType.PERPETUAL
    with pytest.raises(UnsupportedEndpointError):
        parser.parse({"e": "unknown"})


def _snapshot() -> Any:
    return _provider(
        {
            (BinanceRESTProvider.SPOT_BASE, "/api/v3/depth"): {
                "lastUpdateId": 10,
                "bids": [["99", "2"], ["98", "1"]],
                "asks": [["101", "2"], ["102", "1"]],
            }
        }
    ).fetch_order_book("BTCUSDT")


def _delta(*, first: int, final: int, bid: str = "100", ask: str = "101") -> OrderBookDelta:
    parsed = BinanceWebSocketParser(MarketType.SPOT).parse(
        {
            "e": "depthUpdate",
            "E": MS,
            "s": "BTCUSDT",
            "U": first,
            "u": final,
            "b": [[bid, "3"]],
            "a": [[ask, "2"]],
        }
    )
    assert isinstance(parsed, OrderBookDelta)
    return parsed


def test_order_book_reconciliation_detects_gap_cross_and_duplicate() -> None:
    reconciler = OrderBookReconciler(_snapshot())
    updated = reconciler.apply(_delta(first=11, final=11))
    assert updated.sequence == 11 and updated.best_bid == 100
    assert reconciler.apply(_delta(first=10, final=11)) is updated
    with pytest.raises(SequenceGapError):
        reconciler.apply(_delta(first=13, final=13))

    crossed = OrderBookReconciler(_snapshot())
    with pytest.raises(DataQualityError, match="crossed"):
        crossed.apply(_delta(first=11, final=11, bid="102", ask="101"))


def test_order_book_materialized_depth_stays_bounded() -> None:
    reconciler = OrderBookReconciler(_snapshot())
    updated = reconciler.apply(_delta(first=11, final=11, bid="100", ask="103"))

    assert len(updated.bids) == 2
    assert len(updated.asks) == 2
    assert [level.price for level in updated.bids] == [Decimal("100"), Decimal("99")]
    assert [level.price for level in updated.asks] == [Decimal("101"), Decimal("102")]


def test_futures_book_bootstrap_bridges_snapshot_before_enforcing_previous_sequence() -> None:
    snapshot = _snapshot().model_copy(update={"market_type": MarketType.PERPETUAL})
    parser = BinanceWebSocketParser(MarketType.PERPETUAL)

    def delta(first: int, final: int, previous: int) -> OrderBookDelta:
        value = parser.parse(
            {
                "e": "depthUpdate",
                "E": MS,
                "s": "BTCUSDT",
                "U": first,
                "u": final,
                "pu": previous,
                "b": [["100", "3"]],
                "a": [["101", "2"]],
            }
        )
        assert isinstance(value, OrderBookDelta)
        return value

    reconciler = OrderBookReconciler(snapshot)
    # Snapshot 10 was taken mid-stream; pu=9 on the first covering delta is expected.
    assert reconciler.apply(delta(10, 12, 9)).sequence == 12
    # USD-M update ids need not be consecutive; pu links to the previous u.
    assert reconciler.apply(delta(20, 22, 12)).sequence == 22
    with pytest.raises(SequenceGapError):
        reconciler.apply(delta(23, 23, 12))


def test_reconnect_policy_is_bounded_and_deterministic_with_injected_rng() -> None:
    policy = ReconnectPolicy(base_s=2, max_s=10, jitter_ratio=0.25)
    assert policy.delay(0, rng=lambda: 0.5) == 2
    assert policy.delay(5, rng=lambda: 0.5) == 10
    assert policy.delay(0, rng=lambda: 0.0) == 1.5


def test_missing_websocket_extra_fails_before_reconnect_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("argus.ingestion.binance.importlib.util.find_spec", lambda _name: None)
    with pytest.raises(ConfigError, match=r"argus\[ingestion\]"):
        ensure_websocket_runtime()


@pytest.mark.asyncio
async def test_websocket_supervisor_reconnects_without_real_network() -> None:
    calls = 0
    waits: list[float] = []

    async def connector() -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            if False:  # pragma: no cover
                yield {}
            raise RuntimeError("temporary disconnect")
        yield TRADE_EVENT

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        policy=ReconnectPolicy(base_s=1, max_s=2, jitter_ratio=0),
        sleep=sleep,
    ).stream(max_reconnects=1)
    record = await anext(stream)
    await stream.aclose()

    assert isinstance(record, Trade)
    assert calls == 2 and waits == [1]


@pytest.mark.asyncio
async def test_websocket_supervisor_drops_zero_trade_without_reconnect() -> None:
    calls = 0
    zero_trade = {**TRADE_EVENT, "p": "0", "q": "0", "X": "NA", "st": 1}

    async def connector() -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        yield zero_trade
        yield TRADE_EVENT

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.PERPETUAL),
        sleep=lambda _seconds: None,  # type: ignore[arg-type]
    ).stream(max_reconnects=0)
    record = await anext(stream)
    await stream.aclose()

    assert isinstance(record, Trade)
    assert calls == 1


@pytest.mark.asyncio
async def test_websocket_supervisor_drops_unknown_event_without_reconnect() -> None:
    calls = 0

    async def connector() -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        yield {"e": "futureEvent", "s": "BTCUSDT"}
        yield TRADE_EVENT

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        sleep=lambda _seconds: None,  # type: ignore[arg-type]
    ).stream(max_reconnects=0)
    record = await anext(stream)
    await stream.aclose()

    assert isinstance(record, Trade)
    assert calls == 1


@pytest.mark.asyncio
async def test_websocket_event_counters_reconcile_all_frame_outcomes() -> None:
    metrics = StreamRuntimeMetrics(started_at=NOW)
    zero_trade = {**TRADE_EVENT, "p": "0", "q": "0", "X": "NA", "st": 1}

    async def connector() -> AsyncIterator[dict[str, Any] | str]:
        yield "{bad-json"
        yield zero_trade
        yield {"result": None, "id": 7}
        yield {"e": "futureEvent", "s": "BTCUSDT"}
        yield TRADE_EVENT

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.PERPETUAL),
        metrics=metrics,
    ).stream(max_reconnects=0)
    record = await anext(stream)
    metrics.observe(record)
    await stream.aclose()

    payload = metrics.as_payload()
    assert payload["ws_events_received_total"] == 5
    assert payload["ws_events_accepted_total"] == 1
    assert payload["ws_events_dropped_total"] == 2
    assert payload["ws_parse_errors_total"] == 1
    assert payload["ws_status_events_total"] == 1
    assert payload["ws_invalid_price_total"] == 1
    assert payload["ws_invalid_quantity_total"] == 1
    assert payload["ws_counter_reconciled"] is True


def test_stream_metric_buffers_and_label_cardinality_are_bounded() -> None:
    metrics = StreamRuntimeMetrics(started_at=NOW, maximum_stream_labels=2)
    metrics._raw_latency_ms = deque(maxlen=128)
    metrics._adjusted_latency_ms = deque(maxlen=128)
    metrics._queue_wait_ms = deque(maxlen=128)
    parser = BinanceWebSocketParser(MarketType.SPOT)
    record = parser.parse(TRADE_EVENT)
    for index in range(10_000):
        varied = record.model_copy(update={"symbol": f"S{index % 5}USDT"})
        metrics.observe(varied)

    payload = metrics.as_payload()
    assert payload["stream_label_count"] == 2
    assert payload["evicted_stream_labels"] > 0
    assert payload["metrics_buffer_length"] == 128 * 3


@pytest.mark.asyncio
async def test_websocket_supervisor_reconnects_after_malformed_json() -> None:
    calls = 0
    waits: list[float] = []

    async def connector() -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            if False:  # pragma: no cover
                yield {}
            raise json.JSONDecodeError("bad payload", "{", 1)
        yield TRADE_EVENT

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        policy=ReconnectPolicy(base_s=1, max_s=2, jitter_ratio=0),
        sleep=sleep,
    ).stream(max_reconnects=1)
    record = await anext(stream)
    await stream.aclose()

    assert isinstance(record, Trade)
    assert calls == 2
    assert waits == [1]


@pytest.mark.asyncio
async def test_websocket_supervisor_cleans_up_across_one_hundred_reconnects() -> None:
    calls = 0
    waits: list[float] = []
    metrics = StreamRuntimeMetrics(started_at=NOW)

    async def connector() -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls <= 100:
            if False:  # pragma: no cover - keeps this an async generator
                yield {}
            raise ConnectionError("controlled disconnect")
        yield TRADE_EVENT

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        policy=ReconnectPolicy(base_s=0.001, max_s=0.001, jitter_ratio=0),
        sleep=sleep,
        metrics=metrics,
    ).stream(max_reconnects=100)
    record = await anext(stream)
    await stream.aclose()

    assert isinstance(record, Trade)
    assert calls == 101
    assert len(waits) == 100
    assert metrics.ws_reconnects_total == 100


@pytest.mark.asyncio
async def test_short_connection_message_does_not_reset_backoff_loop() -> None:
    calls = 0
    waits: list[float] = []

    async def connector() -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        calls += 1
        yield {**TRADE_EVENT, "t": calls}

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        policy=ReconnectPolicy(base_s=1, max_s=8, jitter_ratio=0),
        sleep=sleep,
        stable_connection_reset_s=900,
    ).stream(max_reconnects=3)
    for _ in range(4):
        assert isinstance(await anext(stream), Trade)
    await stream.aclose()

    assert waits == [1, 2, 4]


def test_reconnect_reason_classification() -> None:
    assert _reconnect_reason(TimeoutError()) == "READ_TIMEOUT"
    assert _reconnect_reason(ConnectionResetError()) == "CONNECTION_RESET"
    assert _reconnect_reason(socket.gaierror()) == "DNS_ERROR"
    assert _reconnect_reason(RuntimeError("unknown")) == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("compression_enabled", [True, False])
async def test_websocket_compression_modes_share_the_same_parser(
    monkeypatch: pytest.MonkeyPatch, compression_enabled: bool
) -> None:
    import websockets

    kwargs_seen: dict[str, object] = {}

    class FakeSocket:
        close_code = 1000
        close_reason = "test complete"

        def __aiter__(self) -> FakeSocket:
            return self

        async def __anext__(self) -> str:
            if kwargs_seen.get("yielded"):
                await asyncio.Event().wait()
            kwargs_seen["yielded"] = True
            return json.dumps(TRADE_EVENT)

        async def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def connect(_url: str, **kwargs: object) -> FakeSocket:
        kwargs_seen.update(kwargs)
        return FakeSocket()

    monkeypatch.setattr(websockets, "connect", connect)
    metrics = StreamRuntimeMetrics(started_at=datetime.now(tz=UTC))
    provider = BinanceWebSocketProvider(
        compression_enabled=compression_enabled,
        disconnect_after_s=1,
        metrics=metrics,
    )
    stream = provider.stream(("BTCUSDT",), MarketType.SPOT)
    record = await anext(stream)
    await stream.aclose()

    assert isinstance(record, Trade)
    assert kwargs_seen["compression"] == ("deflate" if compression_enabled else None)
    assert kwargs_seen["max_size"] == 1_048_576
    assert metrics.as_payload()["active_connections"] == 0
    assert metrics.as_payload()["connection_closure_reasons"] == {"LOCAL_SHUTDOWN": 1}


@pytest.mark.asyncio
async def test_websocket_task_cancellation_records_local_shutdown_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import websockets

    class FakeSocket:
        close_code = 1000
        close_reason = "local cancellation"

        def __init__(self) -> None:
            self._yielded = False

        def __aiter__(self) -> FakeSocket:
            return self

        async def __anext__(self) -> str:
            if self._yielded:
                await asyncio.Event().wait()
            self._yielded = True
            return json.dumps(TRADE_EVENT)

        async def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def connect(_url: str, **_kwargs: object) -> FakeSocket:
        return FakeSocket()

    monkeypatch.setattr(websockets, "connect", connect)
    metrics = StreamRuntimeMetrics(started_at=datetime.now(tz=UTC))
    stream = BinanceWebSocketProvider(metrics=metrics).stream(("BTCUSDT",), MarketType.SPOT)
    assert isinstance(await anext(stream), Trade)

    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()

    snapshot = metrics.as_payload()
    assert snapshot["active_connections"] == 0
    assert snapshot["connection_closures_total"] == 1
    assert snapshot["reconnect_reasons"] == {}
    assert snapshot["connection_closure_reasons"] == {"LOCAL_SHUTDOWN": 1}


@pytest.mark.asyncio
async def test_websocket_supervisor_timeout_fails_closed_at_retry_limit() -> None:
    async def connector() -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(1)
        yield TRADE_EVENT  # pragma: no cover

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        disconnect_after_s=0.01,
    ).stream(max_reconnects=0)
    with pytest.raises(AdapterError, match="reconnect limit exceeded"):
        await anext(stream)


@pytest.mark.asyncio
async def test_websocket_supervisor_reports_exhausted_reconnects() -> None:
    async def connector() -> AsyncIterator[dict[str, Any]]:
        if False:  # pragma: no cover
            yield {}
        raise RuntimeError("down")

    async def no_sleep(_seconds: float) -> None:
        return None

    stream = WebSocketSupervisor(
        connector,
        BinanceWebSocketParser(MarketType.SPOT),
        sleep=no_sleep,
    ).stream(max_reconnects=0)
    with pytest.raises(AdapterError, match="reconnect limit exceeded"):
        await anext(stream)
