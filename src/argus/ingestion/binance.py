"""Binance public spot/USD-M perpetual ingestion.

REST backfill/snapshot and the WebSocket event stream are deliberately separate. The
module uses only public market-data endpoints; it accepts no API key and has no order
endpoints. Thanks to transport/connector injection the default test suite stays offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import random
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, cast

from pydantic import ValidationError

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.crypto import (
    BasisSnapshot,
    CryptoMarketRecord,
    FundingRate,
    FuturesTicker,
    LiquidationEvent,
    LongShortRatio,
    OpenInterest,
    OrderBookDelta,
    OrderBookLevel,
    OrderBookSnapshot,
    SpotTicker,
    Trade,
)
from argus.core.enums import DataRepresentation, MarketType, OrderSide, Timeframe
from argus.core.errors import (
    AdapterError,
    ClockSkewError,
    ConfigError,
    DataQualityError,
    DisconnectedError,
    SequenceGapError,
    UnsupportedEndpointError,
)
from argus.core.schemas import DataStatus, Provenance, utcnow
from argus.core.trading import ExchangeFilterSet, UniverseMarketObservation
from argus.ingestion.crypto import normalize_crypto_symbol
from argus.observability.logging import get_logger

JsonObject = dict[str, Any]
RawWebSocketMessage = JsonObject | str | bytes
StreamConnector = Callable[[], AsyncIterator[RawWebSocketMessage]]
log = get_logger(__name__)


class WebSocketMetricSink(Protocol):
    def ws_received(self) -> None: ...

    def ws_dropped(
        self,
        *,
        invalid_price: bool = False,
        invalid_quantity: bool = False,
    ) -> None: ...

    def ws_status_event(self) -> None: ...

    def ws_parse_error(self) -> None: ...

    def ws_reconnect(self, event: dict[str, object] | None = None) -> None: ...

    def ws_connection_event(self, event: dict[str, object]) -> None: ...

    def ws_transport_opened(self) -> None: ...

    def ws_transport_closed(self) -> None: ...


def ensure_websocket_runtime() -> None:
    """Verify the optional WS dependency once before starting a long-lived service."""
    if importlib.util.find_spec("websockets") is None:
        raise ConfigError(
            "Binance WebSocket requires 'websockets', which is not installed; "
            "run python -m pip install 'argus[ingestion]'"
        )


def _decimal(value: object, *, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise AdapterError(f"Binance response field {field} is missing/invalid")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise AdapterError(f"Binance response field {field} is not numeric") from exc


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else _decimal(value, field="optional")


def _invalid_positive_number(value: object) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) <= 0
    except Exception:
        return True


def _timestamp_ms(value: object, *, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(cast(str | int | float, value)) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError) as exc:
        raise AdapterError(f"Binance response field {field} has an invalid timestamp") from exc


def _symbol(value: str) -> str:
    return normalize_crypto_symbol(value)


class JSONTransport(Protocol):
    """Smallest injectable surface of the REST client."""

    def get_json(self, base_url: str, path: str, params: dict[str, str | int]) -> object: ...


class UrlLibJSONTransport:
    """Stdlib HTTP transport for public endpoints; accepts no headers/secrets."""

    def __init__(self, *, timeout_s: float = 10.0) -> None:
        self._timeout_s = timeout_s

    def get_json(self, base_url: str, path: str, params: dict[str, str | int]) -> object:
        query = urllib.parse.urlencode(params)
        url = f"{base_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers={"User-Agent": "argus-crypto/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            # The URL path is safe to log; keep the query string out of the error text.
            raise AdapterError(f"Binance public REST failed: {path}") from exc


class RateLimiter:
    """Simple provider-aware, thread-safe minimum call interval."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last_request is not None:
                wait = self._interval - (now - self._last_request)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last_request = now


