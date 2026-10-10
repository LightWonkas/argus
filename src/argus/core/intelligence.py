"""Intraday news, market-reaction and trade-plan contracts.

These models belong to the research/decision journal. None of them carries order
submission authority, position size or leverage. News text is untrusted external data;
only the title, a short summary and structured facts are stored.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus.core.enums import MarketRegimeKind, Timeframe
from argus.core.schemas import DataStatus, utcnow


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be tz-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def _http_url(value: str) -> str:
    cleaned = value.strip()
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only a valid http/https URL is accepted")
    return cleaned


def _symbols(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip().upper() for value in values if value.strip()))


class NewsEventType(StrEnum):
    LISTING = "listing"
    DELISTING = "delisting"
    TRADING_PAIR_ADDED = "trading_pair_added"
    TRADING_PAIR_REMOVED = "trading_pair_removed"
    DEPOSIT_PAUSED = "deposit_paused"
    WITHDRAWAL_PAUSED = "withdrawal_paused"
    DEPOSIT_RESUMED = "deposit_resumed"
    WITHDRAWAL_RESUMED = "withdrawal_resumed"
    PARTNERSHIP = "partnership"
    PRODUCT_RELEASE = "product_release"
    PROTOCOL_RELEASE = "protocol_release"
    NETWORK_UPGRADE = "network_upgrade"
    HARD_FORK = "hard_fork"
    TOKENOMICS_CHANGE = "tokenomics_change"
    TOKEN_BURN = "token_burn"
    TOKEN_MINT = "token_mint"
    STABLECOIN_DEPEG = "stablecoin_depeg"
    STABLECOIN_RESERVE_UPDATE = "stablecoin_reserve_update"
    HACK = "hack"
    SECURITY_INCIDENT = "security_incident"
    EXPLOIT = "exploit"
    BRIDGE_ATTACK = "bridge_attack"
    SUSPICIOUS_TRANSFER = "suspicious_transfer"
    ETF_DECISION = "etf_decision"
    REGULATORY = "regulatory"
    REGULATORY_ACTION = "regulatory_action"
    TOKEN_UNLOCK = "token_unlock"
    EXCHANGE_FLOW = "exchange_flow"
    OUTAGE = "outage"
    LAWSUIT = "lawsuit"
    RATE_DECISION = "rate_decision"
    CPI_RELEASE = "cpi_release"
    EMPLOYMENT_RELEASE = "employment_release"
    LIQUIDITY_EVENT = "liquidity_event"
    RUMOR = "rumor"
    CORRECTION = "correction"
    GENERAL_MARKET = "general_market"
    UNKNOWN = "unknown"


class SourceGroup(StrEnum):
    EXCHANGE_ANNOUNCEMENT = "exchange_announcement"
    REGULATOR = "regulator"
    MACROECONOMY = "macroeconomy"
    SECURITY_ALERT = "security_alert"
    OFFICIAL_PROJECT = "official_project"
    STABLECOIN_ISSUER = "stablecoin_issuer"
    GENERAL_NEWS = "general_news"
    NEWS_AGGREGATOR = "news_aggregator"
    OPTIONAL_TECHNICAL_SIGNAL = "optional_technical_signal"


class SourceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MANUAL_CONFIGURATION_REQUIRED = "manual_configuration_required"


class EventStatus(StrEnum):
    ACTIVE = "active"
    UPDATED = "updated"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class EventDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class EventSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EventUrgency(StrEnum):
    ROUTINE = "routine"
    ELEVATED = "elevated"
    IMMEDIATE = "immediate"


class NewsSentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class NewsVerificationStatus(StrEnum):
    VERIFIED = "verified"
    SINGLE_SOURCE = "single_source"
    RUMOR = "rumor"
    DISPUTED = "disputed"
    REJECTED = "rejected"
    NO_DATA = "no_data"


class NewsCoverageStatus(StrEnum):
    """Result that keeps source health and per-coin news coverage separate."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_STALE = "provider_stale"
    NO_RELEVANT_NEWS = "no_relevant_news"
    RELEVANT_NEWS = "relevant_news"
    NEWS_REJECTED = "news_rejected"
    NEWS_CONFLICT = "news_conflict"
    NEWS_ERROR = "news_error"


class NewsSourceType(StrEnum):
    RSS = "rss"
    ATOM = "atom"
    OFFICIAL_API = "official_api"


