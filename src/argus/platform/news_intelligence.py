"""Deterministic news normalization, entity resolution and verification pipeline."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from argus.core.intelligence import (
    CryptoNewsRecord,
    NewsCoverageStatus,
    NewsEntityMatch,
    NewsEventType,
    NewsIngestionBatch,
    NewsProviderHealth,
    NewsProviderHealthStatus,
    NewsSentiment,
    NewsVerificationStatus,
    ProviderNewsItem,
)
from argus.core.schemas import DataStatus, utcnow
from argus.ingestion.news_intelligence import NewsProvider

# Multilingual detection vocabulary (English + Turkish terms): functional data used to
# classify Turkish-language news — the Turkish entries are intentional, not leftovers.
_NON_WORD = re.compile(r"[^a-z0-9çğıöşü]+", re.IGNORECASE)
_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all instructions",
    "system prompt",
    "developer message",
    "execute this command",
    "forget your rules",
    "önceki talimatları görmezden gel",
    "sistem mesajı",
)
_EVENT_TERMS: tuple[tuple[NewsEventType, tuple[str, ...]], ...] = (
    (NewsEventType.CORRECTION, ("correction", "corrected", "errata", "düzeltme")),
    (NewsEventType.WITHDRAWAL_RESUMED, ("withdrawals resumed", "withdrawal restored")),
    (NewsEventType.DEPOSIT_RESUMED, ("deposits resumed", "deposit restored")),
    (NewsEventType.WITHDRAWAL_PAUSED, ("withdrawals paused", "withdrawal suspended")),
    (NewsEventType.DEPOSIT_PAUSED, ("deposits paused", "deposit suspended", "deposits disabled")),
    (NewsEventType.TRADING_PAIR_REMOVED, ("trading pair removed", "remove trading pair")),
    (NewsEventType.TRADING_PAIR_ADDED, ("trading pair added", "new trading pair")),
    (NewsEventType.DELISTING, ("delist", "delisting", "listeden çıkar")),
    (NewsEventType.LISTING, ("listing", "will list", "listeleme", "listelenecek")),
    (NewsEventType.BRIDGE_ATTACK, ("bridge attack", "bridge exploit")),
    (NewsEventType.HACK, ("hack", "hacked")),
    (NewsEventType.EXPLOIT, ("exploit", "drained", "sömürü", "çalındı")),
    (
        NewsEventType.SECURITY_INCIDENT,
        ("security incident", "breach", "vulnerability", "güvenlik açığı"),
    ),
    (NewsEventType.STABLECOIN_DEPEG, ("depeg", "de-peg", "lost its peg")),
    (NewsEventType.STABLECOIN_RESERVE_UPDATE, ("reserve attestation", "reserve report")),
    (NewsEventType.ETF_DECISION, ("etf approval", "etf decision", "exchange-traded fund")),
    (NewsEventType.REGULATORY_ACTION, ("enforcement action", "regulatory action", "charges ")),
    (NewsEventType.REGULATORY, ("regulator", "regulation", "sec ", "düzenleyici")),
    (NewsEventType.TOKEN_UNLOCK, ("token unlock", "unlock schedule", "kilit açılım")),
    (NewsEventType.TOKEN_BURN, ("token burn", "burned tokens")),
    (NewsEventType.TOKEN_MINT, ("token mint", "minted tokens")),
    (NewsEventType.TOKENOMICS_CHANGE, ("tokenomics", "supply schedule")),
    (NewsEventType.OUTAGE, ("outage", "service disruption", "kesinti")),
    (NewsEventType.LAWSUIT, ("lawsuit", "sued", "legal action", "dava")),
    (
        NewsEventType.RATE_DECISION,
        ("interest rate decision", "federal funds rate", "monetary policy"),
    ),
    (NewsEventType.CPI_RELEASE, ("consumer price index", "cpi ", "inflation release")),
    (
        NewsEventType.EMPLOYMENT_RELEASE,
        ("employment situation", "nonfarm payroll", "unemployment rate"),
    ),
    (
        NewsEventType.LIQUIDITY_EVENT,
        ("liquidity facility", "balance sheet", "quantitative tightening"),
    ),
    (NewsEventType.PARTNERSHIP, ("partnership", "partnered", "iş ortaklığı")),
    (
        NewsEventType.NETWORK_UPGRADE,
        ("network upgrade", "mainnet upgrade", "ağ yükselt"),
    ),
    (NewsEventType.HARD_FORK, ("hard fork",)),
    (NewsEventType.PROTOCOL_RELEASE, ("protocol release", "protocol version")),
    (
        NewsEventType.PRODUCT_RELEASE,
        ("product launch", "released", "launches", "ürün lansmanı"),
    ),
    (NewsEventType.EXCHANGE_FLOW, ("exchange inflow", "exchange outflow", "whale deposit")),
    (NewsEventType.RUMOR, ("rumor", "unconfirmed", "iddia", "söylenti")),
)
_NEGATIVE_EVENTS = {
    NewsEventType.DELISTING,
    NewsEventType.SECURITY_INCIDENT,
    NewsEventType.EXPLOIT,
    NewsEventType.OUTAGE,
    NewsEventType.LAWSUIT,
    NewsEventType.HACK,
    NewsEventType.BRIDGE_ATTACK,
    NewsEventType.STABLECOIN_DEPEG,
    NewsEventType.DEPOSIT_PAUSED,
    NewsEventType.WITHDRAWAL_PAUSED,
    NewsEventType.TRADING_PAIR_REMOVED,
    NewsEventType.REGULATORY_ACTION,
}
_POSITIVE_EVENTS = {
    NewsEventType.LISTING,
    NewsEventType.PARTNERSHIP,
    NewsEventType.PRODUCT_RELEASE,
    NewsEventType.NETWORK_UPGRADE,
    NewsEventType.PROTOCOL_RELEASE,
    NewsEventType.DEPOSIT_RESUMED,
    NewsEventType.WITHDRAWAL_RESUMED,
    NewsEventType.TRADING_PAIR_ADDED,
    NewsEventType.TOKEN_BURN,
}


@dataclass(frozen=True)
class NewsIntelligencePolicy:
    maximum_age: timedelta = timedelta(hours=24)
    future_tolerance: timedelta = timedelta(minutes=2)
    default_ttl: timedelta = timedelta(hours=6)
    minimum_reliability: Decimal = Decimal("0.60")
    verified_reliability: Decimal = Decimal("0.80")


class NewsIntelligenceEngine:
    """Turns provider output into PIT records while treating it as untrusted data."""

    def __init__(
        self,
        providers: Sequence[NewsProvider],
        *,
        entity_catalog: Mapping[str, Sequence[str]] | None = None,
        policy: NewsIntelligencePolicy | None = None,
    ) -> None:
        self.providers = tuple(providers)
        self.policy = policy or NewsIntelligencePolicy()
        aliases = entity_catalog or {
            "BTCUSDT": ("bitcoin",),
            "ETHUSDT": ("ethereum", "ether"),
            "BNBUSDT": ("binance coin", "bnb chain"),
            "SOLUSDT": ("solana",),
            "XRPUSDT": ("xrp ledger", "ripple"),
            "DOGEUSDT": ("dogecoin",),
            "UNIUSDT": ("uniswap",),
            "ZECUSDT": ("zcash",),
            "ADAUSDT": ("cardano",),
            "LINKUSDT": ("chainlink",),
            "AVAXUSDT": ("avalanche",),
            "DOTUSDT": ("polkadot",),
        }
        self.entity_catalog = {
            symbol.strip().upper(): tuple(
                alias.strip().lower() for alias in values if alias.strip()
            )
            for symbol, values in aliases.items()
        }

    def collect(
        self,
        *,
        universe_symbols: Sequence[str] = (),
        now: datetime | None = None,
    ) -> NewsIngestionBatch:
        at = now or utcnow()
        if at.tzinfo is None:
            raise ValueError("news collection time must be tz-aware")
        raw: list[ProviderNewsItem] = []
        errors: list[str] = []
        health: list[NewsProviderHealth] = []
        successful_providers = 0
        for provider in self.providers:
            try:
                raw.extend(provider.fetch(now=at))
                successful_providers += 1
            except Exception as exc:
                # The URL, response body or any token never reaches the error text or logs.
                errors.append(f"{provider.name}:{type(exc).__name__}")
            snapshots = getattr(provider, "health_snapshots", ())
            health.extend(item for item in snapshots if isinstance(item, NewsProviderHealth))
        groups: dict[str, list[ProviderNewsItem]] = defaultdict(list)
        for item in raw:
            groups[self._duplicate_key(item)].append(item)
        allowed = {symbol.strip().upper() for symbol in universe_symbols if symbol.strip()}
        if not allowed:
            allowed = set(self.entity_catalog)
        records = tuple(
            self._record(group, duplicate_group=key, allowed_symbols=allowed, now=at)
            for key, group in sorted(groups.items())
        )
        coverage = {
            symbol: self._coverage_for_symbol(
                symbol,
                records,
                health,
                successful_providers=successful_providers,
                errors=errors,
            )
            for symbol in sorted(allowed)
        }
        overall_coverage = self._overall_coverage(tuple(coverage.values()))
        usable = any(record.data_status is DataStatus.OK for record in records)
        if usable or overall_coverage is NewsCoverageStatus.NO_RELEVANT_NEWS:
            status = DataStatus.QUARANTINED if errors else DataStatus.OK
        elif overall_coverage in {
            NewsCoverageStatus.PROVIDER_STALE,
            NewsCoverageStatus.NEWS_CONFLICT,
        }:
            status = DataStatus.QUARANTINED
        else:
            status = DataStatus.REJECTED if records else DataStatus.PENDING
        return NewsIngestionBatch(
            collected_at=at,
            records=records,
            raw_items=tuple(raw),
            provider_errors=tuple(errors),
            provider_health=tuple(health),
            coverage_by_symbol=coverage,
            coverage_status=overall_coverage,
            duplicate_count=max(0, len(raw) - len(records)),
            data_status=status,
        )

    @staticmethod
    def _coverage_for_symbol(
        symbol: str,
        records: Sequence[CryptoNewsRecord],
        health: Sequence[NewsProviderHealth],
        *,
        successful_providers: int,
        errors: Sequence[str],
    ) -> NewsCoverageStatus:
        relevant = [record for record in records if symbol in record.related_symbols]
        if any(
            record.verification_status is NewsVerificationStatus.DISPUTED for record in relevant
        ):
            return NewsCoverageStatus.NEWS_CONFLICT
        if any(record.data_status is DataStatus.OK for record in relevant):
            return NewsCoverageStatus.RELEVANT_NEWS
        if relevant:
            return NewsCoverageStatus.NEWS_REJECTED
        statuses = {item.health_status for item in health if item.enabled}
        if NewsProviderHealthStatus.HEALTHY in statuses or (
            not health and successful_providers > 0
        ):
            return NewsCoverageStatus.NO_RELEVANT_NEWS
        if NewsProviderHealthStatus.ERROR in statuses:
            return NewsCoverageStatus.NEWS_ERROR
        if NewsProviderHealthStatus.STALE in statuses:
            return NewsCoverageStatus.PROVIDER_STALE
        if errors or statuses & {
            NewsProviderHealthStatus.UNAVAILABLE,
            NewsProviderHealthStatus.BACKOFF,
        }:
            return NewsCoverageStatus.PROVIDER_UNAVAILABLE
        return NewsCoverageStatus.PROVIDER_UNAVAILABLE

    @staticmethod
    def _overall_coverage(values: tuple[NewsCoverageStatus, ...]) -> NewsCoverageStatus:
        order = (
            NewsCoverageStatus.NEWS_CONFLICT,
            NewsCoverageStatus.RELEVANT_NEWS,
            NewsCoverageStatus.NEWS_REJECTED,
            NewsCoverageStatus.NO_RELEVANT_NEWS,
            NewsCoverageStatus.PROVIDER_STALE,
            NewsCoverageStatus.NEWS_ERROR,
            NewsCoverageStatus.PROVIDER_UNAVAILABLE,
        )
        return next(
            (status for status in order if status in values),
            NewsCoverageStatus.PROVIDER_UNAVAILABLE,
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(_NON_WORD.sub(" ", value.casefold()).split())

    @staticmethod
    def _canonical_url(value: str) -> str:
        parsed = urlsplit(value)
        query = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source"}
        ]
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(query), "")
        )

    def _duplicate_key(self, item: ProviderNewsItem) -> str:
        title = self._normalize_text(item.title)
        # URLs differ across providers; the verification group is normalized title + UTC day.
        value = f"{title}|{item.published_at.date().isoformat()}"
        return hashlib.sha256(value.encode()).hexdigest()

    def duplicate_key(self, item: ProviderNewsItem) -> str:
        """Public deterministic grouping key used by PIT persistence."""
        return self._duplicate_key(item)

    def _resolve_entities(
        self,
        text: str,
        allowed_symbols: set[str],
        *,
        official_symbols: Sequence[str] = (),
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[NewsEntityMatch, ...]]:
        lowered = text.casefold()
        symbols: list[str] = []
        projects: list[str] = []
        risks: list[str] = []
        matches: list[NewsEntityMatch] = []
        official = {symbol.upper() for symbol in official_symbols}
        for symbol, aliases in self.entity_catalog.items():
            if symbol not in allowed_symbols:
                continue
            base = symbol.removesuffix("USDT")
            strong_symbol = bool(
                re.search(rf"(?<![A-Z0-9])\${re.escape(base)}(?![A-Z0-9])", text.upper())
                or symbol in text.upper()
            )
            matching_aliases = [
                alias
                for alias in aliases
                if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered)
            ]
            evidence: list[str] = []
            if symbol in official:
                evidence.append("official_provider_domain")
            if strong_symbol:
                evidence.append("qualified_symbol")
            evidence.extend(f"project_alias:{alias}" for alias in matching_aliases)
            if evidence:
                symbols.append(symbol)
                projects.extend(matching_aliases or (base.lower(),))
                confidence = (
                    Decimal("0.99")
                    if symbol in official
                    else (
                        Decimal("0.95") if strong_symbol and matching_aliases else Decimal("0.85")
                    )
                )
                matches.append(
                    NewsEntityMatch(
                        symbol=symbol,
                        entity_confidence=confidence,
                        match_evidence=tuple(evidence),
                    )
                )
            elif re.search(rf"(?<![A-Z0-9]){re.escape(base)}(?![A-Z0-9])", text.upper()):
                # A bare short ticker is not entity evidence on its own (SOL/ONE/NEAR etc.).
                risks.append(f"ambiguous_ticker:{base}")
                matches.append(
                    NewsEntityMatch(
                        symbol=symbol,
                        entity_confidence=Decimal("0.20"),
                        match_evidence=(f"bare_ticker:{base}",),
                        ambiguous=True,
                        rejection_reason="a bare short ticker is not sufficient entity evidence",
                    )
                )
        return tuple(symbols), tuple(dict.fromkeys(projects)), tuple(risks), tuple(matches)

    @staticmethod
    def _classify_event(text: str) -> NewsEventType:
        lowered = text.casefold()
        if any(term in lowered for term in ("rumor", "unconfirmed", "iddia", "söylenti")):
            return NewsEventType.RUMOR
        for event_type, terms in _EVENT_TERMS:
            if any(term in lowered for term in terms):
                return event_type
        if "crypto" in lowered or "bitcoin" in lowered or "market" in lowered:
            return NewsEventType.GENERAL_MARKET
        return NewsEventType.UNKNOWN

    @staticmethod
    def _sentiment(event_type: NewsEventType) -> NewsSentiment:
        if event_type in _NEGATIVE_EVENTS:
            return NewsSentiment.NEGATIVE
        if event_type in _POSITIVE_EVENTS:
            return NewsSentiment.POSITIVE
        if event_type is NewsEventType.RUMOR:
            return NewsSentiment.UNKNOWN
        return NewsSentiment.NEUTRAL

    def _record(
        self,
        items: Sequence[ProviderNewsItem],
        *,
        duplicate_group: str,
        allowed_symbols: set[str],
        now: datetime,
    ) -> CryptoNewsRecord:
        representative = max(items, key=lambda item: (item.source_reliability, item.official))
        canonical_url = self._canonical_url(representative.canonical_url)
        combined = f"{representative.title}\n{representative.short_summary or ''}"
        official_symbols = {
            symbol for item in items if item.official for symbol in item.official_symbols
        }
        symbols, projects, entity_risks, entity_matches = self._resolve_entities(
            combined,
            allowed_symbols,
            official_symbols=tuple(official_symbols),
        )
        event_type = self._classify_event(combined)
        risks = list(entity_risks)
        lowered = combined.casefold()
        if any(marker in lowered for marker in _INJECTION_MARKERS):
            risks.append("prompt_injection_text")
        independent_sources = {
            item.source_name.casefold()
            for item in items
            if item.source_reliability >= self.policy.minimum_reliability
        }
        official = any(item.official for item in items)
        status = NewsVerificationStatus.SINGLE_SOURCE
        data_status = DataStatus.OK
        item_sentiments = {
            self._sentiment(self._classify_event(f"{item.title}\n{item.short_summary or ''}"))
            for item in items
        }
        conflicting = (
            NewsSentiment.POSITIVE in item_sentiments and NewsSentiment.NEGATIVE in item_sentiments
        )
        if representative.published_at > now + self.policy.future_tolerance:
            status = NewsVerificationStatus.REJECTED
            data_status = DataStatus.REJECTED
            risks.append("future_timestamp")
        elif now - representative.published_at > self.policy.maximum_age:
            status = NewsVerificationStatus.REJECTED
            data_status = DataStatus.REJECTED
            risks.append("expired_news")
        elif event_type is NewsEventType.RUMOR:
            status = NewsVerificationStatus.RUMOR
            data_status = DataStatus.QUARANTINED
            risks.append("unverified_rumor")
        elif conflicting:
            status = NewsVerificationStatus.DISPUTED
            data_status = DataStatus.QUARANTINED
            risks.append("conflicting_sources")
        elif (
            representative.allowed_event_types
            and event_type not in representative.allowed_event_types
        ):
            status = NewsVerificationStatus.REJECTED
            data_status = DataStatus.REJECTED
            risks.append("event_type_not_allowed_for_provider")
        elif representative.source_reliability < self.policy.minimum_reliability:
            status = NewsVerificationStatus.REJECTED
            data_status = DataStatus.REJECTED
            risks.append("low_source_reliability")
        elif official or (
            len(independent_sources) >= 2
            and representative.source_reliability >= self.policy.verified_reliability
        ):
            status = NewsVerificationStatus.VERIFIED
        if not symbols:
            risks.append("no_resolved_symbol")
        confidence = representative.source_reliability * Decimal("100")
        if status is NewsVerificationStatus.VERIFIED:
            confidence = min(Decimal("100"), confidence + Decimal("10"))
        if "prompt_injection_text" in risks:
            confidence = max(Decimal("0"), confidence - Decimal("20"))
        impact = {
            NewsEventType.HACK: Decimal("95"),
            NewsEventType.BRIDGE_ATTACK: Decimal("95"),
            NewsEventType.EXPLOIT: Decimal("90"),
            NewsEventType.SECURITY_INCIDENT: Decimal("80"),
            NewsEventType.DELISTING: Decimal("80"),
            NewsEventType.LISTING: Decimal("65"),
            NewsEventType.REGULATORY: Decimal("70"),
            NewsEventType.REGULATORY_ACTION: Decimal("80"),
            NewsEventType.ETF_DECISION: Decimal("80"),
            NewsEventType.STABLECOIN_DEPEG: Decimal("95"),
            NewsEventType.DEPOSIT_PAUSED: Decimal("80"),
            NewsEventType.WITHDRAWAL_PAUSED: Decimal("85"),
            NewsEventType.TOKEN_UNLOCK: Decimal("60"),
        }.get(
            event_type, Decimal("40") if event_type is not NewsEventType.UNKNOWN else Decimal("15")
        )
        content_hash = hashlib.sha256(self._normalize_text(combined).encode()).hexdigest()
        news_id = hashlib.sha256(
            f"{duplicate_group}|{representative.published_at.isoformat()}".encode()
        ).hexdigest()
        return CryptoNewsRecord(
            news_id=news_id,
            canonical_url=canonical_url,
            provider=representative.provider,
            source_name=representative.source_name,
            source_reliability=representative.source_reliability,
            title=representative.title,
            short_summary=representative.short_summary,
            published_at=representative.published_at,
            first_seen_at=now,
            ingested_at=now,
            related_symbols=symbols,
            related_projects=projects,
            entity_matches=entity_matches,
            language=representative.language,
            event_type=event_type,
            sentiment=self._sentiment(event_type),
            impact_score=impact,
            confidence=confidence,
            verification_status=status,
            duplicate_group=duplicate_group,
            content_hash=content_hash,
            expires_at=representative.published_at + self.policy.default_ttl,
            risk_flags=tuple(dict.fromkeys(risks)),
            data_status=data_status,
        )


__all__ = ["NewsIntelligenceEngine", "NewsIntelligencePolicy"]
