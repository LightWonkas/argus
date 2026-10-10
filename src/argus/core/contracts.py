"""Typed data contracts shared across layers (L1 → L2 → L3).

Source-independent common contracts: market data (``Quote``, ``OHLCVBar``,
``OHLCVSeries``), news and disclosures (``NewsItem``, ``Disclosure``) and analysis /
decision outputs (``AnalysisOutput``, ``Proposal``).

Monetary values use **Decimal** (to avoid float rounding errors).
Every external record carries a :class:`~argus.core.schemas.Provenance`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus.core.enums import DataRepresentation, EvidenceTier, MarketType, Stance, Timeframe
from argus.core.schemas import DataStatus, Provenance

# Invariant framing on every analysis output (honesty/compliance).
NOT_ADVICE = "This is analysis for decision support. Not financial advice."


def _require_tz(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return v


class Quote(BaseModel):
    """Spot price quote."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(..., min_length=1, description="Instrument, e.g. 'BTC/USDT'")
    price: Decimal = Field(..., gt=0, description="Last traded price")
    quote_currency: str = Field(..., min_length=1, description="Quote currency, e.g. USDT/USD/TRY")
    change_pct_24h: float | None = Field(default=None, description="24h change in percent")
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    base_volume_24h: Decimal | None = Field(default=None, ge=0)
    provenance: Provenance


class OHLCVBar(BaseModel):
    """A single OHLCV candle (point-in-time data point)."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime = Field(..., description="Candle open time (tz-aware, UTC)")
    open: Decimal = Field(..., gt=0)
    high: Decimal = Field(..., gt=0)
    low: Decimal = Field(..., gt=0)
    close: Decimal = Field(..., gt=0)
    volume: Decimal = Field(..., ge=0)

    _check_tz = field_validator("timestamp")(_require_tz)

    @model_validator(mode="after")
    def _ohlc_consistency(self) -> OHLCVBar:
        """OHLC consistency: low ≤ {open, close} ≤ high and low ≤ high."""
        if self.low > self.high:
            raise ValueError(f"low ({self.low}) > high ({self.high})")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open ({self.open}) outside [low={self.low}, high={self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close ({self.close}) outside [low={self.low}, high={self.high}]")
        return self


class NewsItem(BaseModel):
    """Generic news item (news aggregators / RSS / APIs)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(..., min_length=1, description="News id, unique within the source")
    title: str = Field(..., min_length=1)
    published_at: datetime = Field(..., description="Publication time (tz-aware, UTC)")
    url: str | None = None
    summary: str | None = None
    symbols: tuple[str, ...] = Field(default_factory=tuple, description="Related instruments")
    source_name: str | None = Field(default=None, description="News source, e.g. 'Reuters'")
    sentiment: float | None = Field(default=None, ge=-1.0, le=1.0, description="Sentiment [-1..1]")
    provenance: Provenance

    _check_tz = field_validator("published_at")(_require_tz)


class Disclosure(BaseModel):
    """Official company disclosure (Turkey: KAP / global: SEC EDGAR and the like)."""

    model_config = ConfigDict(frozen=True)

    disclosure_id: str = Field(
        ..., min_length=1, description="Disclosure id (e.g. KAP disclosure number)"
    )
    symbol: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    published_at: datetime = Field(..., description="Disclosure time (tz-aware, UTC)")
    url: str | None = None
    category: str | None = Field(
        default=None, description="Type: financials/dividend/material event …"
    )
    provenance: Provenance

    _check_tz = field_validator("published_at")(_require_tz)


class Scenario(BaseModel):
    """An analysis scenario (base / alternative / wait)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        ..., min_length=1, description="Scenario name, e.g. 'Base', 'Alternative', 'Wait'"
    )
    probability_pct: int = Field(..., ge=0, le=100)
    detail: str = Field(..., min_length=1)


class AnalysisOutput(BaseModel):
    """L3/Brain analysis output — **not advice**.

    Shape: ``{ summary, evidence tier [A/B/C], confidence %, invalidation, scenarios }``.
    Without data ``summary`` may be :data:`~argus.core.schemas.DATA_PENDING` (honesty).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(..., min_length=1)
    timeframe: Timeframe
    summary: str = Field(..., min_length=1, description="Executive summary")
    bias: str | None = Field(default=None, description="bullish | bearish | neutral")
    evidence_tier: EvidenceTier = Field(..., description="Dominant evidence tier [A]/[B]/[C]")
    confidence_pct: int = Field(..., ge=0, le=100, description="Confidence in percent")
    invalidation: str = Field(..., min_length=1, description="Invalidation condition")
    scenarios: tuple[Scenario, ...] = Field(default_factory=tuple)
    model: str = Field(..., description="LLM model that produced it")
    disclaimer: str = NOT_ADVICE


class OpinionRef(BaseModel):
    """Summary of one agent opinion (a Proposal contributor — for transparency)."""

    model_config = ConfigDict(frozen=True)

    agent: str
    stance: Stance
    confidence_pct: int = Field(..., ge=0, le=100)


class Proposal(BaseModel):
    """L4 Signal Aggregator output — the confluence synthesis of agent opinions.

    Shape: ``Proposal{direction, strength, horizon, rationale}``. There is **NO execution** —
    this is a decision-support proposal only and **not financial advice**.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(..., min_length=1)
    timeframe: Timeframe
    direction: Stance
    strength: int = Field(
        ...,
        ge=0,
        le=100,
        description="Conviction strength of the direction (confidence of agreeing agents)",
    )
    confluence_score: int = Field(
        ..., ge=0, le=100, description="Share of agents agreeing on the direction"
    )
    rationale: str
    contributors: tuple[OpinionRef, ...] = Field(default_factory=tuple)
    data_status: DataStatus = DataStatus.OK
    disclaimer: str = NOT_ADVICE


class OHLCVSeries(BaseModel):
    """Ordered OHLCV candle series for one instrument + timeframe."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(..., min_length=1)
    timeframe: Timeframe
    bars: tuple[OHLCVBar, ...] = Field(default_factory=tuple)
    provenance: Provenance
    exchange: str | None = None
    market_type: MarketType | None = None
    sequence_start: int | None = Field(default=None, ge=0)
    sequence_end: int | None = Field(default=None, ge=0)
    data_status: DataStatus = DataStatus.OK
    representation: DataRepresentation = DataRepresentation.TRANSFORMED

    @model_validator(mode="after")
    def _sequence_consistency(self) -> OHLCVSeries:
        if (
            self.sequence_start is not None
            and self.sequence_end is not None
            and self.sequence_start > self.sequence_end
        ):
            raise ValueError("sequence_start must not be greater than sequence_end")
        return self

    @property
    def event_timestamp(self) -> datetime:
        return self.latest.timestamp if self.latest is not None else self.provenance.timestamp

    @property
    def ingestion_timestamp(self) -> datetime:
        return self.provenance.ingested_at

    @property
    def latest(self) -> OHLCVBar | None:
        """Latest candle (if any)."""
        return self.bars[-1] if self.bars else None

    def closes(self) -> list[Decimal]:
        """Close series (for indicator calculations)."""
        return [bar.close for bar in self.bars]
