"""Low-cost broad scan and bounded deep discovery of Binance spot markets."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Protocol

from argus.config.models import RuntimeConfig
from argus.core.contracts import OHLCVSeries
from argus.core.crypto import OrderBookSnapshot
from argus.core.enums import MarketType, OrderSide, Timeframe
from argus.core.schemas import DataStatus, utcnow
from argus.core.trading import UniverseEligibilitySnapshot, UniverseMarketObservation
from argus.decision.costs import FeeAndCostEngine
from argus.platform.universe import UniverseDiscoveryEngine
from argus.storage.sqlite import SqliteStore


class UniverseRESTClient(Protocol):
    def fetch_spot_universe_observations(self) -> tuple[UniverseMarketObservation, ...]: ...

    def fetch_first_spot_kline_time(self, symbol: str) -> datetime: ...

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


@dataclass(frozen=True)
class UniverseCycleResult:
    scanned_count: int
    broad_pass_count: int
    deep_scanned_count: int
    eligible_count: int
    watchlist: tuple[str, ...]
    errors: tuple[str, ...] = ()


class DynamicUniverseCycle:
    """Produces PIT snapshots; missing deep data is never treated as eligible."""

    def __init__(
        self,
        provider: UniverseRESTClient,
        store: SqliteStore,
        config: RuntimeConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.provider = provider
        self.store = store
        self.config = config
        self.clock = clock
        self.engine = UniverseDiscoveryEngine(config.universe_discovery)

    def run_once(self) -> UniverseCycleResult:
        if not self.config.universe_discovery.enabled:
            return UniverseCycleResult(0, 0, 0, 0, tuple(self.config.universe.symbols))
        at = self.clock().astimezone(UTC)
        observations = self.provider.fetch_spot_universe_observations()
        broad = [
            observation
            for observation in observations
            if not self.engine.broad_rejection_reasons(observation)
        ]
        broad.sort(
            key=lambda item: (
                -(item.quote_volume_24h or Decimal("0")),
                item.spread_bps if item.spread_bps is not None else Decimal("Infinity"),
                item.symbol,
            )
        )
        policy = self.config.universe_discovery
        pool_size = policy.deep_watchlist_size * policy.deep_candidate_pool_multiplier
        errors: list[str] = []
        eligibility: list[UniverseEligibilitySnapshot] = []
        deep_observations = broad[:pool_size]
        deep_symbols = {observation.symbol for observation in deep_observations}
        for observation in observations:
            if observation.symbol in deep_symbols:
                continue
            self.store.save_universe_snapshot(
                self.engine.evaluate(
                    observation,
                    discovered_at=at,
                    maximum_trade_notional=Decimal(str(policy.liquidity_probe_notional)),
                )
            )
        for observation in deep_observations:
            try:
                enriched = self._enrich(observation)
            except Exception as exc:
                errors.append(f"{observation.symbol}: {type(exc).__name__}: {exc}")
                enriched = observation
            snapshot = self.engine.evaluate(
                enriched,
                discovered_at=at,
                maximum_trade_notional=Decimal(str(policy.liquidity_probe_notional)),
            )
            self.store.save_universe_snapshot(snapshot)
            self.store.save_research_snapshot(
                self.engine.score(
                    snapshot,
                    liquidity=None,
                    trend=None,
                    momentum=None,
                    order_flow=None,
                    regime_compatibility=None,
                    execution_quality=None,
                    risk=None,
                    manipulation_risk=None,
                    cost_adjusted_edge=None,
                    portfolio_fit=None,
                    evidence=("universe:public-spot",),
                    data_sources=("binance:public-rest",),
                    model_version="broad-deep-eligibility-v1",
                )
            )
            eligibility.append(snapshot)
        eligible = [snapshot for snapshot in eligibility if snapshot.eligible]
        eligible.sort(
            key=lambda item: (
                -(item.quote_volume_24h or Decimal("0")),
                item.spread_bps if item.spread_bps is not None else Decimal("Infinity"),
                item.symbol,
            )
        )
        proposed = tuple(item.symbol for item in eligible[: policy.deep_watchlist_size])
        watchlist = self._rotate_watchlist(proposed, eligible, at)
        self.store.set_checkpoint("health.universe_discovery", at.isoformat())
        return UniverseCycleResult(
            scanned_count=len(observations),
            broad_pass_count=len(broad),
            deep_scanned_count=min(len(broad), pool_size),
            eligible_count=len(eligible),
            watchlist=watchlist,
            errors=tuple(errors),
        )

    def _enrich(self, observation: UniverseMarketObservation) -> UniverseMarketObservation:
        policy = self.config.universe_discovery
        listing_time = self.provider.fetch_first_spot_kline_time(observation.symbol)
        limit = min(1500, max(policy.minimum_history_bars, 365))
        series = self.provider.fetch_ohlcv(
            observation.symbol,
            Timeframe.D1,
            market_type=MarketType.SPOT,
            limit=limit,
        )
        book = self.provider.fetch_order_book(
            observation.symbol,
            market_type=MarketType.SPOT,
            limit=self.config.ingestion.order_book_depth,
        )
        bid_depth = sum((level.price * level.quantity for level in book.bids), Decimal("0"))
        ask_depth = sum((level.price * level.quantity for level in book.asks), Decimal("0"))
        if book.best_ask is None:
            raise ValueError("ask side NO_DATA")
        probe_quantity = Decimal(str(policy.liquidity_probe_notional)) / book.best_ask
        fill = FeeAndCostEngine.simulate_market_fill(
            book,
            side=OrderSide.BUY,
            quantity=probe_quantity,
        )
        slippage = (
            fill.slippage_bps
            if fill.fully_fillable
            else Decimal(str(policy.maximum_slippage_bps)) + Decimal("1")
        )
        values = observation.model_dump(exclude_computed_fields=True)
        values.update(
            {
                "listing_time": listing_time,
                "listing_time_source": "binance:first-daily-kline-proxy",
                "estimated_depth_quote": min(bid_depth, ask_depth),
                "estimated_slippage_bps": slippage,
                "realized_volatility_pct": self._annualized_volatility(series),
                "history_bars": len(series.bars),
                "data_status": (
                    DataStatus.OK
                    if series.data_status is DataStatus.OK and book.data_status is DataStatus.OK
                    else DataStatus.PENDING
                ),
            }
        )
        return UniverseMarketObservation.model_validate(values)

    @staticmethod
    def _annualized_volatility(series: OHLCVSeries) -> Decimal | None:
        closes = [float(bar.close) for bar in series.bars]
        if len(closes) < 2 or any(value <= 0 for value in closes):
            return None
        returns = [math.log(current / previous) for previous, current in pairwise(closes)]
        if not returns:
            return None
        annualized = statistics.pstdev(returns) * math.sqrt(365) * 100
        return Decimal(str(annualized))

    def _rotate_watchlist(
        self,
        proposed: tuple[str, ...],
        eligible: list[UniverseEligibilitySnapshot],
        now: datetime,
    ) -> tuple[str, ...]:
        raw = self.store.get_checkpoint("dynamic_universe:watchlist")
        current: tuple[str, ...] = ()
        changed_at: datetime | None = None
        if raw is not None:
            try:
                payload = json.loads(raw)
                current = tuple(str(symbol) for symbol in payload["symbols"])
                changed_at = datetime.fromisoformat(str(payload["changed_at"])).astimezone(UTC)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                current = ()
                changed_at = None
        eligible_symbols = {snapshot.symbol for snapshot in eligible}
        safe_current = tuple(symbol for symbol in current if symbol in eligible_symbols)
        cooldown = self.config.universe_discovery.watchlist_rotation_cooldown_s
        if (
            safe_current
            and changed_at is not None
            and (now - changed_at).total_seconds() < cooldown
        ):
            selected = safe_current
        else:
            selected = proposed
        if selected != current:
            self.store.set_checkpoint(
                "dynamic_universe:watchlist",
                json.dumps(
                    {"symbols": selected, "changed_at": now.isoformat()},
                    separators=(",", ":"),
                ),
            )
        return selected