class BinanceRESTProvider:
    """Binance public spot and USD-M perpetual REST provider."""

    SPOT_BASE = "https://api.binance.com"
    FUTURES_BASE = "https://fapi.binance.com"

    def __init__(
        self,
        transport: JSONTransport | None = None,
        *,
        confidence: float = 0.98,
        max_clock_skew_ms: int = 30_000,
        requests_per_second: float = 10.0,
        request_attempts: int = 3,
        retry_policy: ReconnectPolicy | None = None,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        limiter: RateLimiter | None = None,
    ) -> None:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be within [0..1]")
        if max_clock_skew_ms < 0:
            raise ValueError("max_clock_skew_ms cannot be negative")
        if request_attempts < 1:
            raise ValueError("request_attempts must be at least 1")
        self._transport = transport or UrlLibJSONTransport()
        self._confidence = confidence
        self._max_clock_skew_ms = max_clock_skew_ms
        self._request_attempts = request_attempts
        self._retry_policy = retry_policy or ReconnectPolicy(base_s=0.25, max_s=5.0)
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._rng = rng
        self._limiter = limiter or RateLimiter(requests_per_second)

    @property
    def source(self) -> str:
        return "binance:public-rest"

    @staticmethod
    def _base(market_type: MarketType) -> str:
        if market_type is MarketType.SPOT:
            return BinanceRESTProvider.SPOT_BASE
        if market_type is MarketType.PERPETUAL:
            return BinanceRESTProvider.FUTURES_BASE
        raise UnsupportedEndpointError(f"unsupported Binance market_type: {market_type}")

    def _get(self, market_type: MarketType, path: str, **params: str | int) -> object:
        last_error: Exception | None = None
        for attempt in range(self._request_attempts):
            self._limiter.acquire()
            try:
                return self._transport.get_json(self._base(market_type), path, params)
            except Exception as exc:
                last_error = exc
                if attempt < self._request_attempts - 1:
                    self._sleep(self._retry_policy.delay(attempt, rng=self._rng))
        assert last_error is not None
        raise AdapterError(f"Binance public REST retry limit exceeded: {path}") from last_error

    @staticmethod
    def _object(payload: object, *, endpoint: str) -> JsonObject:
        if not isinstance(payload, dict):
            raise AdapterError(f"Binance {endpoint} did not return an object")
        return cast(JsonObject, payload)

    def check_clock_skew(self, market_type: MarketType = MarketType.SPOT) -> int:
        path = "/api/v3/time" if market_type is MarketType.SPOT else "/fapi/v1/time"
        raw = self._object(self._get(market_type, path), endpoint=path)
        server_ms = int(raw.get("serverTime", -1))
        local_ms = round(self._wall_clock() * 1000)
        skew = server_ms - local_ms
        if server_ms < 0 or abs(skew) > self._max_clock_skew_ms:
            raise ClockSkewError(
                f"Binance clock skew {skew}ms (allowed {self._max_clock_skew_ms}ms)"
            )
        return skew

    def fetch_spot_ticker(self, symbol: str) -> SpotTicker:
        canonical = _symbol(symbol)
        path = "/api/v3/ticker/24hr"
        raw = self._object(self._get(MarketType.SPOT, path, symbol=canonical), endpoint=path)
        event_time = _timestamp_ms(raw.get("closeTime"), field="closeTime")
        return SpotTicker(
            exchange="binance",
            symbol=canonical,
            event_timestamp=event_time,
            source=self.source,
            confidence=self._confidence,
            last_price=_decimal(raw.get("lastPrice"), field="lastPrice"),
            bid=_optional_decimal(raw.get("bidPrice")),
            ask=_optional_decimal(raw.get("askPrice")),
            base_volume_24h=_optional_decimal(raw.get("volume")),
            quote_volume_24h=_optional_decimal(raw.get("quoteVolume")),
        )

    def fetch_spot_universe_observations(self) -> tuple[UniverseMarketObservation, ...]:
        """Broad-scan view from public exchangeInfo + 24h ticker + bookTicker.

        Listing age, deep book and historical volatility are not part of this endpoint;
        records are therefore deliberately returned as ``PENDING`` and cannot pass the
        eligibility gate without a deep scan.
        """
        exchange_path = "/api/v3/exchangeInfo"
        ticker_path = "/api/v3/ticker/24hr"
        book_path = "/api/v3/ticker/bookTicker"
        exchange = self._object(self._get(MarketType.SPOT, exchange_path), endpoint=exchange_path)
        ticker_payload = self._get(MarketType.SPOT, ticker_path)
        book_payload = self._get(MarketType.SPOT, book_path)
        if not isinstance(exchange.get("symbols"), list):
            raise AdapterError("Binance exchangeInfo symbols is not a list")
        if not isinstance(ticker_payload, list) or not isinstance(book_payload, list):
            raise AdapterError("Binance broad-scan ticker response is not a list")
        tickers = {
            str(item.get("symbol")): cast(JsonObject, item)
            for item in ticker_payload
            if isinstance(item, dict) and item.get("symbol")
        }
        books = {
            str(item.get("symbol")): cast(JsonObject, item)
            for item in book_payload
            if isinstance(item, dict) and item.get("symbol")
        }
        observed_at = utcnow()
        output: list[UniverseMarketObservation] = []
        for item in exchange["symbols"]:
            if not isinstance(item, dict):
                raise AdapterError("Binance exchangeInfo symbol row is malformed")
            symbol_info = cast(JsonObject, item)
            # This provider's contract is Spot-USDT discovery only. Binance exchangeInfo
            # also returns other quote assets; feeding them to the USDT normalizer would
            # fail the whole broad scan on a single BNB/EUR row.
            if str(symbol_info.get("quoteAsset", "")).upper() != "USDT":
                continue
            symbol = _symbol(str(symbol_info.get("symbol", "")))
            ticker = tickers.get(symbol, {})
            book = books.get(symbol, {})
            output.append(
                UniverseMarketObservation(
                    symbol=symbol,
                    base_asset=str(symbol_info.get("baseAsset", "")),
                    quote_asset=str(symbol_info.get("quoteAsset", "")),
                    exchange_status=str(symbol_info.get("status", "")),
                    spot_allowed=bool(symbol_info.get("isSpotTradingAllowed", False)),
                    observed_at=observed_at,
                    quote_volume_24h=self._positive_optional(ticker.get("quoteVolume")),
                    trade_count_24h=self._nonnegative_int(ticker.get("count")),
                    bid=self._positive_optional(book.get("bidPrice")),
                    ask=self._positive_optional(book.get("askPrice")),
                    filters=self._spot_filters(symbol_info.get("filters")),
                    data_status=DataStatus.PENDING,
                )
            )
        return tuple(output)

    def fetch_first_spot_kline_time(self, symbol: str) -> datetime:
        """Return the documented first-daily-kline proxy instead of the listing time."""
        canonical = _symbol(symbol)
        path = "/api/v3/klines"
        payload = self._get(
            MarketType.SPOT,
            path,
            symbol=canonical,
            interval="1d",
            startTime=0,
            limit=1,
        )
        if (
            not isinstance(payload, list)
            or not payload
            or not isinstance(payload[0], list)
            or not payload[0]
        ):
            raise AdapterError(f"{canonical}: first kline proxy NO_DATA")
        return _timestamp_ms(payload[0][0], field="firstOpenTime")

    @staticmethod
    def _positive_optional(value: object) -> Decimal | None:
        parsed = _optional_decimal(value)
        return parsed if parsed is not None and parsed > 0 else None

    @staticmethod
    def _nonnegative_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(cast(str | int | float, value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _spot_filters(value: object) -> ExchangeFilterSet | None:
        if not isinstance(value, list):
            return None
        filters = {
            str(item.get("filterType")): cast(JsonObject, item)
            for item in value
            if isinstance(item, dict) and item.get("filterType")
        }
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL", {})
        try:
            return ExchangeFilterSet(
                price_tick=BinanceRESTProvider._positive_optional(price.get("tickSize")),
                min_price=BinanceRESTProvider._positive_optional(price.get("minPrice")),
                max_price=BinanceRESTProvider._positive_optional(price.get("maxPrice")),
                lot_step=BinanceRESTProvider._positive_optional(lot.get("stepSize")),
                min_quantity=BinanceRESTProvider._positive_optional(lot.get("minQty")),
                max_quantity=BinanceRESTProvider._positive_optional(lot.get("maxQty")),
                min_notional=BinanceRESTProvider._positive_optional(notional.get("minNotional")),
                max_notional=BinanceRESTProvider._positive_optional(notional.get("maxNotional")),
            )
        except ValidationError:
            return None

    def fetch_futures_ticker(self, symbol: str) -> FuturesTicker:
        canonical = _symbol(symbol)
        ticker_path = "/fapi/v1/ticker/24hr"
        premium_path = "/fapi/v1/premiumIndex"
        ticker = self._object(
            self._get(MarketType.PERPETUAL, ticker_path, symbol=canonical), endpoint=ticker_path
        )
        premium = self._object(
            self._get(MarketType.PERPETUAL, premium_path, symbol=canonical), endpoint=premium_path
        )
        event_time = _timestamp_ms(ticker.get("closeTime"), field="closeTime")
        return FuturesTicker(
            exchange="binance",
            symbol=canonical,
            event_timestamp=event_time,
            source=self.source,
            confidence=self._confidence,
            last_price=_decimal(ticker.get("lastPrice"), field="lastPrice"),
            mark_price=_optional_decimal(premium.get("markPrice")),
            index_price=_optional_decimal(premium.get("indexPrice")),
            bid=_optional_decimal(ticker.get("bidPrice")),
            ask=_optional_decimal(ticker.get("askPrice")),
            funding_rate=_optional_decimal(premium.get("lastFundingRate")),
            next_funding_timestamp=(
                _timestamp_ms(premium["nextFundingTime"], field="nextFundingTime")
                if premium.get("nextFundingTime")
                else None
            ),
        )

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 500,
    ) -> OHLCVSeries:
        if not 1 <= limit <= 1500:
            raise ValueError("limit must be within 1..1500")
        canonical = _symbol(symbol)
        path = "/api/v3/klines" if market_type is MarketType.SPOT else "/fapi/v1/klines"
        payload = self._get(
            market_type, path, symbol=canonical, interval=timeframe.value, limit=limit
        )
        if not isinstance(payload, list):
            raise AdapterError(f"Binance {path} did not return a list")
        bars: list[OHLCVBar] = []
        for raw_row in payload:
            if not isinstance(raw_row, list) or len(raw_row) < 6:
                raise AdapterError(f"Binance {path} malformed kline row")
            bars.append(
                OHLCVBar(
                    timestamp=_timestamp_ms(raw_row[0], field="openTime"),
                    open=_decimal(raw_row[1], field="open"),
                    high=_decimal(raw_row[2], field="high"),
                    low=_decimal(raw_row[3], field="low"),
                    close=_decimal(raw_row[4], field="close"),
                    volume=_decimal(raw_row[5], field="volume"),
                )
            )
        event_time = bars[-1].timestamp if bars else utcnow()
        return OHLCVSeries(
            symbol=canonical,
            timeframe=timeframe,
            bars=tuple(bars),
            provenance=Provenance(
                source=self.source,
                timestamp=event_time,
                confidence=self._confidence,
            ),
            exchange="binance",
            market_type=market_type,
            data_status=DataStatus.OK if bars else DataStatus.PENDING,
            representation=DataRepresentation.TRANSFORMED,
        )

    def fetch_order_book(
        self,
        symbol: str,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 100,
    ) -> OrderBookSnapshot:
        if limit not in {5, 10, 20, 50, 100, 500, 1000}:
            raise ValueError("unsupported Binance order-book limit")
        canonical = _symbol(symbol)
        path = "/api/v3/depth" if market_type is MarketType.SPOT else "/fapi/v1/depth"
        raw = self._object(
            self._get(market_type, path, symbol=canonical, limit=limit), endpoint=path
        )
        sequence = int(raw.get("lastUpdateId", -1))
        if sequence < 0:
            raise AdapterError("Binance depth lastUpdateId is missing")
        return OrderBookSnapshot(
            exchange="binance",
            market_type=market_type,
            symbol=canonical,
            event_timestamp=utcnow(),
            source=self.source,
            confidence=self._confidence,
            sequence=sequence,
            bids=self._levels(raw.get("bids"), field="bids"),
            asks=self._levels(raw.get("asks"), field="asks"),
        )

    @staticmethod
    def _levels(value: object, *, field: str) -> tuple[OrderBookLevel, ...]:
        if not isinstance(value, list):
            raise AdapterError(f"Binance depth {field} is not a list")
        levels: list[OrderBookLevel] = []
        for row in value:
            if not isinstance(row, list) or len(row) < 2:
                raise AdapterError(f"Binance depth {field} row is malformed")
            levels.append(
                OrderBookLevel(
                    price=_decimal(row[0], field=f"{field}.price"),
                    quantity=_decimal(row[1], field=f"{field}.quantity"),
                )
            )
        return tuple(levels)

    def fetch_funding_rate(self, symbol: str) -> FundingRate:
        canonical = _symbol(symbol)
        path = "/fapi/v1/premiumIndex"
        raw = self._object(self._get(MarketType.PERPETUAL, path, symbol=canonical), endpoint=path)
        event_time = _timestamp_ms(raw.get("time"), field="time")
        return FundingRate(
            exchange="binance",
            symbol=canonical,
            event_timestamp=event_time,
            source=self.source,
            confidence=self._confidence,
            rate=_decimal(raw.get("lastFundingRate"), field="lastFundingRate"),
            next_funding_timestamp=(
                _timestamp_ms(raw["nextFundingTime"], field="nextFundingTime")
                if raw.get("nextFundingTime")
                else None
            ),
        )

    def fetch_open_interest(self, symbol: str) -> OpenInterest:
        canonical = _symbol(symbol)
        path = "/fapi/v1/openInterest"
        raw = self._object(self._get(MarketType.PERPETUAL, path, symbol=canonical), endpoint=path)
        return OpenInterest(
            exchange="binance",
            symbol=canonical,
            event_timestamp=_timestamp_ms(raw.get("time"), field="time"),
            source=self.source,
            confidence=self._confidence,
            contracts=_decimal(raw.get("openInterest"), field="openInterest"),
        )

    def fetch_long_short_ratio(self, symbol: str, *, period: str = "5m") -> LongShortRatio:
        canonical = _symbol(symbol)
        path = "/futures/data/globalLongShortAccountRatio"
        payload = self._get(MarketType.PERPETUAL, path, symbol=canonical, period=period, limit=1)
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise AdapterError(f"Binance {path} empty/malformed response")
        raw = cast(JsonObject, payload[0])
        return LongShortRatio(
            exchange="binance",
            symbol=canonical,
            event_timestamp=_timestamp_ms(raw.get("timestamp"), field="timestamp"),
            source=self.source,
            confidence=self._confidence,
            long_ratio=_decimal(raw.get("longAccount"), field="longAccount"),
            short_ratio=_decimal(raw.get("shortAccount"), field="shortAccount"),
            account_ratio=_optional_decimal(raw.get("longShortRatio")),
        )

    def fetch_basis(self, symbol: str) -> BasisSnapshot:
        spot = self.fetch_spot_ticker(symbol)
        futures = self.fetch_futures_ticker(symbol)
        return BasisSnapshot(
            exchange="binance",
            symbol=spot.symbol,
            event_timestamp=max(spot.event_timestamp, futures.event_timestamp),
            source=self.source,
            confidence=min(spot.confidence, futures.confidence),
            spot_price=spot.last_price,
            perpetual_price=futures.mark_price or futures.last_price,
        )

    def fetch_liquidations(self, symbol: str) -> tuple[LiquidationEvent, ...]:
        raise UnsupportedEndpointError(
            f"{_symbol(symbol)}: Binance public REST does not provide liquidation history; "
            "use WebSocket"
        )


class BinanceWebSocketParser:
    """Convert Binance combined-stream JSON messages into typed contracts."""

    def __init__(self, market_type: MarketType, *, confidence: float = 0.98) -> None:
        self.market_type = market_type
        self.confidence = confidence

    @property
    def source(self) -> str:
        return "binance:public-websocket"

    def parse(self, message: JsonObject) -> CryptoMarketRecord:
        wrapped = message.get("data", message)
        if not isinstance(wrapped, dict):
            raise AdapterError("Binance WebSocket data is not an object")
        data = cast(JsonObject, wrapped)
        event = str(data.get("e", ""))
        if event in {"trade", "aggTrade"}:
            return self._trade(data, aggregate=event == "aggTrade")
        if event == "depthUpdate":
            return self._depth(data)
        if event == "forceOrder":
            return self._liquidation(data)
        if event == "24hrTicker":
            return self._ticker(data)
        raise UnsupportedEndpointError(f"unsupported Binance WebSocket event: {event or '?'}")

    def _common(self, data: JsonObject) -> dict[str, object]:
        return {
            "exchange": "binance",
            "market_type": self.market_type,
            "symbol": _symbol(str(data.get("s", ""))),
            "event_timestamp": _timestamp_ms(data.get("E"), field="E"),
            "source": self.source,
            "confidence": self.confidence,
        }

    def _trade(self, data: JsonObject, *, aggregate: bool) -> Trade:
        buyer_maker = bool(data.get("m"))
        trade_id = data.get("a") if aggregate else data.get("t")
        price = _decimal(data.get("p"), field="p")
        quantity = _decimal(data.get("q"), field="q")
        if price <= 0 or quantity <= 0:
            raise DataQualityError("Binance zero/invalid trade event")
        return Trade(
            **self._common(data),
            trade_id=str(trade_id),
            side=OrderSide.SELL if buyer_maker else OrderSide.BUY,
            price=price,
            quantity=quantity,
            is_buyer_maker=buyer_maker,
        )

    def _depth(self, data: JsonObject) -> OrderBookDelta:
        return OrderBookDelta(
            **self._common(data),
            sequence=int(data.get("u", -1)),
            first_sequence=int(data.get("U", -1)),
            final_sequence=int(data.get("u", -1)),
            previous_final_sequence=(int(data["pu"]) if data.get("pu") is not None else None),
            bids=BinanceRESTProvider._levels(data.get("b"), field="bids"),
            asks=BinanceRESTProvider._levels(data.get("a"), field="asks"),
        )

    def _liquidation(self, data: JsonObject) -> LiquidationEvent:
        order = data.get("o")
        if not isinstance(order, dict):
            raise AdapterError("Binance forceOrder 'o' object is missing")
        raw = cast(JsonObject, order)
        nested = dict(data)
        nested["s"] = raw.get("s")
        # T inside forceOrder is used as the actual trade time when available.
        nested["E"] = raw.get("T", data.get("E"))
        return LiquidationEvent(
            **self._common(nested),
            liquidation_id=f"{raw.get('s')}:{raw.get('T')}:{raw.get('S')}:{raw.get('p')}",
            side=OrderSide(str(raw.get("S", "")).lower()),
            price=_decimal(raw.get("ap") or raw.get("p"), field="price"),
            quantity=_decimal(raw.get("z") or raw.get("q"), field="quantity"),
        )

    def _ticker(self, data: JsonObject) -> CryptoMarketRecord:
        common = self._common(data)
        kwargs = {
            **common,
            "last_price": _decimal(data.get("c"), field="c"),
            "bid": _optional_decimal(data.get("b")),
            "ask": _optional_decimal(data.get("a")),
        }
        if self.market_type is MarketType.SPOT:
            return SpotTicker(
                **kwargs,
                base_volume_24h=_optional_decimal(data.get("v")),
                quote_volume_24h=_optional_decimal(data.get("q")),
            )
        return FuturesTicker(**kwargs)


class OrderBookReconciler:
    """Build a deterministic local book from a REST snapshot + ordered delta stream."""

    def __init__(self, snapshot: OrderBookSnapshot) -> None:
        if snapshot.sequence is None:
            raise ValueError("order-book snapshot must include a sequence")
        self._snapshot = snapshot
        self._bids = {level.price: level.quantity for level in snapshot.bids}
        self._asks = {level.price: level.quantity for level in snapshot.asks}
        self._sequence = snapshot.sequence
        # Persisted materialized books must not grow without bound as prices visit
        # new levels. Keep a deeper internal working set, but expose only the same
        # depth as the authoritative REST snapshot.
        self._output_depth = max(1, len(snapshot.bids), len(snapshot.asks))
        self._tracked_depth = max(5_000, self._output_depth)
        # The REST snapshot is taken mid-stream. The first covering delta bridges to the
        # snapshot; futures ``pu`` continuity can be enforced strictly only after that bridge.
        self._synchronized = False

    @property
    def snapshot(self) -> OrderBookSnapshot:
        return self._snapshot

    def apply(self, delta: OrderBookDelta) -> OrderBookSnapshot:
        if (
            delta.exchange != self._snapshot.exchange
            or delta.market_type is not self._snapshot.market_type
            or delta.symbol != self._snapshot.symbol
        ):
            raise SequenceGapError("delta does not match the snapshot identity")
        if delta.final_sequence <= self._sequence:
            return self._snapshot  # pre-snapshot/duplicate delta: skip idempotently
        expected = self._sequence + 1
        if self._synchronized and delta.previous_final_sequence is not None:
            # USD-M update ids are not consecutive integers; Binance's continuity
            # contract is that the new event's ``pu`` equals the previous ``u``.
            continuous = delta.previous_final_sequence == self._sequence
        elif not self._synchronized and delta.previous_final_sequence is not None:
            # The first futures delta must cover the REST snapshot update id.
            continuous = delta.first_sequence <= self._sequence <= delta.final_sequence
        else:
            # The spot stream carries no ``pu``; snapshot+1 must fall within the range.
            continuous = delta.first_sequence <= expected <= delta.final_sequence
        if not continuous:
            raise SequenceGapError(
                f"order-book sequence gap: expected {expected}, got "
                f"{delta.first_sequence}..{delta.final_sequence}"
            )
        self._apply_side(self._bids, delta.bids)
        self._apply_side(self._asks, delta.asks)
        self._trim_internal()
        self._sequence = delta.final_sequence
        self._synchronized = True
        candidate = self._snapshot.model_copy(
            update={
                "event_timestamp": delta.event_timestamp,
                "ingestion_timestamp": delta.ingestion_timestamp,
                "source": delta.source,
                "confidence": min(self._snapshot.confidence, delta.confidence),
                "sequence": self._sequence,
                "bids": tuple(
                    OrderBookLevel(price=price, quantity=quantity)
                    for price, quantity in sorted(self._bids.items(), reverse=True)[
                        : self._output_depth
                    ]
                ),
                "asks": tuple(
                    OrderBookLevel(price=price, quantity=quantity)
                    for price, quantity in sorted(self._asks.items())[: self._output_depth]
                ),
            }
        )
        if candidate.crossed:
            raise DataQualityError("order-book crossed; snapshot must be resynchronized")
        self._snapshot = candidate
        return candidate

    def _trim_internal(self) -> None:
        if len(self._bids) > self._tracked_depth:
            self._bids = dict(sorted(self._bids.items(), reverse=True)[: self._tracked_depth])
        if len(self._asks) > self._tracked_depth:
            self._asks = dict(sorted(self._asks.items())[: self._tracked_depth])

    @staticmethod
    def _apply_side(book: dict[Decimal, Decimal], updates: tuple[OrderBookLevel, ...]) -> None:
        for level in updates:
            if level.quantity == 0:
                book.pop(level.price, None)
            else:
                book[level.price] = level.quantity


class ReconnectPolicy:
    """Exponential backoff + bounded jitter; the RNG is injectable in tests."""

    def __init__(self, *, base_s: float = 1.0, max_s: float = 60.0, jitter_ratio: float = 0.2):
        if base_s <= 0 or max_s < base_s or not 0 <= jitter_ratio <= 1:
            raise ValueError("invalid reconnect policy")
        self.base_s = base_s
        self.max_s = max_s
        self.jitter_ratio = jitter_ratio

    def delay(self, attempt: int, *, rng: Callable[[], float] = random.random) -> float:
        raw: float = min(self.base_s * float(2**attempt), self.max_s)
        factor: float = 1.0 + ((rng() * 2.0) - 1.0) * self.jitter_ratio
        return max(0.0, raw * factor)


class ReconnectReason(StrEnum):
    REMOTE_CLOSE = "REMOTE_CLOSE"
    PING_TIMEOUT = "PING_TIMEOUT"
    READ_TIMEOUT = "READ_TIMEOUT"
    CONNECTION_RESET = "CONNECTION_RESET"
    DNS_ERROR = "DNS_ERROR"
    TLS_ERROR = "TLS_ERROR"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    PARSE_ERROR = "PARSE_ERROR"
    PROACTIVE_RENEWAL = "PROACTIVE_RENEWAL"
    LOCAL_SHUTDOWN = "LOCAL_SHUTDOWN"
    UNKNOWN = "UNKNOWN"


@dataclass
class _ConnectionAttemptState:
    connected_at: datetime | None = None
    connected_monotonic: float | None = None
    last_message_monotonic: float | None = None
    close_code: int | None = None
    close_reason: str | None = None

    def reset(self) -> None:
        self.connected_at = None
        self.connected_monotonic = None
        self.last_message_monotonic = None
        self.close_code = None
        self.close_reason = None


def _masked_exception_message(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")[:500]
    return re.sub(
        r"(?i)(api[_-]?key|secret|signature|token)(\s*[=:]\s*)([^&\s]+)",
        r"\1\2<redacted>",
        message,
    )


def _reconnect_reason(exc: BaseException) -> ReconnectReason:
    name = type(exc).__name__
    message = str(exc).casefold()
    if isinstance(exc, TimeoutError):
        return ReconnectReason.READ_TIMEOUT
    if isinstance(exc, socket.gaierror):
        return ReconnectReason.DNS_ERROR
    if isinstance(exc, ssl.SSLError):
        return ReconnectReason.TLS_ERROR
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return ReconnectReason.CONNECTION_RESET
    if "connectionclosed" in name.casefold():
        if "ping timeout" in message or "keepalive" in message:
            return ReconnectReason.PING_TIMEOUT
        return ReconnectReason.REMOTE_CLOSE
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return ReconnectReason.PARSE_ERROR
    if isinstance(exc, SequenceGapError):
        return ReconnectReason.SEQUENCE_GAP
    return ReconnectReason.UNKNOWN


def _connection_close_details(exc: BaseException) -> tuple[int | None, str | None]:
    received = getattr(exc, "rcvd", None)
    code = getattr(received, "code", None)
    reason = getattr(received, "reason", None)
    if code is None:
        code = getattr(exc, "code", None)
    if reason is None:
        reason = getattr(exc, "reason", None)
    return (int(code) if isinstance(code, int) else None, str(reason)[:200] if reason else None)


class WebSocketSupervisor:
    """Consume the injected WS connector with reconnect and a disconnect timeout."""

    def __init__(
        self,
        connector: StreamConnector,
        parser: BinanceWebSocketParser,
        *,
        policy: ReconnectPolicy | None = None,
        disconnect_after_s: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        metrics: WebSocketMetricSink | None = None,
        worker_id: str = "unassigned",
        symbol: str = "*",
        stream_type: str = "combined",
        compression_enabled: bool = True,
        stable_connection_reset_s: float = 900.0,
        attempt_state: _ConnectionAttemptState | None = None,
    ) -> None:
        if disconnect_after_s <= 0 or stable_connection_reset_s <= 0:
            raise ValueError("disconnect/stable durations must be positive")
        self._connector = connector
        self._parser = parser
        self._policy = policy or ReconnectPolicy()
        self._disconnect_after_s = disconnect_after_s
        self._sleep = sleep
        self._rng = rng
        self._metrics = metrics
        self._worker_id = worker_id
        self._symbol = symbol
        self._stream_type = stream_type
        self._compression_enabled = compression_enabled
        self._stable_connection_reset_s = stable_connection_reset_s
        self._attempt_state = attempt_state or _ConnectionAttemptState()

    def _event(
        self,
        *,
        exc: BaseException,
        reason: ReconnectReason,
        attempt_number: int,
        backoff_seconds: float,
        started_at: datetime,
        started_monotonic: float,
        shutdown_in_progress: bool,
    ) -> dict[str, object]:
        now = datetime.now(tz=UTC)
        monotonic_now = time.monotonic()
        connected_at = self._attempt_state.connected_at or started_at
        connected_monotonic = self._attempt_state.connected_monotonic or started_monotonic
        close_code, close_reason = _connection_close_details(exc)
        close_code = close_code if close_code is not None else self._attempt_state.close_code
        close_reason = close_reason or self._attempt_state.close_reason
        last_message_age = (
            max(0.0, monotonic_now - self._attempt_state.last_message_monotonic)
            if self._attempt_state.last_message_monotonic is not None
            else None
        )
        return {
            "worker_id": self._worker_id,
            "symbol": self._symbol,
            "market_type": self._parser.market_type.value,
            "stream_type": self._stream_type,
            "connected_at": connected_at.isoformat(),
            "disconnected_at": now.isoformat(),
            "connection_lifetime_seconds": round(max(0.0, monotonic_now - connected_monotonic), 3),
            "reconnect_reason": reason.value,
            "close_code": close_code,
            "close_reason": close_reason,
            "exception_type": type(exc).__name__,
            "exception_message_masked": _masked_exception_message(exc),
            # The library owns ping/pong and doesn't expose a stable last-pong API.
            "ping_age": None,
            "last_message_age": round(last_message_age, 3)
            if last_message_age is not None
            else None,
            "sequence_gap_detected": reason is ReconnectReason.SEQUENCE_GAP,
            "resync_requested": False,
            "attempt_number": attempt_number,
            "backoff_seconds": round(backoff_seconds, 3),
            "compression_enabled": self._compression_enabled,
            "shutdown_in_progress": shutdown_in_progress,
        }

    async def stream(
        self, *, max_reconnects: int | None = None
    ) -> AsyncIterator[CryptoMarketRecord]:
        failures = 0
        total_reconnects = 0
        dropped_records = 0
        while True:
            attempt_started_at = datetime.now(tz=UTC)
            attempt_started_monotonic = time.monotonic()
            self._attempt_state.reset()
            try:
                iterator = self._connector().__aiter__()
                try:
                    while True:
                        try:
                            raw_message = await asyncio.wait_for(
                                anext(iterator), timeout=self._disconnect_after_s
                            )
                        except StopAsyncIteration as exc:
                            raise DisconnectedError("Binance WebSocket stream closed") from exc
                        if self._metrics is not None:
                            self._metrics.ws_received()
                        self._attempt_state.last_message_monotonic = time.monotonic()
                        try:
                            if isinstance(raw_message, bytes):
                                decoded: object = json.loads(raw_message.decode("utf-8"))
                            elif isinstance(raw_message, str):
                                decoded = json.loads(raw_message)
                            else:
                                decoded = raw_message
                            if not isinstance(decoded, dict):
                                raise AdapterError("Binance WebSocket JSON is not an object")
                            message = cast(JsonObject, decoded)
                        except (UnicodeDecodeError, json.JSONDecodeError, AdapterError) as exc:
                            if self._metrics is not None:
                                self._metrics.ws_parse_error()
                            dropped_records += 1
                            if dropped_records == 1 or dropped_records % 100 == 0:
                                log.warning(
                                    "binance-websocket-parse-error",
                                    market_type=self._parser.market_type.value,
                                    error_type=type(exc).__name__,
                                    dropped_count=dropped_records,
                                )
                            continue
                        if "id" in message and "result" in message:
                            if self._metrics is not None:
                                self._metrics.ws_status_event()
                            continue
                        try:
                            record = self._parser.parse(message)
                        except (
                            DataQualityError,
                            UnsupportedEndpointError,
                            ValidationError,
                        ) as exc:
                            wrapped = message.get("data", message)
                            event_type = wrapped.get("e") if isinstance(wrapped, dict) else None
                            symbol = wrapped.get("s") if isinstance(wrapped, dict) else None
                            dropped_records += 1
                            invalid_price = (
                                _invalid_positive_number(wrapped.get("p"))
                                if isinstance(wrapped, dict)
                                else False
                            )
                            invalid_quantity = (
                                _invalid_positive_number(wrapped.get("q"))
                                if isinstance(wrapped, dict)
                                else False
                            )
                            if self._metrics is not None:
                                self._metrics.ws_dropped(
                                    invalid_price=invalid_price,
                                    invalid_quantity=invalid_quantity,
                                )
                            details = {
                                "market_type": self._parser.market_type.value,
                                "symbol": symbol,
                                "event_type": event_type,
                                "error_type": type(exc).__name__,
                                "dropped_count": dropped_records,
                            }
                            if dropped_records == 1 or dropped_records % 100 == 0:
                                log.warning("binance-websocket-record-dropped", **details)
                            else:
                                log.debug("binance-websocket-record-dropped", **details)
                            continue
                        except AdapterError as exc:
                            if self._metrics is not None:
                                self._metrics.ws_parse_error()
                            dropped_records += 1
                            if dropped_records == 1 or dropped_records % 100 == 0:
                                log.warning(
                                    "binance-websocket-record-parse-error",
                                    market_type=self._parser.market_type.value,
                                    error_type=type(exc).__name__,
                                    dropped_count=dropped_records,
                                )
                            continue
                        yield record
                finally:
                    # Cancellation while awaiting ``anext`` does not guarantee that
                    # an injected async-generator connector is finalized promptly.
                    # Explicitly close it before a retry or worker shutdown so TLS,
                    # compression and socket state cannot outlive the worker.
                    aclose = getattr(iterator, "aclose", None)
                    if aclose is not None:
                        await aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if max_reconnects is not None and total_reconnects >= max_reconnects:
                    raise AdapterError("Binance WebSocket reconnect limit exceeded") from exc
                lifetime = time.monotonic() - (
                    self._attempt_state.connected_monotonic or attempt_started_monotonic
                )
                if lifetime >= self._stable_connection_reset_s:
                    failures = 0
                delay = self._policy.delay(failures, rng=self._rng)
                failures += 1
                total_reconnects += 1
                reason = _reconnect_reason(exc)
                event = self._event(
                    exc=exc,
                    reason=reason,
                    attempt_number=failures,
                    backoff_seconds=delay,
                    started_at=attempt_started_at,
                    started_monotonic=attempt_started_monotonic,
                    shutdown_in_progress=False,
                )
                if self._metrics is not None:
                    self._metrics.ws_reconnect(event)
                log.warning(
                    "binance-websocket-reconnect",
                    **event,
                )
                await self._sleep(delay)


class BinanceWebSocketProvider:
    """Real Binance combined-stream connection; ``websockets`` is lazily imported."""

    SPOT_BASE = "wss://stream.binance.com:9443/stream"
    FUTURES_BASE = "wss://fstream.binance.com/stream"

    def __init__(
        self,
        *,
        policy: ReconnectPolicy | None = None,
        disconnect_after_s: float = 30.0,
        metrics: WebSocketMetricSink | None = None,
        compression_enabled: bool = True,
        max_message_size: int = 1_048_576,
        max_queue_messages: int = 16,
        ping_interval_s: float = 20.0,
        ping_timeout_s: float = 20.0,
        open_timeout_s: float = 10.0,
        close_timeout_s: float = 10.0,
        stable_connection_reset_s: float = 900.0,
    ) -> None:
        self._policy = policy or ReconnectPolicy()
        self._disconnect_after_s = disconnect_after_s
        self._metrics = metrics
        self._compression_enabled = compression_enabled
        self._max_message_size = max_message_size
        self._max_queue_messages = max_queue_messages
        self._ping_interval_s = ping_interval_s
        self._ping_timeout_s = ping_timeout_s
        self._open_timeout_s = open_timeout_s
        self._close_timeout_s = close_timeout_s
        self._stable_connection_reset_s = stable_connection_reset_s

    @staticmethod
    def url(symbols: tuple[str, ...], market_type: MarketType) -> str:
        if not symbols:
            raise ValueError("at least one WebSocket symbol is required")
        streams: list[str] = []
        for symbol in symbols:
            root = _symbol(symbol).lower()
            streams.extend((f"{root}@trade", f"{root}@depth@100ms", f"{root}@ticker"))
            if market_type is MarketType.PERPETUAL:
                streams.append(f"{root}@forceOrder")
        base = (
            BinanceWebSocketProvider.SPOT_BASE
            if market_type is MarketType.SPOT
            else BinanceWebSocketProvider.FUTURES_BASE
        )
        return f"{base}?streams={'/'.join(streams)}"

    async def stream(
        self, symbols: tuple[str, ...], market_type: MarketType
    ) -> AsyncIterator[CryptoMarketRecord]:
        url = self.url(symbols, market_type)
        worker_id = f"{market_type.value}:combined"
        attempt_state = _ConnectionAttemptState()

        async def connector() -> AsyncIterator[RawWebSocketMessage]:
            import websockets  # lazy: default unit tests and REST do not require it

            started = time.monotonic()
            websocket = await websockets.connect(
                url,
                compression="deflate" if self._compression_enabled else None,
                max_size=self._max_message_size,
                max_queue=self._max_queue_messages,
                ping_interval=self._ping_interval_s,
                ping_timeout=self._ping_timeout_s,
                open_timeout=self._open_timeout_s,
                close_timeout=self._close_timeout_s,
            )
            attempt_state.connected_at = datetime.now(tz=UTC)
            attempt_state.connected_monotonic = time.monotonic()
            if self._metrics is not None:
                self._metrics.ws_transport_opened()
            local_shutdown_type: str | None = None
            try:
                log.info(
                    "binance-websocket-connected",
                    worker_id=worker_id,
                    market_type=market_type.value,
                    symbols=symbols,
                    connect_ms=round((time.monotonic() - started) * 1000, 1),
                    compression_enabled=self._compression_enabled,
                )
                async for raw in websocket:
                    attempt_state.last_message_monotonic = time.monotonic()
                    yield raw
            except (asyncio.CancelledError, GeneratorExit) as exc:
                local_shutdown_type = type(exc).__name__
                raise
            finally:
                with contextlib.suppress(Exception):
                    await websocket.close()
                with contextlib.suppress(Exception):
                    await websocket.wait_closed()
                attempt_state.close_code = websocket.close_code
                attempt_state.close_reason = websocket.close_reason
                if self._metrics is not None:
                    self._metrics.ws_transport_closed()
                if local_shutdown_type is not None:
                    disconnected_at = datetime.now(tz=UTC)
                    lifetime = max(
                        0.0,
                        time.monotonic() - (attempt_state.connected_monotonic or time.monotonic()),
                    )
                    last_message_age = (
                        max(0.0, time.monotonic() - attempt_state.last_message_monotonic)
                        if attempt_state.last_message_monotonic is not None
                        else None
                    )
                    event: dict[str, object] = {
                        "worker_id": worker_id,
                        "symbol": ",".join(symbols),
                        "market_type": market_type.value,
                        "stream_type": "combined",
                        "connected_at": (attempt_state.connected_at or disconnected_at).isoformat(),
                        "disconnected_at": disconnected_at.isoformat(),
                        "connection_lifetime_seconds": round(lifetime, 3),
                        "reconnect_reason": ReconnectReason.LOCAL_SHUTDOWN.value,
                        "close_code": attempt_state.close_code,
                        "close_reason": attempt_state.close_reason,
                        "exception_type": local_shutdown_type,
                        "exception_message_masked": "",
                        "ping_age": None,
                        "last_message_age": (
                            round(last_message_age, 3) if last_message_age is not None else None
                        ),
                        "sequence_gap_detected": False,
                        "resync_requested": False,
                        "attempt_number": 0,
                        "backoff_seconds": 0.0,
                        "compression_enabled": self._compression_enabled,
                        "shutdown_in_progress": True,
                    }
                    if self._metrics is not None:
                        self._metrics.ws_connection_event(event)
                    log.info("binance-websocket-connection-closed", **event)

        supervisor = WebSocketSupervisor(
            connector,
            BinanceWebSocketParser(market_type),
            policy=self._policy,
            disconnect_after_s=self._disconnect_after_s,
            metrics=self._metrics,
            worker_id=worker_id,
            symbol=",".join(symbols),
            compression_enabled=self._compression_enabled,
            stable_connection_reset_s=self._stable_connection_reset_s,
            attempt_state=attempt_state,
        )
        supervised_stream = supervisor.stream()
        try:
            async for record in supervised_stream:
                yield record
        finally:
            aclose = getattr(supervised_stream, "aclose", None)
            if aclose is not None:
                await aclose()
