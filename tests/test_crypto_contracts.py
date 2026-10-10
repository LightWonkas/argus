"""Crypto contract and idempotent SQLite migration tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.crypto import (
    BasisSnapshot,
    CryptoAnalysisContext,
    FeatureSnapshot,
    FundingRate,
    FuturesTicker,
    LiquidationEvent,
    LongShortRatio,
    MarketRegime,
    OpenInterest,
    OrderBookDelta,
    OrderBookLevel,
    OrderBookSnapshot,
    RiskAssessment,
    ShadowFill,
    ShadowOrder,
    ShadowPosition,
    SpotTicker,
    Trade,
    TradeCandidate,
)
from argus.core.enums import (
    DataRepresentation,
    MarketRegimeKind,
    MarketType,
    OrderSide,
    RiskDecision,
    ShadowPositionStatus,
    Timeframe,
)
from argus.core.schemas import DataStatus, Provenance
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)


def _meta(*, market_type: MarketType = MarketType.SPOT) -> dict[str, object]:
    return {
        "exchange": "binance",
        "market_type": market_type,
        "symbol": "BTCUSDT",
        "event_timestamp": NOW,
        "ingestion_timestamp": NOW + timedelta(milliseconds=20),
        "source": "binance:public",
        "confidence": 0.99,
        "sequence": 42,
        "representation": DataRepresentation.TRANSFORMED,
    }


def test_spot_and_futures_ticker_validate_market_and_utc() -> None:
    local_time = datetime(2026, 9, 2, 12, 0, tzinfo=timezone(timedelta(hours=3)))
    spot = SpotTicker(
        **{**_meta(), "event_timestamp": local_time},
        last_price=Decimal("60000.1"),
        bid=Decimal("60000"),
        ask=Decimal("60000.2"),
    )
    assert spot.event_timestamp.tzinfo is UTC
    assert spot.market_type is MarketType.SPOT

    futures = FuturesTicker(
        **_meta(market_type=MarketType.PERPETUAL),
        last_price=Decimal("60010"),
        mark_price=Decimal("60008"),
        funding_rate=Decimal("0.0001"),
    )
    assert futures.market_type is MarketType.PERPETUAL

    with pytest.raises(ValidationError):
        SpotTicker(
            **{**_meta(), "event_timestamp": datetime(2026, 9, 2, 9, 0)},
            last_price=Decimal("1"),
        )
    with pytest.raises(ValidationError):
        SpotTicker(**_meta(), last_price=Decimal("1"), bid=Decimal("2"), ask=Decimal("1"))


def test_trade_orderbook_and_derivative_contracts_are_decimal_and_explicit() -> None:
    trade = Trade(
        **_meta(), trade_id="t-1", side=OrderSide.BUY, price=Decimal("10"), quantity=Decimal("2")
    )
    assert trade.notional == Decimal("20")

    book = OrderBookSnapshot(
        **_meta(),
        bids=(OrderBookLevel(price=Decimal("101"), quantity=Decimal("1")),),
        asks=(OrderBookLevel(price=Decimal("100"), quantity=Decimal("1")),),
    )
    assert book.crossed is True  # the Data Quality Gate gives the final verdict later

    delta = OrderBookDelta(
        **_meta(),
        first_sequence=43,
        final_sequence=45,
        previous_final_sequence=42,
        bids=(OrderBookLevel(price=Decimal("99"), quantity=Decimal("0")),),
    )
    assert delta.final_sequence == 45
    with pytest.raises(ValidationError):
        OrderBookDelta(**_meta(), first_sequence=9, final_sequence=8)

    perpetual = _meta(market_type=MarketType.PERPETUAL)
    funding = FundingRate(**perpetual, rate=Decimal("-0.0002"))
    oi = OpenInterest(**perpetual, contracts=Decimal("1200"))
    basis = BasisSnapshot(
        **perpetual, spot_price=Decimal("60000"), perpetual_price=Decimal("60060")
    )
    ratio = LongShortRatio(**perpetual, long_ratio=Decimal("0.55"), short_ratio=Decimal("0.45"))
    liquidation = LiquidationEvent(
        **perpetual,
        liquidation_id="liq-1",
        side=OrderSide.SELL,
        price=Decimal("59000"),
        quantity=Decimal("0.5"),
    )
    assert funding.rate < 0 and oi.contracts == Decimal("1200")
    assert basis.basis == Decimal("60") and basis.basis_pct == Decimal("0.100")
    assert ratio.long_ratio > ratio.short_ratio
    assert liquidation.notional == Decimal("29500.0")


def test_feature_regime_context_and_safe_trade_boundary() -> None:
    features = FeatureSnapshot(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        as_of=NOW,
        values={"rsi": Decimal("55.2"), "funding": None},
        missing_data=("funding",),
        data_status=DataStatus.PENDING,
    )
    regime = MarketRegime(
        symbol="BTCUSDT",
        as_of=NOW,
        regime=MarketRegimeKind.UNKNOWN,
        confidence_pct=0,
        missing_data=("order_book",),
        data_status=DataStatus.PENDING,
    )
    context = CryptoAnalysisContext(
        symbol="BTCUSDT",
        as_of=NOW,
        features=features,
        regime=regime,
        missing_data=("spot", "futures"),
    )
    assert context.data_status is DataStatus.PENDING

    candidate = TradeCandidate(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        horizon=Timeframe.H1,
        entry_reference=Decimal("60000"),
        invalidation="1h close below 59000",
        evidence_source_ids=("feature:ema", "derivatives:oi"),
        confidence_pct=70,
    )
    assert candidate.execution_enabled is False
    assert candidate.human_approval_required is True
    assert candidate.leverage == 0
    with pytest.raises(ValidationError):
        TradeCandidate(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            horizon=Timeframe.H1,
            entry_reference=Decimal("60000"),
            invalidation="x",
            evidence_source_ids=("x",),
            confidence_pct=70,
            leverage=Decimal("2"),
        )

    risk = RiskAssessment(candidate_id=candidate.candidate_id)
    assert risk.decision is RiskDecision.REJECT
    assert risk.human_review_required is True


def test_shadow_contracts_model_partial_fill_and_closed_state() -> None:
    order = ShadowOrder(
        candidate_id="c1",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0.4"),
    )
    fill = ShadowFill(
        order_id=order.order_id,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        price=Decimal("60001"),
        quantity=Decimal("0.4"),
        commission=Decimal("2.4"),
    )
    position = ShadowPosition(
        candidate_id="c1",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.4"),
        average_entry_price=fill.price,
        mark_price=Decimal("60100"),
    )
    assert order.filled_quantity < order.quantity
    assert position.status is ShadowPositionStatus.OPEN
    with pytest.raises(ValidationError):
        ShadowPosition.model_validate(
            {**position.model_dump(), "status": ShadowPositionStatus.CLOSED}
        )


def test_ohlcv_series_crypto_metadata_is_backward_compatible() -> None:
    bar = OHLCVBar(timestamp=NOW, open=1, high=2, low=1, close=2, volume=3)
    provenance = Provenance(source="legacy", timestamp=NOW, confidence=0.9)
    legacy = OHLCVSeries(
        symbol="BTCUSDT", timeframe=Timeframe.H1, bars=(bar,), provenance=provenance
    )
    assert legacy.exchange is None and legacy.market_type is None
    assert legacy.event_timestamp == NOW

    crypto = legacy.model_copy(
        update={
            "exchange": "binance",
            "market_type": MarketType.SPOT,
            "sequence_start": 1,
            "sequence_end": 2,
        }
    )
    assert crypto.market_type is MarketType.SPOT
    with pytest.raises(ValidationError):
        OHLCVSeries(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            bars=(bar,),
            provenance=provenance,
            sequence_start=2,
            sequence_end=1,
        )


def test_sqlite_crypto_event_is_idempotent_and_migration_preserves_data(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE legacy_keep (value TEXT NOT NULL)")
    raw.execute("INSERT INTO legacy_keep VALUES ('keep-me')")
    raw.commit()
    raw.close()

    ticker = SpotTicker(**_meta(), last_price=Decimal("60000"))
    store = SqliteStore(db)
    assert store.save_crypto_record(ticker) is True
    assert store.save_crypto_record(ticker) is False
    assert store.crypto_event_count(record_type="SpotTicker") == 1
    store.close()

    check = sqlite3.connect(db)
    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "crypto_event",
        "trade_candidate",
        "risk_assessment",
        "shadow_order",
        "shadow_fill",
        "shadow_position",
        "crypto_artifact",
        "schema_migration",
        "stream_rollup_cursor",
        "trade_rollup_1m",
        "book_metric_rollup_1m",
    } <= tables
    assert check.execute("SELECT value FROM legacy_keep").fetchone()[0] == "keep-me"
    check.close()


def test_sqlite_crypto_stream_batch_is_visible_after_explicit_commit(tmp_path: Path) -> None:
    db = tmp_path / "batch.db"
    writer = SqliteStore(db)
    reader = SqliteStore(db)
    try:
        ticker = SpotTicker(**_meta(), last_price=Decimal("60000"))
        assert writer.save_crypto_record(ticker, commit=False)
        assert reader.crypto_event_count() == 0
        writer.commit()
        assert reader.crypto_event_count() == 1
    finally:
        reader.close()
        writer.close()


def test_current_schema_opens_read_only_while_stream_transaction_is_active(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    writer = SqliteStore(db)
    ticker = SpotTicker(**_meta(), last_price=Decimal("60000"))
    assert writer.save_crypto_record(ticker, commit=False)
    try:
        reader = SqliteStore(db)
        try:
            assert reader.ping()
            assert reader.crypto_event_count() == 0
        finally:
            reader.close()
    finally:
        writer.commit()
        writer.close()


def test_concurrent_fresh_database_migration_is_safe(tmp_path: Path) -> None:
    db = tmp_path / "startup.db"

    def open_and_close(_: int) -> bool:
        store = SqliteStore(db)
        try:
            return store.ping()
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(open_and_close, range(4))) == [True] * 4

    check = sqlite3.connect(db)
    try:
        assert (
            check.execute("SELECT COUNT(*) FROM schema_migration WHERE version=6").fetchone()[0]
            == 1
        )
    finally:
        check.close()


def test_single_store_serializes_concurrent_fastapi_style_reads(tmp_path: Path) -> None:
    """A single lifespan connection must not cause cursor/API misuse in the threadpool."""

    db = tmp_path / "threaded-api.db"
    store = SqliteStore(db)
    ticker = SpotTicker(**_meta(), last_price=Decimal("60000"))
    assert store.save_crypto_record(ticker)

    def read_many(index: int) -> tuple[int, Decimal | None]:
        for _ in range(40):
            assert store.ping()
            assert store.crypto_event_count() == 1
            latest = store.latest_crypto_record("BTCUSDT", "SpotTicker")
            assert isinstance(latest, SpotTicker)
        return index, latest.last_price if isinstance(latest, SpotTicker) else None

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(read_many, range(64)))
        assert len(results) == 64
        assert {price for _, price in results} == {Decimal("60000")}
    finally:
        store.close()


def test_sqlite_crypto_read_models_and_artifacts_are_point_in_time() -> None:
    store = SqliteStore()
    try:
        older = SpotTicker(
            **{**_meta(), "event_timestamp": NOW - timedelta(minutes=1)},
            last_price=Decimal("59900"),
        )
        newer = SpotTicker(**_meta(), last_price=Decimal("60000"))
        store.save_crypto_record(older)
        store.save_crypto_record(newer)

        latest = store.latest_crypto_record("BTCUSDT", "SpotTicker")
        assert isinstance(latest, SpotTicker)
        assert latest.last_price == Decimal("60000")
        assert store.recent_crypto_records("BTCUSDT", "SpotTicker", limit=1) == [newer]

        assert store.save_crypto_artifact(
            artifact_type="features",
            symbol="BTCUSDT",
            as_of=NOW,
            data_status=DataStatus.OK,
            payload={"rsi": "55.2", "funding": None},
        )
        assert not store.save_crypto_artifact(
            artifact_type="features",
            symbol="BTCUSDT",
            as_of=NOW,
            data_status=DataStatus.OK,
            payload={"rsi": "99"},
        )
        artifact = store.latest_crypto_artifact("BTCUSDT", "features")
        assert artifact is not None
        assert artifact[0] == NOW
        assert artifact[1] is DataStatus.OK
        assert artifact[2] == {"funding": None, "rsi": "55.2"}

        with pytest.raises(ValueError, match="unknown crypto record type: MadeUpPrice"):
            store.latest_crypto_record("BTCUSDT", "MadeUpPrice")
    finally:
        store.close()
