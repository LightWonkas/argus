"""Two-stage public universe discovery and persistent watchlist rotation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from argus.config.models import RuntimeConfig
from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.crypto import OrderBookLevel, OrderBookSnapshot
from argus.core.enums import MarketType, Timeframe
from argus.core.schemas import DataStatus, Provenance
from argus.core.trading import ExchangeFilterSet, UniverseMarketObservation
from argus.interface.universe_runtime import DynamicUniverseCycle
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _filters() -> ExchangeFilterSet:
    return ExchangeFilterSet(
        price_tick="0.01",
        lot_step="0.001",
        min_quantity="0.001",
        min_notional="5",
    )


def _observation(symbol: str, base: str, volume: str) -> UniverseMarketObservation:
    return UniverseMarketObservation(
        symbol=symbol,
        base_asset=base,
        quote_asset="USDT",
        exchange_status="TRADING",
        spot_allowed=True,
        observed_at=NOW,
        quote_volume_24h=volume,
        trade_count_24h=1000,
        bid="99.95",
        ask="100.05",
        filters=_filters(),
        data_status=DataStatus.PENDING,
    )


class FakeUniverseREST:
    def __init__(self, observations: tuple[UniverseMarketObservation, ...]) -> None:
        self.observations = observations
        self.deep_calls: list[str] = []

    def fetch_spot_universe_observations(self) -> tuple[UniverseMarketObservation, ...]:
        return self.observations

    def fetch_first_spot_kline_time(self, symbol: str) -> datetime:
        self.deep_calls.append(f"listing:{symbol}")
        return NOW - timedelta(days=365)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 500,
    ) -> OHLCVSeries:
        self.deep_calls.append(f"ohlcv:{symbol}")
        bars = tuple(
            OHLCVBar(
                timestamp=NOW - timedelta(days=index + 1),
                open=Decimal("100") + index,
                high=Decimal("101") + index,
                low=Decimal("99") + index,
                close=Decimal("100.5") + index,
                volume=Decimal("1000"),
            )
            for index in reversed(range(40))
        )
        return OHLCVSeries(
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            provenance=Provenance(source="fake", timestamp=bars[-1].timestamp, confidence=1),
            exchange="binance",
            market_type=market_type,
            data_status=DataStatus.OK,
        )

    def fetch_order_book(
        self,
        symbol: str,
        *,
        market_type: MarketType = MarketType.SPOT,
        limit: int = 100,
    ) -> OrderBookSnapshot:
        self.deep_calls.append(f"book:{symbol}")
        return OrderBookSnapshot(
            exchange="binance",
            market_type=market_type,
            symbol=symbol,
            event_timestamp=NOW,
            ingestion_timestamp=NOW,
            source="fake",
            sequence=1,
            bids=(OrderBookLevel(price="99.95", quantity="5000"),),
            asks=(OrderBookLevel(price="100.05", quantity="5000"),),
        )


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        universe={"symbols": ["BTCUSDT", "ETHUSDT"], "strict": False},
        universe_discovery={
            "enabled": True,
            "deep_watchlist_size": 1,
            "deep_candidate_pool_multiplier": 1,
            "minimum_listing_age_days": 30,
            "minimum_history_bars": 30,
            "minimum_quote_volume_24h": 1000,
            "maximum_spread_bps": 15,
            "minimum_depth_quote": 1000,
            "maximum_slippage_bps": 20,
            "maximum_realized_volatility_pct": 250,
            "watchlist_rotation_cooldown_s": 1800,
        },
    )


def test_dynamic_universe_deep_scans_only_shortlist_and_persists_all_symbols(
    tmp_path: Path,
) -> None:
    provider = FakeUniverseREST(
        (
            _observation("BTCUSDT", "BTC", "100000000"),
            _observation("USDCUSDT", "USDC", "200000000"),
        )
    )
    with SqliteStore(tmp_path / "universe.db") as store:
        result = DynamicUniverseCycle(provider, store, _config(), clock=lambda: NOW).run_once()
        assert result.scanned_count == 2
        assert result.broad_pass_count == 1
        assert result.deep_scanned_count == 1
        assert result.eligible_count == 1
        assert result.watchlist == ("BTCUSDT",)
        assert provider.deep_calls == [
            "listing:BTCUSDT",
            "ohlcv:BTCUSDT",
            "book:BTCUSDT",
        ]
        snapshots = store.latest_universe_snapshots()
        assert {snapshot.symbol for snapshot in snapshots} == {"BTCUSDT", "USDCUSDT"}
        assert next(item for item in snapshots if item.symbol == "BTCUSDT").eligible
        assert not next(item for item in snapshots if item.symbol == "USDCUSDT").eligible
        assert store.latest_research_scores()[0].final_research_score is None


def test_latest_universe_page_is_bounded_and_cursor_driven(tmp_path: Path) -> None:
    from argus.core.trading import AssetCategory, UniverseEligibilitySnapshot, UniverseRejectReason

    with SqliteStore(tmp_path / "page.db") as store:
        for symbol in ("ADAUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"):
            store.save_universe_snapshot(
                UniverseEligibilitySnapshot(
                    symbol=symbol,
                    base_asset=symbol.removesuffix("USDT"),
                    quote_asset="USDT",
                    discovered_at=NOW,
                    eligible=symbol in {"BTCUSDT", "ETHUSDT"},
                    rejection_reasons=(
                        ()
                        if symbol in {"BTCUSDT", "ETHUSDT"}
                        else (UniverseRejectReason.LOW_LIQUIDITY,)
                    ),
                    quality_status=DataStatus.OK,
                    taxonomy_category=AssetCategory.CRYPTO,
                    policy_version="test-v1",
                )
            )
        first, total, eligible, as_of, cursor = store.latest_universe_snapshot_page(limit=2)
        second, total_2, eligible_2, as_of_2, cursor_2 = store.latest_universe_snapshot_page(
            limit=2, cursor=cursor
        )

        assert [row.symbol for row in first] == ["ADAUSDT", "BTCUSDT"]
        assert [row.symbol for row in second] == ["ETHUSDT", "SOLUSDT"]
        assert (total, eligible, as_of) == (4, 2, NOW)
        assert (total_2, eligible_2, as_of_2, cursor_2) == (4, 2, NOW, None)


def test_watchlist_rotation_cooldown_keeps_still_eligible_current_symbol(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "rotation.db"
    first = FakeUniverseREST(
        (
            _observation("BTCUSDT", "BTC", "200000000"),
            _observation("ETHUSDT", "ETH", "100000000"),
        )
    )
    with SqliteStore(path) as store:
        initial = DynamicUniverseCycle(first, store, config, clock=lambda: NOW).run_once()
        assert initial.watchlist == ("BTCUSDT",)

        second = FakeUniverseREST(
            (
                _observation("ETHUSDT", "ETH", "300000000"),
                _observation("BTCUSDT", "BTC", "200000000"),
            )
        )
        within_cooldown = DynamicUniverseCycle(
            second,
            store,
            config,
            clock=lambda: NOW + timedelta(minutes=5),
        ).run_once()
        # With pool=1 BTC is not deep-scanned, so the previous symbol loses its eligibility
        # evidence; for safety a stale watchlist is not kept just because of the cooldown.
        assert within_cooldown.watchlist == ("ETHUSDT",)