class NewsReliabilityTier(StrEnum):
    GENERAL = "general"
    TRUSTED = "trusted"
    OFFICIAL = "official"


class NewsProviderHealthStatus(StrEnum):
    HEALTHY = "healthy"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    BACKOFF = "backoff"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"


class MarketReactionStatus(StrEnum):
    NOT_PRICED = "not_priced"
    PARTIALLY_PRICED = "partially_priced"
    ALREADY_PRICED = "already_priced"
    ADVERSE_REACTION = "adverse_reaction"
    PUMP_RISK = "pump_risk"
    INCONCLUSIVE = "inconclusive"
    NO_DATA = "no_data"
    # v2 names; old values are kept for point-in-time records.
    LIKELY_PRICED_IN = "likely_priced_in"
    OVERREACTION = "overreaction"
    REVERSAL_RISK = "reversal_risk"
    INSUFFICIENT_DATA = "insufficient_data"


class SourceRetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    maximum_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = Field(default=5.0, gt=0, le=3600)
    maximum_delay_seconds: float = Field(default=300.0, gt=0, le=86_400)

    @model_validator(mode="after")
    def _ordered_delays(self) -> SourceRetryPolicy:
        if self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError("maximum_delay_seconds must not be below base_delay_seconds")
        return self


class SourceRegistryEntry(BaseModel):
    """Single typed view of the config-defined source and its runtime health."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["source-registry-v1"] = "source-registry-v1"
    source_id: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    source_group: SourceGroup
    official_domain: str = Field(..., min_length=1, max_length=253)
    feed_type: NewsSourceType
    feed_url: str | None = None
    enabled: bool = False
    availability: SourceAvailability = SourceAvailability.UNAVAILABLE
    priority: int = Field(default=100, ge=0, le=1000)
    trust_level: NewsReliabilityTier = NewsReliabilityTier.GENERAL
    supported_event_types: tuple[NewsEventType, ...] = ()
    symbol_scope: tuple[str, ...] = ()
    polling_interval_seconds: int = Field(default=300, ge=30, le=86_400)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    freshness_threshold_seconds: int = Field(default=86_400, gt=0, le=2_592_000)
    rate_limit_per_minute: int = Field(default=2, ge=1, le=600)
    retry_policy: SourceRetryPolicy = Field(default_factory=SourceRetryPolicy)
    parser_version: str = Field(default="rss-atom-v1", min_length=1, max_length=64)
    terms_or_license_note: str = Field(..., min_length=1, max_length=1000)
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    health_status: NewsProviderHealthStatus = NewsProviderHealthStatus.NOT_CONFIGURED
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    parse_failure_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    fetch_latency_ms: float | None = Field(default=None, ge=0)
    quarantine_reason: str | None = Field(default=None, max_length=500)

    _feed = field_validator("feed_url")(lambda value: None if value is None else _http_url(value))
    _scope = field_validator("symbol_scope")(_symbols)
    _success_utc = field_validator("last_success_at")(_optional_utc)
    _failure_utc = field_validator("last_failure_at")(_optional_utc)

    @model_validator(mode="after")
    def _configuration_consistency(self) -> SourceRegistryEntry:
        if self.enabled and (
            self.feed_url is None or self.availability is not SourceAvailability.AVAILABLE
        ):
            raise ValueError("an enabled source must be AVAILABLE and have a feed_url")
        return self


class RawNewsDocument(BaseModel):
    """Immutable raw news envelope with a fixed first-seen time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["raw-news-document-v1"] = "raw-news-document-v1"
    document_id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = Field(..., min_length=1)
    canonical_url: str
    source_item_id: str = Field(..., min_length=1, max_length=500)
    title: str = Field(..., min_length=1, max_length=500)
    body_or_summary: str | None = Field(default=None, max_length=20_000)
    author: str | None = Field(default=None, max_length=300)
    published_at: datetime | None = None
    first_seen_at: datetime
    fetched_at: datetime
    content_hash: str = Field(..., min_length=64, max_length=64)
    language: str = Field(default="und", min_length=2, max_length=16)
    parser_version: str = Field(..., min_length=1, max_length=64)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    _url = field_validator("canonical_url")(_http_url)
    _published = field_validator("published_at")(_optional_utc)
    _first_seen = field_validator("first_seen_at")(_utc)
    _fetched = field_validator("fetched_at")(_utc)

    @model_validator(mode="after")
    def _pit_order(self) -> RawNewsDocument:
        if self.first_seen_at > self.fetched_at:
            raise ValueError("first_seen_at must not be after fetched_at")
        return self


class EventEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    source_id: str
    canonical_url: str
    evidence_role: Literal["primary", "secondary", "discovery"]
    independent_origin: str = Field(..., min_length=1)
    excerpt: str | None = Field(default=None, max_length=500)

    _url = field_validator("canonical_url")(_http_url)


class EventEntity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_type: Literal["symbol", "project", "network", "exchange"]
    canonical_name: str = Field(..., min_length=1)
    confidence: Decimal = Field(..., ge=0, le=1)
    evidence: tuple[str, ...] = ()
    ambiguous: bool = False


class NormalizedCryptoEvent(BaseModel):
    """PIT event contract merging one or more raw documents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["normalized-crypto-event-v1"] = "normalized-crypto-event-v1"
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    document_ids: tuple[str, ...] = Field(..., min_length=1)
    event_type: NewsEventType
    event_status: EventStatus = EventStatus.ACTIVE
    affected_symbols: tuple[str, ...] = ()
    affected_projects: tuple[str, ...] = ()
    affected_networks: tuple[str, ...] = ()
    affected_exchanges: tuple[str, ...] = ()
    occurred_at: datetime | None = None
    announced_at: datetime | None = None
    first_seen_at: datetime
    expires_at: datetime
    direction: EventDirection = EventDirection.UNKNOWN
    severity: EventSeverity = EventSeverity.INFO
    urgency: EventUrgency = EventUrgency.ROUTINE
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    source_trust: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    corroboration_count: int = Field(default=0, ge=0)
    facts: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    evidence: tuple[EventEvidence, ...] = ()
    entities: tuple[EventEntity, ...] = ()
    deduplication_key: str = Field(..., min_length=1, max_length=128)
    supersedes_event_id: str | None = None
    data_status: DataStatus = DataStatus.PENDING

    _symbols = field_validator("affected_symbols")(_symbols)
    _occurred = field_validator("occurred_at")(_optional_utc)
    _announced = field_validator("announced_at")(_optional_utc)
    _first_seen = field_validator("first_seen_at")(_utc)
    _expires = field_validator("expires_at")(_utc)

    @model_validator(mode="after")
    def _event_consistency(self) -> NormalizedCryptoEvent:
        if self.expires_at <= self.first_seen_at:
            raise ValueError("expires_at must be after first_seen_at")
        if self.event_status in {EventStatus.CORRECTED, EventStatus.SUPERSEDED} and not (
            self.supersedes_event_id
        ):
            raise ValueError("a correction/superseded event must carry the previous event_id")
        if any(entity.ambiguous for entity in self.entities) and self.affected_symbols:
            raise ValueError("an ambiguous entity cannot be published as a trading symbol")
        return self


class ExternalTechnicalSignal(BaseModel):
    """External alert; not an order or a price/position decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["external-technical-signal-v1"] = "external-technical-signal-v1"
    signal_id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = "tradingview"
    symbol: str
    signal_type: str = Field(..., min_length=1, max_length=100)
    timeframe: str = Field(..., min_length=1, max_length=16)
    direction: Literal["bullish", "bearish", "neutral"]
    observed_at: datetime
    received_at: datetime = Field(default_factory=utcnow)
    nonce: str = Field(..., min_length=8, max_length=128)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    data_status: DataStatus = DataStatus.PENDING
    submission_authorized: Literal[False] = False

    _symbol = field_validator("symbol")(lambda value: value.strip().upper())
    _observed = field_validator("observed_at")(_utc)
    _received = field_validator("received_at")(_utc)


class TradeHorizon(StrEnum):
    NO_TRADE = "no_trade"
    SCALP_15_30_MIN = "scalp_15_30_min"
    INTRADAY_30_60_MIN = "intraday_30_60_min"
    EXTENDED_INTRADAY = "extended_intraday"


class PreferredOrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    NONE = "none"


class TradeStrategyBasis(StrEnum):
    NEWS_CATALYST = "news_catalyst"
    TECHNICAL_INTRADAY = "technical_intraday"
    NO_TRADE = "no_trade"


class CostSource(StrEnum):
    VERIFIED_ACCOUNT = "verified_account"
    CONSERVATIVE_ESTIMATE = "conservative_estimate"
    NO_DATA = "no_data"


