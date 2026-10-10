"""News intelligence runtime: public-only, PIT-writing, with isolated failures."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from argus.config.models import RuntimeConfig
from argus.core.intelligence import (
    NewsCoverageStatus,
    NewsEventType,
    NewsIngestionBatch,
    RawNewsDocument,
)
from argus.core.schemas import DataStatus, utcnow
from argus.ingestion.news_intelligence import RssAtomProvider, RssFeedSource
from argus.platform.intelligence_pipeline import IntelligenceNormalizer
from argus.platform.news_intelligence import NewsIntelligenceEngine, NewsIntelligencePolicy
from argus.platform.source_registry import SourceRegistry
from argus.storage.sqlite import SqliteStore


class NewsIntelligenceCycle:
    def __init__(
        self,
        store: SqliteStore,
        config_provider: Callable[[], RuntimeConfig],
        *,
        owns_store: bool = False,
    ) -> None:
        self.store = store
        self.config_provider = config_provider
        self.owns_store = owns_store
        self._provider_signature: str | None = None
        self._providers: tuple[RssAtomProvider, ...] = ()

    def run_once(self) -> NewsIngestionBatch:
        started = time.perf_counter()
        config = self.config_provider()
        news = config.news_intelligence
        at = utcnow()
        snapshots = self.store.latest_universe_snapshots()
        symbols = tuple(row.symbol for row in snapshots if row.eligible) or tuple(
            config.universe.symbols
        )
        if not news.enabled:
            self.store.save_source_registry(
                SourceRegistry(news).entries(watchlist_symbols=symbols), observed_at=at
            )
            batch = NewsIngestionBatch(
                collected_at=at,
                coverage_status=NewsCoverageStatus.PROVIDER_UNAVAILABLE,
                data_status=DataStatus.PENDING,
            )
            self._checkpoint(
                batch,
                configured_feeds=0,
                inserted_records=0,
                reason="news intelligence disabled",
                processing_latency_ms=(time.perf_counter() - started) * 1000,
            )
            return batch
        providers = self._providers_for(config)
        configured_feeds = sum(len(provider.feeds) for provider in providers)
        engine = NewsIntelligenceEngine(
            providers,
            policy=NewsIntelligencePolicy(
                maximum_age=timedelta(minutes=news.maximum_age_minutes),
                future_tolerance=timedelta(seconds=news.future_tolerance_seconds),
                default_ttl=timedelta(minutes=news.default_ttl_minutes),
                minimum_reliability=Decimal(str(news.minimum_reliability)),
                verified_reliability=Decimal(str(news.verified_reliability)),
            ),
        )
        batch = engine.collect(universe_symbols=symbols, now=at)
        registry = SourceRegistry(news)
        registry.update_health(batch.provider_health)
        registry_entries = registry.entries(watchlist_symbols=symbols)
        self.store.save_source_registry(registry_entries, observed_at=at)
        entry_by_id = {entry.source_id: entry for entry in registry_entries}
        normalizer = IntelligenceNormalizer()
        documents = tuple(
            normalizer.raw_document(item, first_seen_at=at) for item in batch.raw_items
        )
        self.store.save_raw_news_documents(documents)
        documents_by_group: dict[str, list[RawNewsDocument]] = {}
        for item, document in zip(batch.raw_items, documents, strict=True):
            documents_by_group.setdefault(engine.duplicate_key(item), []).append(document)
        normalized_events = []
        for record in batch.records:
            group_documents = documents_by_group.get(record.duplicate_group, [])
            if not group_documents:
                continue
            supersedes_event_id = None
            if record.event_type is NewsEventType.CORRECTION:
                for symbol in record.related_symbols:
                    prior = next(
                        (
                            candidate
                            for candidate in self.store.latest_normalized_events(
                                symbol=symbol,
                                as_of=record.first_seen_at,
                                limit=25,
                            )
                            if candidate.event_type is not NewsEventType.CORRECTION
                            and candidate.event_id != record.news_id
                        ),
                        None,
                    )
                    if prior is not None:
                        supersedes_event_id = prior.event_id
                        break
            event = normalizer.normalized_event(
                record,
                group_documents,
                entry_by_id,
                supersedes_event_id=supersedes_event_id,
            )
            self.store.save_normalized_event(event)
            normalized_events.append(event)
        inserted_records = self.store.save_crypto_news(batch.records)
        self._checkpoint(
            batch,
            configured_feeds=configured_feeds,
            inserted_records=inserted_records,
            reason=("RSS/Atom batch processed" if configured_feeds else "no news feed configured"),
            normalized_events=len(normalized_events),
            critical_sources_healthy=sum(
                entry.enabled
                and entry.trust_level.value == "official"
                and entry.health_status.value == "healthy"
                for entry in registry_entries
            ),
            processing_latency_ms=(time.perf_counter() - started) * 1000,
        )
        return batch

    def _providers_for(self, config: RuntimeConfig) -> tuple[RssAtomProvider, ...]:
        news = config.news_intelligence
        signature = json.dumps(
            [feed.model_dump(mode="json") for feed in news.feeds if feed.enabled],
            sort_keys=True,
        )
        if signature == self._provider_signature:
            return self._providers
        providers: list[RssAtomProvider] = []
        for feed in news.feeds:
            if not feed.enabled:
                continue
            assert feed.feed_url is not None
            assert feed.name is not None
            assert feed.provider_id is not None
            source = RssFeedSource(
                url=feed.feed_url,
                source_name=feed.name,
                reliability=Decimal(str(feed.reliability)),
                provider_id=feed.provider_id,
                source_type=feed.source_type,
                reliability_tier=feed.reliability_tier,
                language=feed.language,
                official=feed.official,
                official_symbols=tuple(feed.official_symbols),
                enabled=feed.enabled,
                timeout_seconds=feed.timeout_seconds or news.timeout_s,
                maximum_items=feed.maximum_items or news.maximum_items_per_feed,
                maximum_payload_bytes=(feed.maximum_payload_bytes or news.maximum_response_bytes),
                freshness_window_seconds=feed.freshness_window_seconds,
                allowed_event_types=tuple(feed.allowed_event_types),
                minimum_request_interval_seconds=feed.minimum_request_interval_seconds,
                maximum_redirects=feed.maximum_redirects,
                backoff_base_seconds=feed.backoff_base_seconds,
                backoff_max_seconds=feed.backoff_max_seconds,
            )
            providers.append(
                RssAtomProvider(
                    (source,),
                    timeout_s=news.timeout_s,
                    maximum_bytes=news.maximum_response_bytes,
                    maximum_items_per_feed=news.maximum_items_per_feed,
                )
            )
        self._provider_signature = signature
        self._providers = tuple(providers)
        return self._providers

    def _checkpoint(
        self,
        batch: NewsIngestionBatch,
        *,
        configured_feeds: int,
        inserted_records: int,
        reason: str,
        normalized_events: int = 0,
        critical_sources_healthy: int = 0,
        processing_latency_ms: float | None = None,
    ) -> None:
        self.store.set_checkpoint(
            "news_intelligence:health",
            json.dumps(
                {
                    "checked_at": batch.collected_at.isoformat(),
                    "data_status": batch.data_status.value,
                    "configured_feeds": configured_feeds,
                    "records": len(batch.records),
                    "downloaded_items": sum(item.items_received for item in batch.provider_health),
                    "new_records": inserted_records,
                    "normalized_events": normalized_events,
                    "critical_sources_healthy": critical_sources_healthy,
                    "processing_latency_ms": processing_latency_ms,
                    "duplicates": batch.duplicate_count + len(batch.records) - inserted_records,
                    "batch_duplicates": batch.duplicate_count,
                    "storage_duplicates": len(batch.records) - inserted_records,
                    "rejected_records": sum(
                        item.data_status is DataStatus.REJECTED for item in batch.records
                    ),
                    "ambiguous_entity_matches": sum(
                        match.ambiguous for item in batch.records for match in item.entity_matches
                    ),
                    "provider_errors": list(batch.provider_errors),
                    "coverage_status": batch.coverage_status.value,
                    "coverage_by_symbol": {
                        symbol: status.value
                        for symbol, status in sorted(batch.coverage_by_symbol.items())
                    },
                    "provider_health": [
                        item.model_dump(mode="json") for item in batch.provider_health
                    ],
                    "reason": reason,
                },
                sort_keys=True,
            ),
        )

    def close(self) -> None:
        if self.owns_store:
            self.store.close()

    def diagnostic_state(self) -> dict[str, int]:
        """Bounded provider state sizes without enabling expensive heap profiling."""

        return {
            "provider_count": len(self._providers),
            "news_cache_entries": sum(provider.cache_entries for provider in self._providers),
            "news_cache_capacity": sum(provider.cache_capacity for provider in self._providers),
        }


__all__ = ["NewsIntelligenceCycle"]
