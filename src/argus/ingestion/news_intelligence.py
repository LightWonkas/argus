"""Terms-of-use-friendly RSS/Atom news ingestion contracts.

This module does not scrape web pages. It reads only explicitly configured RSS/Atom
URLs, with a bounded response size and timeout. A missing API-key provider is normal
``NO_DATA`` behavior.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field, field_validator

from argus.core.intelligence import (
    NewsEventType,
    NewsProviderHealth,
    NewsProviderHealthStatus,
    NewsReliabilityTier,
    NewsSourceType,
    ProviderNewsItem,
)
from argus.core.schemas import utcnow

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be tz-aware")
    return value.astimezone(UTC)


class RssFeedSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(..., min_length=1)
    source_name: str = Field(..., min_length=1)
    reliability: Decimal = Field(..., ge=0, le=1)
    provider_id: str = Field(default="rss_atom", min_length=1)
    source_type: NewsSourceType = NewsSourceType.RSS
    reliability_tier: NewsReliabilityTier = NewsReliabilityTier.GENERAL
    language: str = Field(default="und", min_length=2, max_length=16)
    official: bool = False
    official_symbols: tuple[str, ...] = ()
    enabled: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    maximum_items: int = Field(default=100, gt=0, le=500)
    maximum_payload_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    freshness_window_seconds: int = Field(default=86_400, gt=0, le=2_592_000)
    allowed_event_types: tuple[NewsEventType, ...] = ()
    minimum_request_interval_seconds: float = Field(default=30.0, ge=0, le=86_400)
    maximum_redirects: int = Field(default=3, ge=0, le=10)
    backoff_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    backoff_max_seconds: float = Field(default=300.0, gt=0, le=86_400)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("RSS/Atom source must be an http/https URL")
        return value.strip()


@runtime_checkable
class NewsProvider(Protocol):
    name: str

    def fetch(self, *, now: datetime | None = None) -> Sequence[ProviderNewsItem]: ...


@runtime_checkable
class OfficialAnnouncementProvider(NewsProvider, Protocol):
    """Marker contract for official announcement API/feed providers."""


@runtime_checkable
class ConfiguredNewsApiProvider(NewsProvider, Protocol):
    """API contract usable only when the secret is configured on the backend."""

    configured: bool


@dataclass(frozen=True)
class NewsHttpResponse:
    status_code: int
    payload: bytes
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None


NewsTransport = Callable[[str, float, int], bytes | NewsHttpResponse]


@dataclass
class _FeedState:
    etag: str | None = None
    last_modified: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_item_published_at: datetime | None = None
    consecutive_failures: int = 0
    next_retry_at: datetime | None = None
    cached_items: tuple[ProviderNewsItem, ...] = ()


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, maximum_redirects: int) -> None:
        super().__init__()
        self.maximum_redirects = maximum_redirects
        self.redirects = 0

    def redirect_request(self, *args: object, **kwargs: object) -> urllib.request.Request | None:
        self.redirects += 1
        if self.redirects > self.maximum_redirects:
            raise RuntimeError("news feed redirect limit exceeded")
        return super().redirect_request(*args, **kwargs)  # type: ignore[arg-type]


def _stdlib_transport(
    url: str,
    timeout_s: float,
    maximum_bytes: int,
    conditional_headers: dict[str, str] | None = None,
    maximum_redirects: int = 3,
) -> NewsHttpResponse:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
            "User-Agent": "ArgusCryptoNews/0.1 (+public RSS/Atom reader)",
            **(conditional_headers or {}),
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_BoundedRedirectHandler(maximum_redirects))
    try:
        response_context = opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return NewsHttpResponse(
                status_code=304,
                payload=b"",
                content_type=exc.headers.get_content_type() if exc.headers else None,
                etag=exc.headers.get("ETag") if exc.headers else None,
                last_modified=exc.headers.get("Last-Modified") if exc.headers else None,
            )
        raise
    with response_context as response:
        content_type = response.headers.get_content_type()
        if content_type.lower() not in {
            "application/atom+xml",
            "application/rss+xml",
            "application/xml",
            "text/xml",
        }:
            raise ValueError("news feed Content-Type is not XML/RSS/Atom")
        payload = bytes(response.read(maximum_bytes + 1))
        result = NewsHttpResponse(
            status_code=int(response.status),
            payload=payload,
            content_type=content_type,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )
    if len(payload) > maximum_bytes:
        raise ValueError("news feed response exceeded the configured size limit")
    return result


class RssAtomProvider:
    """Normalize one or more configured RSS/Atom feeds."""

    name = "rss_atom"

    def __init__(
        self,
        feeds: Sequence[RssFeedSource],
        *,
        timeout_s: float = 10.0,
        maximum_bytes: int = 2_000_000,
        maximum_items_per_feed: int = 100,
        transport: NewsTransport | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("news timeout must be positive")
        if maximum_bytes < 1024:
            raise ValueError("news maximum_bytes must be at least 1024")
        if maximum_items_per_feed <= 0:
            raise ValueError("maximum_items_per_feed must be positive")
        self.feeds = tuple(feeds)
        self.timeout_s = timeout_s
        self.maximum_bytes = maximum_bytes
        self.maximum_items_per_feed = maximum_items_per_feed
        self._transport = transport or _stdlib_transport
        self.name = self.feeds[0].provider_id if len(self.feeds) == 1 else "rss_atom"
        self._state = {feed.provider_id: _FeedState() for feed in self.feeds}
        self._health: tuple[NewsProviderHealth, ...] = ()

    @property
    def health_snapshots(self) -> tuple[NewsProviderHealth, ...]:
        return self._health

    @property
    def cache_entries(self) -> int:
        """Current retained feed items; bounded by every feed's ``maximum_items``."""

        return sum(len(state.cached_items) for state in self._state.values())

    @property
    def cache_capacity(self) -> int:
        return sum(min(self.maximum_items_per_feed, feed.maximum_items) for feed in self.feeds)

    def fetch(self, *, now: datetime | None = None) -> Sequence[ProviderNewsItem]:
        observed_at = _aware(now or utcnow())
        records: list[ProviderNewsItem] = []
        errors: list[Exception] = []
        health: list[NewsProviderHealth] = []
        for feed in self.feeds:
            if not feed.enabled:
                continue
            state = self._state[feed.provider_id]
            if state.next_retry_at is not None and observed_at < state.next_retry_at:
                health.append(
                    self._health_snapshot(
                        feed,
                        state,
                        observed_at,
                        status=NewsProviderHealthStatus.BACKOFF,
                    )
                )
                continue
            if (
                state.last_attempt_at is not None
                and (observed_at - state.last_attempt_at).total_seconds()
                < feed.minimum_request_interval_seconds
            ):
                records.extend(state.cached_items)
                health.append(self._health_snapshot(feed, state, observed_at))
                continue
            state.last_attempt_at = observed_at
            started = datetime.now(tz=UTC)
            try:
                response = self._request(feed, state)
                latency_ms = max(
                    0.0,
                    (datetime.now(tz=UTC) - started).total_seconds() * 1000,
                )
                if response.status_code == 304:
                    items = state.cached_items
                else:
                    items = self.parse(response.payload, feed=feed, fetched_at=observed_at)
                    state.cached_items = items
                    state.last_item_published_at = max(
                        (item.published_at for item in items),
                        default=state.last_item_published_at,
                    )
                state.etag = response.etag or state.etag
                state.last_modified = response.last_modified or state.last_modified
                state.last_success_at = observed_at
                state.consecutive_failures = 0
                state.next_retry_at = None
                records.extend(items)
                health.append(
                    self._health_snapshot(
                        feed,
                        state,
                        observed_at,
                        response_status=response.status_code,
                        items_received=0 if response.status_code == 304 else len(items),
                        latency_ms=latency_ms,
                    )
                )
            except (
                Exception
            ) as exc:  # provider isolation; the batch engine moves on to the next source
                errors.append(exc)
                state.consecutive_failures += 1
                delay = min(
                    feed.backoff_base_seconds * (2 ** (state.consecutive_failures - 1)),
                    feed.backoff_max_seconds,
                )
                state.next_retry_at = observed_at + timedelta(seconds=delay)
                provider_status = (
                    NewsProviderHealthStatus.ERROR
                    if isinstance(exc, (ValueError, ET.ParseError))
                    else NewsProviderHealthStatus.UNAVAILABLE
                )
                health.append(
                    self._health_snapshot(
                        feed,
                        state,
                        observed_at,
                        status=provider_status,
                        error_type=type(exc).__name__,
                    )
                )
        self._health = tuple(health)
        if not records and errors:
            raise RuntimeError(f"all RSS/Atom sources failed ({len(errors)})") from errors[0]
        return tuple(records)

    def _request(self, feed: RssFeedSource, state: _FeedState) -> NewsHttpResponse:
        headers: dict[str, str] = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        timeout = feed.timeout_seconds or self.timeout_s
        maximum_bytes = feed.maximum_payload_bytes or self.maximum_bytes
        if self._transport is _stdlib_transport:
            return _stdlib_transport(
                feed.url,
                timeout,
                maximum_bytes,
                headers,
                feed.maximum_redirects,
            )
        payload = self._transport(feed.url, timeout, maximum_bytes)
        if isinstance(payload, NewsHttpResponse):
            return payload
        return NewsHttpResponse(
            status_code=200,
            payload=payload,
            content_type="application/xml",
        )

    @staticmethod
    def _health_snapshot(
        feed: RssFeedSource,
        state: _FeedState,
        observed_at: datetime,
        *,
        status: NewsProviderHealthStatus | None = None,
        response_status: int | None = None,
        items_received: int = 0,
        latency_ms: float | None = None,
        error_type: str | None = None,
    ) -> NewsProviderHealth:
        if status is None:
            latest = state.last_item_published_at
            stale = (
                latest is None
                or (observed_at - latest).total_seconds() > feed.freshness_window_seconds
            )
            status = NewsProviderHealthStatus.STALE if stale else NewsProviderHealthStatus.HEALTHY
        return NewsProviderHealth(
            provider_id=feed.provider_id,
            name=feed.source_name,
            feed_url=feed.url,
            source_type=feed.source_type,
            reliability_tier=feed.reliability_tier,
            enabled=feed.enabled,
            timeout_seconds=feed.timeout_seconds,
            maximum_items=feed.maximum_items,
            maximum_payload_bytes=feed.maximum_payload_bytes,
            freshness_window_seconds=feed.freshness_window_seconds,
            allowed_event_types=feed.allowed_event_types,
            last_checked_at=observed_at,
            last_success_at=state.last_success_at,
            last_item_published_at=state.last_item_published_at,
            consecutive_failures=state.consecutive_failures,
            health_status=status,
            response_status=response_status,
            items_received=items_received,
            latency_ms=latency_ms,
            etag_present=state.etag is not None,
            last_modified_present=state.last_modified is not None,
            error_type=error_type,
        )

    def parse(
        self,
        payload: bytes,
        *,
        feed: RssFeedSource,
        fetched_at: datetime | None = None,
    ) -> tuple[ProviderNewsItem, ...]:
        if len(payload) > self.maximum_bytes:
            raise ValueError("news feed content exceeded the size limit")
        upper_prefix = payload[:4096].upper()
        if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
            raise ValueError("XML feed containing DTD/entity was rejected")
        root = ET.fromstring(payload)
        at = _aware(fetched_at or utcnow())
        local = self._local_name(root.tag)
        if local == "rss" or self._first_child(root, "channel") is not None:
            nodes = [node for node in root.iter() if self._local_name(node.tag) == "item"]
        elif local == "feed":
            nodes = [node for node in root if self._local_name(node.tag) == "entry"]
        else:
            raise ValueError("unsupported XML news feed format")
        result: list[ProviderNewsItem] = []
        limit = min(self.maximum_items_per_feed, feed.maximum_items)
        for node in nodes[:limit]:
            item = self._parse_node(node, feed=feed, fetched_at=at)
            if item is not None:
                result.append(item)
        return tuple(result)

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    @classmethod
    def _first_child(cls, node: ET.Element, name: str) -> ET.Element | None:
        return next((child for child in node if cls._local_name(child.tag) == name), None)

    @classmethod
    def _text(cls, node: ET.Element, *names: str) -> str | None:
        wanted = set(names)
        for child in node:
            if cls._local_name(child.tag) in wanted and child.text:
                return child.text.strip()
        return None

    @classmethod
    def _link(cls, node: ET.Element, feed_url: str) -> str | None:
        for child in node:
            if cls._local_name(child.tag) != "link":
                continue
            href = child.attrib.get("href")
            if href:
                rel = child.attrib.get("rel", "alternate")
                if rel == "alternate":
                    return urljoin(feed_url, href.strip())
            if child.text:
                return urljoin(feed_url, child.text.strip())
        return None

    @staticmethod
    def _published(value: str | None, fallback: datetime) -> datetime:
        if not value:
            return fallback
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _summary(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()
        cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
        return cleaned[:1000] or None

    def _parse_node(
        self,
        node: ET.Element,
        *,
        feed: RssFeedSource,
        fetched_at: datetime,
    ) -> ProviderNewsItem | None:
        title = self._summary(self._text(node, "title"))
        link = self._link(node, feed.url)
        if not title or link is None:
            return None
        published_raw = self._text(node, "pubdate", "published", "updated", "date")
        published_at = self._published(published_raw, fetched_at)
        provider_id = self._text(node, "guid", "id")
        if not provider_id:
            provider_id = hashlib.sha256(
                f"{feed.source_name}|{link}|{published_at.isoformat()}|{title}".encode()
            ).hexdigest()
        return ProviderNewsItem(
            provider_item_id=provider_id,
            provider_id=feed.provider_id,
            canonical_url=link,
            provider=self.name,
            source_name=feed.source_name,
            source_reliability=feed.reliability,
            title=title,
            short_summary=self._summary(self._text(node, "description", "summary", "content")),
            published_at=published_at,
            language=feed.language,
            official=feed.official,
            official_symbols=feed.official_symbols,
            allowed_event_types=feed.allowed_event_types,
        )


class NoDataConfiguredNewsApiProvider:
    """Explicit, network-free NO_DATA provider used when no secret is configured."""

    name = "configured_news_api"
    configured = False

    def fetch(self, *, now: datetime | None = None) -> Sequence[ProviderNewsItem]:
        del now
        return ()


__all__ = [
    "ConfiguredNewsApiProvider",
    "NewsHttpResponse",
    "NewsProvider",
    "NewsTransport",
    "NoDataConfiguredNewsApiProvider",
    "OfficialAnnouncementProvider",
    "RssAtomProvider",
    "RssFeedSource",
]