class DailyMissionAction(StrEnum):
    CONTINUE = "continue"
    DAILY_PROFIT_LOCK = "daily_profit_lock"
    DAILY_TARGET_REACHED = "daily_target_reached"
    DAILY_LOSS_LIMIT = "daily_loss_limit"


class ProviderNewsItem(BaseModel):
    """Short news record normalised by the provider and not yet verified."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_item_id: str = Field(..., min_length=1)
    provider_id: str = Field(default="rss_atom", min_length=1)
    canonical_url: str
    provider: str = Field(..., min_length=1)
    source_name: str = Field(..., min_length=1)
    source_reliability: Decimal = Field(..., ge=0, le=1)
    title: str = Field(..., min_length=1, max_length=500)
    short_summary: str | None = Field(default=None, max_length=1000)
    published_at: datetime
    language: str = Field(default="und", min_length=2, max_length=16)
    official: bool = False
    official_symbols: tuple[str, ...] = ()
    allowed_event_types: tuple[NewsEventType, ...] = ()

    _url = field_validator("canonical_url")(_http_url)
    _published_utc = field_validator("published_at")(_utc)
    _official_symbols = field_validator("official_symbols")(_symbols)


class NewsEntityMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    entity_confidence: Decimal = Field(..., ge=0, le=1)
    match_evidence: tuple[str, ...] = ()
    ambiguous: bool = False
    rejection_reason: str | None = None

    _symbol = field_validator("symbol")(lambda value: value.strip().upper())


class NewsProviderHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    feed_url: str
    source_type: NewsSourceType
    reliability_tier: NewsReliabilityTier
    enabled: bool = True
    timeout_seconds: float = Field(..., gt=0)
    maximum_items: int = Field(..., gt=0)
    maximum_payload_bytes: int = Field(..., ge=1024)
    freshness_window_seconds: int = Field(..., gt=0)
    allowed_event_types: tuple[NewsEventType, ...] = ()
    last_checked_at: datetime
    last_success_at: datetime | None = None
    last_item_published_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    health_status: NewsProviderHealthStatus
    response_status: int | None = None
    items_received: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    etag_present: bool = False
    last_modified_present: bool = False
    error_type: str | None = None

    _feed_url = field_validator("feed_url")(_http_url)
    _last_checked = field_validator("last_checked_at")(_utc)
    _last_success = field_validator("last_success_at")(_optional_utc)
    _last_item = field_validator("last_item_published_at")(_optional_utc)


class CryptoNewsRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    news_id: str = Field(default_factory=lambda: uuid4().hex)
    canonical_url: str
    provider: str = Field(..., min_length=1)
    source_name: str = Field(..., min_length=1)
    source_reliability: Decimal = Field(..., ge=0, le=1)
    title: str = Field(..., min_length=1, max_length=500)
    short_summary: str | None = Field(default=None, max_length=1000)
    published_at: datetime
    first_seen_at: datetime
    ingested_at: datetime = Field(default_factory=utcnow)
    related_symbols: tuple[str, ...] = ()
    related_projects: tuple[str, ...] = ()
    entity_matches: tuple[NewsEntityMatch, ...] = ()
    language: str = Field(default="und", min_length=2, max_length=16)
    event_type: NewsEventType = NewsEventType.UNKNOWN
    sentiment: NewsSentiment = NewsSentiment.UNKNOWN
    impact_score: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    verification_status: NewsVerificationStatus = NewsVerificationStatus.SINGLE_SOURCE
    duplicate_group: str = Field(..., min_length=1)
    content_hash: str = Field(..., min_length=64, max_length=64)
    expires_at: datetime
    risk_flags: tuple[str, ...] = ()
    data_status: DataStatus = DataStatus.OK

    _url = field_validator("canonical_url")(_http_url)
    _published_utc = field_validator("published_at")(_utc)
    _first_seen_utc = field_validator("first_seen_at")(_utc)
    _ingested_utc = field_validator("ingested_at")(_utc)
    _expires_utc = field_validator("expires_at")(_utc)
    _related_symbols = field_validator("related_symbols")(_symbols)

    @model_validator(mode="after")
    def _time_and_status_consistency(self) -> CryptoNewsRecord:
        if self.first_seen_at > self.ingested_at:
            raise ValueError("first_seen_at must not be after ingested_at")
        if self.expires_at <= self.published_at:
            raise ValueError("expires_at must be after published_at")
        if (
            self.verification_status
            in {
                NewsVerificationStatus.REJECTED,
                NewsVerificationStatus.NO_DATA,
            }
            and self.data_status is DataStatus.OK
        ):
            raise ValueError("rejected/NO_DATA news cannot have OK status")
        return self


class NewsIngestionBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collected_at: datetime = Field(default_factory=utcnow)
    records: tuple[CryptoNewsRecord, ...] = ()
    # Bounded provider envelopes so the runtime can build versioned RawNewsDocuments.
    raw_items: tuple[ProviderNewsItem, ...] = ()
    provider_errors: tuple[str, ...] = ()
    provider_health: tuple[NewsProviderHealth, ...] = ()
    coverage_by_symbol: dict[str, NewsCoverageStatus] = Field(default_factory=dict)
    coverage_status: NewsCoverageStatus = NewsCoverageStatus.PROVIDER_UNAVAILABLE
    duplicate_count: int = Field(default=0, ge=0)
    data_status: DataStatus = DataStatus.PENDING

    _collected_utc = field_validator("collected_at")(_utc)


class NewsMarketReaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reaction_id: str = Field(default_factory=lambda: uuid4().hex)
    news_id: str
    symbol: str
    as_of: datetime
    status: MarketReactionStatus
    price_move_before_bps: Decimal | None = None
    price_move_after_bps: Decimal | None = None
    volume_ratio: Decimal | None = Field(default=None, ge=0)
    spread_change_bps: Decimal | None = None
    depth_change_pct: Decimal | None = None
    order_book_imbalance_before: Decimal | None = Field(default=None, ge=-1, le=1)
    order_book_imbalance_after: Decimal | None = Field(default=None, ge=-1, le=1)
    open_interest_change_pct: Decimal | None = None
    funding_change_bps: Decimal | None = None
    relative_to_btc_bps: Decimal | None = None
    realized_volatility_pct: Decimal | None = Field(default=None, ge=0)
    reversal_from_extreme_bps: Decimal | None = Field(default=None, ge=0)
    estimated_slippage_bps: Decimal | None = Field(default=None, ge=0)
    freshness_seconds: int | None = Field(default=None, ge=0)
    remaining_effect_minutes: int | None = Field(default=None, ge=0)
    blocks_entry: bool = True
    reasons: tuple[str, ...] = ()
    data_status: DataStatus = DataStatus.PENDING

    _as_of_utc = field_validator("as_of")(_utc)


class TimeframeAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeframe: Timeframe
    as_of: datetime
    bars_used: int = Field(..., ge=0)
    provisional_bars_excluded: int = Field(default=0, ge=0)
    close_price: Decimal | None = Field(default=None, gt=0)
    return_bps: Decimal | None = None
    volume_ratio: Decimal | None = Field(default=None, ge=0)
    realized_volatility_pct: Decimal | None = Field(default=None, ge=0)
    vwap_proxy: Decimal | None = Field(default=None, gt=0)
    trend: Literal["up", "down", "flat", "no_data"] = "no_data"
    data_status: DataStatus = DataStatus.PENDING

    _as_of_utc = field_validator("as_of")(_utc)


class MultiTimeframeAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    as_of: datetime
    analyses: dict[Timeframe, TimeframeAnalysis] = Field(default_factory=dict)
    required_timeframes: tuple[Timeframe, ...] = (
        Timeframe.M1,
        Timeframe.M5,
        Timeframe.M15,
        Timeframe.H1,
    )
    aligned: bool = False
    missing_data: tuple[str, ...] = ()
    data_status: DataStatus = DataStatus.PENDING

    _as_of_utc = field_validator("as_of")(_utc)


class CoinResearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    research_id: str = Field(default_factory=lambda: uuid4().hex)
    symbol: str
    as_of: datetime
    eligibility: bool
    strategy_basis: TradeStrategyBasis = TradeStrategyBasis.NO_TRADE
    news_coverage_status: NewsCoverageStatus = NewsCoverageStatus.PROVIDER_UNAVAILABLE
    market_regime: MarketRegimeKind
    technical_score: Decimal | None = Field(default=None, ge=0, le=100)
    news_score: Decimal | None = Field(default=None, ge=0, le=100)
    news_reliability: Decimal | None = Field(default=None, ge=0, le=1)
    market_reaction: MarketReactionStatus = MarketReactionStatus.NO_DATA
    liquidity_score: Decimal | None = Field(default=None, ge=0, le=100)
    manipulation_risk: Decimal | None = Field(default=None, ge=0, le=100)
    execution_quality: Decimal | None = Field(default=None, ge=0, le=100)
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    evidence: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    valid_until: datetime
    data_status: DataStatus = DataStatus.PENDING

    _as_of_utc = field_validator("as_of")(_utc)
    _valid_until_utc = field_validator("valid_until")(_utc)

    @model_validator(mode="after")
    def _validity(self) -> CoinResearchResult:
        if self.valid_until <= self.as_of:
            raise ValueError("research valid_until must be after as_of")
        return self


class TradePlan(BaseModel):
    """Cost-aware, time-boxed trade research plan with no execution authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    mission_id: str
    symbol: str
    side: Literal["buy", "sell", "no_trade"] = "no_trade"
    thesis: str = "NO_TRADE: no evidenced thesis"
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    news_events: tuple[str, ...] = ()
    technical_conditions: tuple[str, ...] = ()
    market_reaction: MarketReactionStatus = MarketReactionStatus.NO_DATA
    strategy_basis: TradeStrategyBasis = TradeStrategyBasis.NO_TRADE
    created_at: datetime = Field(default_factory=utcnow)
    valid_until: datetime
    horizon: TradeHorizon
    expected_holding_minutes: int = Field(..., ge=0, le=120)
    entry_condition: str = Field(..., min_length=1)
    preferred_order_type: PreferredOrderType = PreferredOrderType.NONE
    expected_entry_price: Decimal | None = Field(default=None, gt=0)
    entry_zone: tuple[Decimal, Decimal] | None = None
    invalidation_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    target_prices: tuple[Decimal, ...] = ()
    take_profit_policy: str = Field(..., min_length=1)
    trailing_policy: str = Field(..., min_length=1)
    time_stop: datetime | None = None
    thesis_invalidation: str = Field(..., min_length=1)
    expected_gross_return_bps: Decimal | None = None
    expected_entry_cost_bps: Decimal | None = Field(default=None, ge=0)
    expected_exit_cost_bps: Decimal | None = Field(default=None, ge=0)
    estimated_round_trip_cost_bps: Decimal | None = Field(default=None, ge=0)
    expected_net_return_bps: Decimal | None = None
    estimated_fees_bps: Decimal | None = Field(default=None, ge=0)
    expected_slippage_bps: Decimal | None = Field(default=None, ge=0)
    spread_cost_bps: Decimal | None = Field(default=None, ge=0)
    execution_buffer_bps: Decimal | None = Field(default=None, ge=0)
    uncertainty_buffer_bps: Decimal = Field(default=Decimal("0"), ge=0)
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    news_evidence: tuple[str, ...] = ()
    technical_evidence: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    freshness: Literal["fresh", "stale", "no_data"] = "no_data"
    guard_result: Literal["allow", "blocked", "pending"] = "pending"
    cost_source: CostSource = CostSource.NO_DATA
    live_eligible: Literal[False] = False
    strategy_version: str = Field(..., min_length=1)
    research_id: str | None = None
    last_reassessed_at: datetime | None = None
    data_status: DataStatus = DataStatus.PENDING
    submission_authorized: Literal[False] = False
    maximum_holding_time: int | None = Field(default=None, ge=0, le=120)
    no_trade_reasons: tuple[str, ...] = ()
    generated_at: datetime | None = None
    expires_at: datetime | None = None
    expected_gross_edge: Decimal | None = None
    expected_net_edge: Decimal | None = None

    _created_utc = field_validator("created_at")(_utc)
    _valid_until_utc = field_validator("valid_until")(_utc)
    _time_stop_utc = field_validator("time_stop")(_optional_utc)
    _reassessed_utc = field_validator("last_reassessed_at")(_optional_utc)
    _generated_utc = field_validator("generated_at")(_optional_utc)
    _expires_utc = field_validator("expires_at")(_optional_utc)

    @model_validator(mode="after")
    def _plan_consistency(self) -> TradePlan:
        if self.valid_until <= self.created_at:
            raise ValueError("plan valid_until must be after created_at")
        if self.horizon is TradeHorizon.NO_TRADE:
            if self.expected_holding_minutes != 0:
                raise ValueError("NO_TRADE holding time must be zero")
            if self.preferred_order_type is not PreferredOrderType.NONE:
                raise ValueError("NO_TRADE cannot carry an order type")
            if self.side != "no_trade":
                raise ValueError("NO_TRADE requires side=no_trade")
        else:
            if self.strategy_basis is TradeStrategyBasis.NO_TRADE:
                raise ValueError("a trade plan must carry strategy_basis")
            if self.expected_holding_minutes < 15:
                raise ValueError("a trade plan must span at least 15 minutes")
            if self.expected_entry_price is None or self.stop_price is None:
                raise ValueError("a trade plan must carry entry and stop")
            if self.time_stop is None or self.time_stop <= self.created_at:
                raise ValueError("a trade plan must carry a valid time_stop")
            if self.data_status is not DataStatus.OK:
                raise ValueError("a trade plan can only be built from OK data")
            # ``side`` was added after the first persisted TradePlan schema.  Keep
            # historical PIT rows readable; every newly generated actionable plan
            # sets buy/sell explicitly in TradePlanner.
        return self


class MissionDailyCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str
    trading_date: date
    timezone: str = Field(default="Europe/Istanbul", min_length=1)
    as_of: datetime = Field(default_factory=utcnow)
    starting_equity_usdt: Decimal = Field(..., gt=0)
    realized_gross_pnl_usdt: Decimal = Decimal("0")
    buy_commission_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    sell_commission_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    realized_slippage_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    spread_impact_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    commission_conversion_cost_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    dust_rounding_cost_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    display_conversion_cost_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    unrealized_pnl_usdt: Decimal = Decimal("0")
    daily_profit_stop_pct: Decimal = Field(default=Decimal("10"), gt=0)
    daily_loss_stop_pct: Decimal = Field(default=Decimal("2"), gt=0)
    entry_blocked: bool = False
    pending_entries_cancel_required: bool = False
    status: Literal["running", "daily_target_reached", "daily_loss_limit"] = "running"

    _as_of_utc = field_validator("as_of")(_utc)

    @property
    def realized_costs_usdt(self) -> Decimal:
        return (
            self.buy_commission_usdt
            + self.sell_commission_usdt
            + self.realized_slippage_usdt
            + self.spread_impact_usdt
            + self.commission_conversion_cost_usdt
            + self.dust_rounding_cost_usdt
            + self.display_conversion_cost_usdt
        )

    @property
    def realized_net_pnl_usdt(self) -> Decimal:
        return self.realized_gross_pnl_usdt - self.realized_costs_usdt

    @property
    def realized_net_return_pct(self) -> Decimal:
        return self.realized_net_pnl_usdt / self.starting_equity_usdt * Decimal("100")


class DailyMissionEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str
    assessed_at: datetime = Field(default_factory=utcnow)
    action: DailyMissionAction
    realized_net_pnl_usdt: Decimal
    realized_net_return_pct: Decimal
    estimated_exit_net_pnl_usdt: Decimal | None = None
    entry_allowed: bool
    cancel_pending_entries: bool = False
    reasons: tuple[str, ...] = ()

    _assessed_utc = field_validator("assessed_at")(_utc)


__all__ = [
    "CoinResearchResult",
    "CostSource",
    "CryptoNewsRecord",
    "DailyMissionAction",
    "DailyMissionEvaluation",
    "EventDirection",
    "EventEntity",
    "EventEvidence",
    "EventSeverity",
    "EventStatus",
    "EventUrgency",
    "ExternalTechnicalSignal",
    "MarketReactionStatus",
    "MissionDailyCheckpoint",
    "MultiTimeframeAnalysis",
    "NewsCoverageStatus",
    "NewsEntityMatch",
    "NewsEventType",
    "NewsIngestionBatch",
    "NewsMarketReaction",
    "NewsProviderHealth",
    "NewsProviderHealthStatus",
    "NewsReliabilityTier",
    "NewsSentiment",
    "NewsSourceType",
    "NewsVerificationStatus",
    "NormalizedCryptoEvent",
    "PreferredOrderType",
    "ProviderNewsItem",
    "RawNewsDocument",
    "SourceAvailability",
    "SourceGroup",
    "SourceRegistryEntry",
    "SourceRetryPolicy",
    "TimeframeAnalysis",
    "TradeHorizon",
    "TradePlan",
    "TradeStrategyBasis",
]
