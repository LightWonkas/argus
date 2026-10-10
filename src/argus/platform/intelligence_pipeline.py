"""Lifts the RSS normalization into a versioned raw-document/event evidence chain."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from argus.core.intelligence import (
    CryptoNewsRecord,
    EventDirection,
    EventEntity,
    EventEvidence,
    EventSeverity,
    EventStatus,
    EventUrgency,
    NewsEventType,
    NewsReliabilityTier,
    NewsSentiment,
    NewsVerificationStatus,
    NormalizedCryptoEvent,
    ProviderNewsItem,
    RawNewsDocument,
    SourceRegistryEntry,
)
from argus.core.schemas import DataStatus, utcnow


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class IntelligenceNormalizer:
    """Deterministic transformer; depends on neither an LLM nor the network."""

    def raw_document(
        self, item: ProviderNewsItem, *, first_seen_at: datetime | None = None
    ) -> RawNewsDocument:
        seen = first_seen_at or utcnow()
        normalized = "\n".join((item.title.strip(), (item.short_summary or "").strip()))
        content_hash = _hash(normalized)
        return RawNewsDocument(
            document_id=_hash(f"{item.provider_id}|{item.provider_item_id}|{content_hash}"),
            source_id=item.provider_id,
            canonical_url=item.canonical_url,
            source_item_id=item.provider_item_id,
            title=item.title,
            body_or_summary=item.short_summary,
            published_at=item.published_at,
            first_seen_at=seen,
            fetched_at=seen,
            content_hash=content_hash,
            language=item.language,
            parser_version="rss-atom-v1",
            raw_metadata={"provider": item.provider, "official": item.official},
        )

    def normalized_event(
        self,
        record: CryptoNewsRecord,
        documents: Sequence[RawNewsDocument],
        registry: Mapping[str, SourceRegistryEntry],
        *,
        supersedes_event_id: str | None = None,
    ) -> NormalizedCryptoEvent:
        if not documents:
            raise ValueError("an event must carry at least one raw document")
        evidence: list[EventEvidence] = []
        origins: set[str] = set()
        seen_content_hashes: set[str] = set()
        trust_values: list[Decimal] = []
        for document in documents:
            source = registry.get(document.source_id)
            origin = source.official_domain if source else document.source_id
            # A verbatim agency/syndication copy is not independent corroboration even
            # when published on another domain. Different documents from the same source
            # do not inflate the source count either.
            if document.content_hash not in seen_content_hashes:
                origins.add(origin)
                seen_content_hashes.add(document.content_hash)
            role = "discovery"
            if source and source.trust_level is NewsReliabilityTier.OFFICIAL:
                role = "primary"
                trust_values.append(Decimal("1"))
            elif source and source.trust_level is NewsReliabilityTier.TRUSTED:
                role = "secondary"
                trust_values.append(Decimal("0.8"))
            else:
                trust_values.append(record.source_reliability)
            evidence.append(
                EventEvidence(
                    document_id=document.document_id,
                    source_id=document.source_id,
                    canonical_url=document.canonical_url,
                    evidence_role=role,
                    independent_origin=origin,
                    excerpt=(document.body_or_summary or "")[:500] or None,
                )
            )
        entities = tuple(
            EventEntity(
                entity_type="symbol",
                canonical_name=match.symbol,
                confidence=match.entity_confidence,
                evidence=match.match_evidence,
                ambiguous=match.ambiguous,
            )
            for match in record.entity_matches
        )
        ambiguous = any(entity.ambiguous for entity in entities)
        affected_symbols = () if ambiguous else record.related_symbols
        direction = {
            NewsSentiment.POSITIVE: EventDirection.POSITIVE,
            NewsSentiment.NEGATIVE: EventDirection.NEGATIVE,
            NewsSentiment.NEUTRAL: EventDirection.NEUTRAL,
            NewsSentiment.MIXED: EventDirection.MIXED,
        }.get(record.sentiment, EventDirection.UNKNOWN)
        critical = {
            NewsEventType.DELISTING,
            NewsEventType.DEPOSIT_PAUSED,
            NewsEventType.WITHDRAWAL_PAUSED,
            NewsEventType.HACK,
            NewsEventType.EXPLOIT,
            NewsEventType.BRIDGE_ATTACK,
            NewsEventType.STABLECOIN_DEPEG,
        }
        severity = (
            EventSeverity.CRITICAL
            if record.event_type in critical
            else EventSeverity.HIGH
            if record.impact_score >= 75
            else EventSeverity.MEDIUM
            if record.impact_score >= 40
            else EventSeverity.LOW
        )
        correction = record.event_type is NewsEventType.CORRECTION
        status = (
            EventStatus.CORRECTED
            if correction and supersedes_event_id is not None
            else EventStatus.ACTIVE
        )
        data_status = record.data_status
        uncertainties = list(record.risk_flags)
        if correction and supersedes_event_id is None:
            uncertainties.append("correction has no resolvable prior event")
            data_status = DataStatus.QUARANTINED
        if ambiguous:
            uncertainties.append("ambiguous entity; symbol excluded from decisions")
            data_status = DataStatus.QUARANTINED
        return NormalizedCryptoEvent(
            # CryptoNewsRecord.news_id is deterministic for the logical story and
            # does not include ingestion time.  Reusing it prevents a restart or a
            # later polling cycle from manufacturing a new event.
            event_id=record.news_id,
            document_ids=tuple(document.document_id for document in documents),
            event_type=record.event_type,
            event_status=status,
            affected_symbols=affected_symbols,
            affected_projects=record.related_projects,
            first_seen_at=record.first_seen_at,
            announced_at=record.published_at,
            expires_at=max(record.expires_at, record.first_seen_at + timedelta(seconds=1)),
            direction=direction,
            severity=severity,
            urgency=(
                EventUrgency.IMMEDIATE
                if severity is EventSeverity.CRITICAL
                else EventUrgency.ROUTINE
            ),
            confidence=record.confidence / Decimal("100"),
            source_trust=max(trust_values, default=Decimal("0")),
            corroboration_count=max(0, len(origins) - 1),
            facts=(record.title,),
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            evidence=tuple(evidence),
            entities=entities,
            deduplication_key=record.duplicate_group,
            supersedes_event_id=supersedes_event_id,
            data_status=(
                DataStatus.REJECTED
                if record.verification_status is NewsVerificationStatus.REJECTED
                else data_status
            ),
        )


__all__ = ["IntelligenceNormalizer"]
