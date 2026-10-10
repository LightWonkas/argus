"""General news API adapters — CryptoPanic · Finnhub · alternative.me F&G.

L1 adapter pattern: HTTP transport is hidden behind a **fetcher** (``JsonFetcher``) → unit
tests need no network (fake JSON is injected); the real fetcher is stdlib ``urllib`` (no new
dependency). Each source produces ticker-tagged :class:`~argus.core.contracts.NewsItem`.

SECURITY: title/summary are **external content** — they always pass through
``sanitize_external_text`` (injection defense; news is never a command). Tokens/keys come
from Settings (SecretStr); they are embedded in URLs but NEVER LOGGED.

Free-tier honesty: CryptoPanic uses a free token, Finnhub's free tier is ~60 requests/min.
Poll frequency lives in config; on overrun the source is skipped for that round and LABELED
(no silent data loss).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from argus.brain.security import sanitize_external_text
from argus.core.contracts import NewsItem
from argus.core.errors import AdapterError
from argus.core.schemas import Provenance, utcnow

# URL → parsed JSON. Fake in tests; _http_get_json when live.
JsonFetcher = Callable[[str], Any]

_TIMEOUT_S = 15.0
_TITLE_MAX = 500  # sanitize cap for news titles (also bounded before reaching the LLM)


def _http_get_json(url: str) -> Any:
    """Stdlib GET + JSON parse (live fetcher). Error → AdapterError (URL not logged: token!)."""
    request = urllib.request.Request(url, headers={"User-Agent": "argus/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network/HTTP/parse — wrap without leaking the tokenized URL
        raise AdapterError(f"news API request failed: {type(exc).__name__}") from exc


def _clean(text: str | None, *, max_len: int = _TITLE_MAX) -> str:
    return sanitize_external_text(text or "", max_len=max_len)


class CryptoPanicSource:
    """CryptoPanic posts API (crypto news, free token).

    https://cryptopanic.com/api/v1/posts/?auth_token=…&public=true
    """

    source = "cryptopanic"

    def __init__(self, token: str, *, fetcher: JsonFetcher | None = None) -> None:
        if not token:
            raise ValueError("CryptoPanic token must not be empty")
        self._token = token
        self._fetch = fetcher or _http_get_json

    def fetch(self, *, limit: int = 50) -> list[NewsItem]:
        url = "https://cryptopanic.com/api/v1/posts/?" + urllib.parse.urlencode(
            {"auth_token": self._token, "public": "true"}
        )
        payload = self._fetch(url)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise AdapterError("cryptopanic: unexpected response format (no results)")
        items: list[NewsItem] = []
        for raw in results[:limit]:
            try:
                items.append(self._to_item(raw))
            except Exception:  # one malformed record does not break the stream (fail-safe)
                continue
        return items

    def _to_item(self, raw: dict[str, Any]) -> NewsItem:
        published = _parse_iso(raw.get("published_at")) or utcnow()
        currencies = raw.get("currencies") or []
        symbols = tuple(
            f"{c['code']}USDT" for c in currencies if isinstance(c, dict) and c.get("code")
        )
        return NewsItem(
            id=str(raw.get("id") or raw.get("slug") or published.isoformat()),
            title=_clean(raw.get("title")) or "(untitled)",
            published_at=published,
            url=raw.get("url"),
            symbols=symbols,
            source_name=_clean((raw.get("source") or {}).get("title"), max_len=100) or None,
            provenance=Provenance(source=self.source, timestamp=published, confidence=0.6),
        )


class FinnhubSource:
    """Finnhub general news API (equities/general; free tier ~60 requests/min).

    https://finnhub.io/api/v1/news?category=general&token=…
    """

    source = "finnhub"

    def __init__(
        self, api_key: str, *, category: str = "general", fetcher: JsonFetcher | None = None
    ) -> None:
        if not api_key:
            raise ValueError("Finnhub key must not be empty")
        self._key = api_key
        self._category = category
        self._fetch = fetcher or _http_get_json

    def fetch(self, *, limit: int = 50) -> list[NewsItem]:
        url = "https://finnhub.io/api/v1/news?" + urllib.parse.urlencode(
            {"category": self._category, "token": self._key}
        )
        payload = self._fetch(url)
        if not isinstance(payload, list):
            raise AdapterError("finnhub: unexpected response format (not a list)")
        items: list[NewsItem] = []
        for raw in payload[:limit]:
            try:
                items.append(self._to_item(raw))
            except Exception:
                continue
        return items

    def _to_item(self, raw: dict[str, Any]) -> NewsItem:
        ts = raw.get("datetime")
        published = (
            datetime.fromtimestamp(float(ts), tz=UTC) if isinstance(ts, int | float) else utcnow()
        )
        related = str(raw.get("related") or "")
        symbols = tuple(s for s in (p.strip() for p in related.split(",")) if s)
        return NewsItem(
            id=str(raw.get("id") or published.isoformat()),
            title=_clean(raw.get("headline")) or "(untitled)",
            published_at=published,
            url=raw.get("url"),
            summary=_clean(raw.get("summary"), max_len=1000) or None,
            symbols=symbols,
            source_name=_clean(raw.get("source"), max_len=100) or None,
            provenance=Provenance(source=self.source, timestamp=published, confidence=0.6),
        )


class FearGreedSource:
    """alternative.me Crypto Fear & Greed index (free, no token) — a Positioning input.

    Value 0–100: ≤25 extreme fear · ≥75 extreme greed (contrarian reading in PositioningAgent).
    """

    source = "alternative.me"
    _URL = "https://api.alternative.me/fng/?limit=2"

    def __init__(self, *, fetcher: JsonFetcher | None = None) -> None:
        self._fetch = fetcher or _http_get_json

    def fetch(self) -> tuple[float, float | None]:
        """Return (current value, trend direction or None); malformed response → AdapterError."""
        payload = self._fetch(self._URL)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise AdapterError("alternative.me: unexpected response format")
        try:
            current = float(data[0]["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("alternative.me: could not parse value") from exc
        trend: float | None = None
        if len(data) > 1:
            try:
                previous = float(data[1]["value"])
            except (KeyError, TypeError, ValueError):
                previous = current
            if current > previous:
                trend = 1.0
            elif current < previous:
                trend = -1.0
        return current, trend


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
