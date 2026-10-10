"""Source Registry → raw document → event → API/webhook acceptance tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.config.models import (
    NewsFeedConfig,
    NewsIntelligenceConfig,
    RetentionConfig,
    RuntimeConfig,
)
from argus.core.intelligence import (
    ExternalTechnicalSignal,
    NewsReliabilityTier,
    NewsSourceType,
    ProviderNewsItem,
    SourceAvailability,
    SourceGroup,
)
from argus.platform.intelligence_pipeline import IntelligenceNormalizer
from argus.platform.news_intelligence import NewsIntelligenceEngine
from argus.platform.source_registry import SourceRegistry
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _feed(**updates: object) -> NewsFeedConfig:
    values: dict[str, object] = {
        "provider_id": "ethereum",
        "name": "Example Project Blog",
        "feed_url": "https://blog.example.org/feed.xml",
        "official_domain": "blog.example.org",
        "source_group": SourceGroup.OFFICIAL_PROJECT,
        "source_type": NewsSourceType.ATOM,
        "availability": SourceAvailability.AVAILABLE,
        "reliability_tier": NewsReliabilityTier.OFFICIAL,
        "reliability": 0.98,
        "official": True,
        "official_symbols": ["ETHUSDT"],
    }
    values.update(updates)
    return NewsFeedConfig.model_validate(values)


def _item(provider_id: str = "ethereum") -> ProviderNewsItem:
    return ProviderNewsItem(
        provider_item_id="release-1",
        provider_id=provider_id,
        canonical_url="https://blog.example.org/2026/09/upgrade?utm_source=test",
        provider=provider_id,
        source_name="Example Project Blog",
        source_reliability=Decimal("0.98"),
        title="Ethereum network upgrade released",
        short_summary="Ethereum mainnet upgrade is available",
        published_at=NOW - timedelta(minutes=2),
        official=True,
        official_symbols=("ETHUSDT",),
    )


def _persist_event(store: SqliteStore) -> str:
    feed = _feed()
    entries = SourceRegistry(NewsIntelligenceConfig(feeds=[feed])).entries(
        watchlist_symbols=("ETHUSDT",)
    )
    store.save_source_registry(entries, observed_at=NOW)
    item = _item()
    engine = NewsIntelligenceEngine(())
    record = engine._record(
        (item,),
        duplicate_group=engine.duplicate_key(item),
        allowed_symbols={"ETHUSDT"},
        now=NOW,
    )
    document = IntelligenceNormalizer().raw_document(item, first_seen_at=NOW)
    store.save_raw_news_documents((document,))
    event = IntelligenceNormalizer().normalized_event(
        record, (document,), {entries[0].source_id: entries[0]}
    )
    assert store.save_normalized_event(event)
    assert not store.save_normalized_event(event)
    return event.event_id


def test_registry_keeps_unavailable_source_visible_and_scopes_project_feed() -> None:
    unavailable = _feed(
        provider_id="example-exchange",
        name="Example Exchange Announcements",
        feed_url=None,
        url=None,
        official_domain="exchange.example.com",
        source_group=SourceGroup.EXCHANGE_ANNOUNCEMENT,
        availability=SourceAvailability.MANUAL_CONFIGURATION_REQUIRED,
        enabled=False,
    )
    registry = SourceRegistry(NewsIntelligenceConfig(feeds=[_feed(), unavailable]))
    entries = registry.entries(watchlist_symbols=("BTCUSDT",))
    ethereum = next(item for item in entries if item.source_id == "ethereum")
    exchange = next(item for item in entries if item.source_id == "example-exchange")
    assert not ethereum.enabled
    assert ethereum.quarantine_reason == "outside the watchlist scope"
    assert not exchange.enabled
    assert exchange.health_status.value == "not_configured"
    assert exchange.feed_url is None


def test_raw_document_event_and_storage_are_pit_and_idempotent() -> None:
    store = SqliteStore()
    try:
        event_id = _persist_event(store)
        event = store.normalized_event(event_id)
        assert event is not None
        assert event.first_seen_at == NOW
        assert event.affected_symbols == ("ETHUSDT",)
        assert event.evidence[0].evidence_role == "primary"
        assert event.data_status.value == "ok"
        assert len(store.latest_raw_news_documents()) == 1
        assert store.latest_normalized_events(symbol="ETHUSDT", as_of=NOW)
        assert store.latest_normalized_events(as_of=NOW - timedelta(seconds=1)) == []
    finally:
        store.close()


def test_event_evidence_bounds_long_feed_summary() -> None:
    feed = _feed()
    entry = SourceRegistry(NewsIntelligenceConfig(feeds=[feed])).entries(
        watchlist_symbols=("ETHUSDT",)
    )[0]
    item = _item().model_copy(update={"short_summary": "x" * 750})
    engine = NewsIntelligenceEngine(())
    record = engine._record(
        (item,),
        duplicate_group=engine.duplicate_key(item),
        allowed_symbols={"ETHUSDT"},
        now=NOW,
    )
    document = IntelligenceNormalizer().raw_document(item, first_seen_at=NOW)
    event = IntelligenceNormalizer().normalized_event(record, (document,), {entry.source_id: entry})
    assert event.evidence[0].excerpt is not None
    assert len(event.evidence[0].excerpt) == 500


def test_identical_syndicated_content_is_not_independent_corroboration() -> None:
    first_feed = _feed(provider_id="publisher-a", official_domain="a.example")
    second_feed = _feed(
        provider_id="publisher-b",
        official_domain="b.example",
        feed_url="https://b.example/feed.xml",
    )
    entries = SourceRegistry(NewsIntelligenceConfig(feeds=[first_feed, second_feed])).entries(
        watchlist_symbols=("ETHUSDT",)
    )
    first_item = _item("publisher-a")
    second_item = _item("publisher-b").model_copy(
        update={"canonical_url": "https://b.example/syndicated-release"}
    )
    engine = NewsIntelligenceEngine(())
    record = engine._record(
        (first_item, second_item),
        duplicate_group=engine.duplicate_key(first_item),
        allowed_symbols={"ETHUSDT"},
        now=NOW,
    )
    normalizer = IntelligenceNormalizer()
    documents = tuple(
        normalizer.raw_document(item, first_seen_at=NOW) for item in (first_item, second_item)
    )
    event = normalizer.normalized_event(
        record,
        documents,
        {entry.source_id: entry for entry in entries},
    )
    assert len(event.evidence) == 2
    assert event.corroboration_count == 0


def test_news_retention_deletes_only_unreferenced_old_documents() -> None:
    store = SqliteStore()
    try:
        _persist_event(store)
        orphan_item = _item("orphan").model_copy(
            update={
                "provider_item_id": "orphan-1",
                "canonical_url": "https://example.com/orphan",
            }
        )
        old = NOW - timedelta(days=3)
        orphan = IntelligenceNormalizer().raw_document(orphan_item, first_seen_at=old)
        store.save_raw_news_documents((orphan,))
        report = store.run_news_retention(
            RetentionConfig(enabled=True, raw_news_document_hours=24), now=NOW
        )
        assert report["deleted"] == 1
        assert len(store.latest_raw_news_documents()) == 1
    finally:
        store.close()


def test_intelligence_schema_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "argus-migration.db"
    first = SqliteStore(path)
    first.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM schema_migration WHERE version=8")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (7,?)",
            (NOW.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()

    for _attempt in range(2):
        migrated = SqliteStore(path)
        assert migrated.ping()
        migrated.close()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migration WHERE version=8"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='normalized_crypto_events'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_intelligence_api_is_bounded_pit_and_webhook_is_not_an_order() -> None:
    store = SqliteStore()
    config = RuntimeConfig(
        news_intelligence=NewsIntelligenceConfig(
            feeds=[_feed()],
            tradingview_enabled=True,
            tradingview_ttl_seconds=120,
            tradingview_rate_limit_per_minute=5,
        )
    )
    try:
        event_id = _persist_event(store)
        app = create_app(
            journal_provider=lambda: store,
            intelligence_store_provider=lambda: store,
            config_reader=lambda: config,
            tradingview_webhook_secret="test-secret-value",
            secure_cookie=False,
        )
        client = TestClient(app)
        sources = client.get("/api/intelligence/sources?limit=1")
        assert sources.status_code == 200
        assert sources.json()["returned_count"] == 1
        assert client.get(f"/api/intelligence/events/{event_id}").status_code == 200
        assert (
            client.get(
                "/api/intelligence/events",
                params={"as_of": (NOW - timedelta(seconds=1)).isoformat()},
            ).json()["data_status"]
            == "no_data"
        )

        payload = {
            "schema_version": "tradingview-signal-v1",
            "symbol": "ETHUSDT",
            "signal_type": "breakout_watch",
            "timeframe": "15m",
            "direction": "bullish",
            "timestamp": NOW.isoformat(),
            "nonce": "nonce-123456",
            "idempotency_key": "delivery-enabled",
            "attributes": {"condition": "close_above_resistance"},
        }
        # The endpoint checks against the real clock, so move the test timestamp to now.
        payload["timestamp"] = datetime.now(UTC).isoformat()
        assert (
            client.post("/api/intelligence/webhooks/tradingview", json=payload).status_code == 401
        )
        accepted = client.post(
            "/api/intelligence/webhooks/tradingview",
            json=payload,
            headers={"X-Argus-Webhook-Token": "test-secret-value"},
        )
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["submission_authorized"] is False
        assert "test-secret-value" not in accepted.text
        replay = client.post(
            "/api/intelligence/webhooks/tradingview",
            json=payload,
            headers={"X-Argus-Webhook-Token": "test-secret-value"},
        )
        assert replay.status_code == 409
        signals = store.latest_external_technical_signals()
        assert len(signals) == 1
        assert isinstance(signals[0], ExternalTechnicalSignal)
        assert signals[0].data_status.value == "quarantined"
        assert store.order_intents("PUBLIC") == []
    finally:
        store.close()


def test_tradingview_disabled_returns_404_even_with_secret() -> None:
    store = SqliteStore()
    try:
        app = create_app(
            journal_provider=lambda: store,
            intelligence_store_provider=lambda: store,
            config_reader=RuntimeConfig,
            tradingview_webhook_secret="unused-secret",
            secure_cookie=False,
        )
        response = TestClient(app).post(
            "/api/intelligence/webhooks/tradingview",
            json={
                "symbol": "BTCUSDT",
                "signal_type": "watch",
                "timeframe": "1m",
                "direction": "neutral",
                "timestamp": datetime.now(UTC).isoformat(),
                "nonce": "nonce-disabled",
                "idempotency_key": "delivery-disabled",
            },
            headers={"X-Argus-Webhook-Token": "unused-secret"},
        )
        assert response.status_code == 404
    finally:
        store.close()
