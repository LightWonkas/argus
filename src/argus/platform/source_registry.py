"""Central, config-driven news source registry.

The registry collects no data. It separates the source definition from runtime health and
produces a uniform view for the UI/API. Sources whose URL is unverified stay disabled with
an explicit availability state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from argus.config.models import NewsFeedConfig, NewsIntelligenceConfig
from argus.core.intelligence import (
    NewsProviderHealth,
    NewsProviderHealthStatus,
    SourceAvailability,
    SourceRegistryEntry,
    SourceRetryPolicy,
)


class SourceRegistry:
    def __init__(self, config: NewsIntelligenceConfig) -> None:
        self._config = config
        self._health: dict[str, NewsProviderHealth] = {}

    def update_health(self, snapshots: Iterable[NewsProviderHealth]) -> None:
        for snapshot in snapshots:
            self._health[snapshot.provider_id] = snapshot

    def entries(self, *, watchlist_symbols: Sequence[str] = ()) -> tuple[SourceRegistryEntry, ...]:
        watchlist = {symbol.strip().upper() for symbol in watchlist_symbols if symbol.strip()}
        return tuple(
            self._entry(feed, watchlist)
            for feed in sorted(
                self._config.feeds, key=lambda item: (item.priority, item.provider_id or "")
            )
        )

    def _entry(self, feed: NewsFeedConfig, watchlist: set[str]) -> SourceRegistryEntry:
        snapshot = self._health.get(feed.provider_id or "")
        scope = tuple(feed.official_symbols)
        # Project feeds are scoped to the current universe/watchlist only. The source record
        # is not dropped; when out of scope it goes passive and the reason shows in the API.
        scoped_out = bool(scope and watchlist and not watchlist.intersection(scope))
        enabled = feed.enabled and not scoped_out
        health = (
            snapshot.health_status
            if snapshot is not None
            else (
                NewsProviderHealthStatus.NOT_CONFIGURED
                if feed.availability is SourceAvailability.MANUAL_CONFIGURATION_REQUIRED
                else (
                    NewsProviderHealthStatus.UNAVAILABLE
                    if feed.availability is SourceAvailability.UNAVAILABLE
                    else NewsProviderHealthStatus.STALE
                )
            )
        )
        quarantine_reason = "outside the watchlist scope" if scoped_out else None
        if snapshot is not None and snapshot.consecutive_failures >= feed.retry_max_attempts:
            health = NewsProviderHealthStatus.QUARANTINED
            quarantine_reason = "consecutive fetch/parse failure threshold exceeded"
        return SourceRegistryEntry(
            source_id=feed.provider_id or "unknown",
            display_name=feed.name or "Unknown",
            source_group=feed.source_group,
            official_domain=feed.official_domain or "invalid.local",
            feed_type=feed.source_type,
            feed_url=feed.feed_url,
            enabled=enabled,
            availability=feed.availability,
            priority=feed.priority,
            trust_level=feed.reliability_tier,
            supported_event_types=tuple(feed.allowed_event_types),
            symbol_scope=scope,
            polling_interval_seconds=max(
                30, int(feed.minimum_request_interval_seconds or self._config.poll_interval_s)
            ),
            request_timeout_seconds=feed.timeout_seconds or self._config.timeout_s,
            freshness_threshold_seconds=feed.freshness_window_seconds,
            rate_limit_per_minute=max(
                1,
                int(60 / max(feed.minimum_request_interval_seconds, 0.1)),
            ),
            retry_policy=SourceRetryPolicy(
                maximum_attempts=feed.retry_max_attempts,
                base_delay_seconds=feed.backoff_base_seconds,
                maximum_delay_seconds=feed.backoff_max_seconds,
            ),
            parser_version=feed.parser_version,
            terms_or_license_note=feed.terms_or_license_note,
            last_success_at=snapshot.last_success_at if snapshot else None,
            last_failure_at=(
                snapshot.last_checked_at if snapshot and snapshot.error_type is not None else None
            ),
            consecutive_failures=snapshot.consecutive_failures if snapshot else 0,
            health_status=health,
            success_count=1 if snapshot and snapshot.last_success_at else 0,
            failure_count=snapshot.consecutive_failures if snapshot else 0,
            parse_failure_count=(
                1
                if snapshot and snapshot.error_type and "parse" in snapshot.error_type.casefold()
                else 0
            ),
            fetch_latency_ms=snapshot.latency_ms if snapshot else None,
            quarantine_reason=quarantine_reason,
        )


__all__ = ["SourceRegistry"]
