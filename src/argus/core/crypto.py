"""Crypto-specific typed contracts.

Records are independent of provider SDKs. Every external market record directly carries
exchange/market/symbol, event and ingestion time, source, confidence, freshness, sequence
(if any), data status and the raw/transformed distinction. Monetary values use ``Decimal``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus.core.contracts import OHLCVSeries
from argus.core.enums import (
    DataRepresentation,
    MarketRegimeKind,
    MarketType,
    OrderSide,
    RiskDecision,
    ShadowOrderStatus,
    ShadowPositionStatus,
    Timeframe,
)
from argus.core.schemas import DataStatus, FreshnessLabel, utcnow


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return v.astimezone(UTC)


def _optional_utc(v: datetime | None) -> datetime | None:
    return None if v is None else _utc(v)


class CryptoMarketRecord(BaseModel):
    """Common, direct metadata envelope of every external crypto market record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str = Field(..., min_length=1)
    market_type: MarketType
    symbol: str = Field(..., min_length=1)
    event_timestamp: datetime
    ingestion_timestamp: datetime = Field(default_factory=utcnow)
    source: str = Field(..., min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    freshness: FreshnessLabel = FreshnessLabel.FRESH
    sequence: int | None = Field(default=None, ge=0)
    data_status: DataStatus = DataStatus.OK
    representation: DataRepresentation = DataRepresentation.TRANSFORMED

    _event_utc = field_validator("event_timestamp")(_utc)
    _ingestion_utc = field_validator("ingestion_timestamp")(_utc)


class SpotTicker(CryptoMarketRecord):
    market_type: Literal[MarketType.SPOT] = MarketType.SPOT
    last_price: Decimal = Field(..., gt=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    base_volume_24h: Decimal | None = Field(default=None, ge=0)
    quote_volume_24h: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _book_consistency(self) -> SpotTicker:
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("spot ticker bid must not be greater than ask")
        return self


class FuturesTicker(CryptoMarketRecord):
    market_type: Literal[MarketType.PERPETUAL] = MarketType.PERPETUAL
    last_price: Decimal = Field(..., gt=0)
    mark_price: Decimal | None = Field(default=None, gt=0)
    index_price: Decimal | None = Field(default=None, gt=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    open_interest: Decimal | None = Field(default=None, ge=0)
    funding_rate: Decimal | None = None
    next_funding_timestamp: datetime | None = None

    _funding_utc = field_validator("next_funding_timestamp")(_optional_utc)

    @model_validator(mode="after")
    def _book_consistency(self) -> FuturesTicker:
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("futures ticker bid must not be greater than ask")
        return self


class Trade(CryptoMarketRecord):
    trade_id: str = Field(..., min_length=1)
    side: OrderSide
    price: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., gt=0)
    is_buyer_maker: bool | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    price: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., ge=0)


class OrderBookSnapshot(CryptoMarketRecord):
    bids: tuple[OrderBookLevel, ...] = Field(default_factory=tuple)
    asks: tuple[OrderBookLevel, ...] = Field(default_factory=tuple)

    @property
    def best_bid(self) -> Decimal | None:
        return max((level.price for level in self.bids if level.quantity > 0), default=None)

    @property
    def best_ask(self) -> Decimal | None:
        return min((level.price for level in self.asks if level.quantity > 0), default=None)

    @property
    def crossed(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid >= self.best_ask
        )


class OrderBookDelta(CryptoMarketRecord):
    first_sequence: int = Field(..., ge=0)
    final_sequence: int = Field(..., ge=0)
    previous_final_sequence: int | None = Field(default=None, ge=0)
    bids: tuple[OrderBookLevel, ...] = Field(default_factory=tuple)
    asks: tuple[OrderBookLevel, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _sequence_order(self) -> OrderBookDelta:
        if self.first_sequence > self.final_sequence:
            raise ValueError("first_sequence must not be greater than final_sequence")
        return self


class FundingRate(CryptoMarketRecord):
    market_type: Literal[MarketType.PERPETUAL] = MarketType.PERPETUAL
    rate: Decimal
    next_funding_timestamp: datetime | None = None

    _next_utc = field_validator("next_funding_timestamp")(_optional_utc)


class OpenInterest(CryptoMarketRecord):
    market_type: Literal[MarketType.PERPETUAL] = MarketType.PERPETUAL
    contracts: Decimal | None = Field(default=None, ge=0)
    base_value: Decimal | None = Field(default=None, ge=0)
    quote_value: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _at_least_one_value(self) -> OpenInterest:
        if self.contracts is None and self.base_value is None and self.quote_value is None:
            raise ValueError("open interest requires at least one value")
        return self


class BasisSnapshot(CryptoMarketRecord):
    market_type: Literal[MarketType.PERPETUAL] = MarketType.PERPETUAL
    spot_price: Decimal = Field(..., gt=0)
    perpetual_price: Decimal = Field(..., gt=0)

    @property
    def basis(self) -> Decimal:
        return self.perpetual_price - self.spot_price

    @property
    def basis_pct(self) -> Decimal:
        return self.basis / self.spot_price * Decimal("100")


class LongShortRatio(CryptoMarketRecord):
    market_type: Literal[MarketType.PERPETUAL] = MarketType.PERPETUAL
    long_ratio: Decimal = Field(..., ge=0)
    short_ratio: Decimal = Field(..., ge=0)
    account_ratio: Decimal | None = Field(default=None, ge=0)


class LiquidationEvent(CryptoMarketRecord):
    market_type: Literal[MarketType.PERPETUAL] = MarketType.PERPETUAL
    liquidation_id: str = Field(..., min_length=1)
    side: OrderSide
    price: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., gt=0)

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


FeatureValue = Decimal | float | int | bool | str | None


class FeatureSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1)
    timeframe: Timeframe
    as_of: datetime
    values: dict[str, FeatureValue] = Field(default_factory=dict)
    evidence_sources: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    missing_data: tuple[str, ...] = ()
    data_status: DataStatus = DataStatus.OK

    _as_of_utc = field_validator("as_of")(_utc)


class MarketRegime(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1)
    as_of: datetime
    regime: MarketRegimeKind
    confidence_pct: int = Field(..., ge=0, le=100)
    features_used: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    data_status: DataStatus = DataStatus.OK

    _as_of_utc = field_validator("as_of")(_utc)


class CryptoAnalysisContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1)
    as_of: datetime
    spot: SpotTicker | None = None
    futures: FuturesTicker | None = None
    series_by_timeframe: dict[Timeframe, OHLCVSeries] = Field(default_factory=dict)
    order_book: OrderBookSnapshot | None = None
    funding: FundingRate | None = None
    open_interest: OpenInterest | None = None
    basis: BasisSnapshot | None = None
    long_short_ratio: LongShortRatio | None = None
    liquidations: tuple[LiquidationEvent, ...] = ()
    features: FeatureSnapshot | None = None
    regime: MarketRegime | None = None
    onchain_features: dict[str, FeatureValue] = Field(default_factory=dict)
    news_facts: tuple[str, ...] = ()
    macro_features: dict[str, FeatureValue] = Field(default_factory=dict)
    event_risk: bool = False
    missing_data: tuple[str, ...] = ()
    data_status: DataStatus = DataStatus.PENDING

    _as_of_utc = field_validator("as_of")(_utc)


class RiskAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(..., min_length=1)
    assessed_at: datetime = Field(default_factory=utcnow)
    decision: RiskDecision = RiskDecision.REJECT
    reasons: tuple[str, ...] = ()
    checks: dict[str, bool | float | str | None] = Field(default_factory=dict)
    human_review_required: bool = True

    _assessed_utc = field_validator("assessed_at")(_utc)


class TradeCandidate(BaseModel):
    """Spot-only decision-support candidate with no execution authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(default_factory=lambda: uuid4().hex)
    symbol: str = Field(..., min_length=1)
    market_type: Literal[MarketType.SPOT] = MarketType.SPOT
    side: OrderSide
    created_at: datetime = Field(default_factory=utcnow)
    horizon: Timeframe
    entry_reference: Decimal = Field(..., gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    invalidation: str = Field(..., min_length=1)
    evidence_source_ids: tuple[str, ...] = Field(..., min_length=1)
    confidence_pct: int = Field(..., ge=0, le=100)
    data_status: Literal[DataStatus.OK] = DataStatus.OK
    leverage: Decimal = Decimal("0")
    execution_enabled: Literal[False] = False
    human_approval_required: Literal[True] = True

    _created_utc = field_validator("created_at")(_utc)
    _expires_utc = field_validator("expires_at")(_optional_utc)

    @model_validator(mode="after")
    def _safe_boundary(self) -> TradeCandidate:
        if self.leverage != 0:
            raise ValueError("leverage must be zero in the current scope")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class ShadowOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str = Field(default_factory=lambda: uuid4().hex)
    candidate_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: OrderSide
    quantity: Decimal = Field(..., gt=0)
    submitted_at: datetime = Field(default_factory=utcnow)
    limit_price: Decimal | None = Field(default=None, gt=0)
    status: ShadowOrderStatus = ShadowOrderStatus.PENDING
    filled_quantity: Decimal = Field(default=Decimal("0"), ge=0)

    _submitted_utc = field_validator("submitted_at")(_utc)

    @model_validator(mode="after")
    def _fill_not_above_order(self) -> ShadowOrder:
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must not exceed the order quantity")
        return self


class ShadowFill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_id: str = Field(default_factory=lambda: uuid4().hex)
    order_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: OrderSide
    filled_at: datetime = Field(default_factory=utcnow)
    price: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., gt=0)
    commission: Decimal = Field(default=Decimal("0"), ge=0)
    slippage: Decimal = Decimal("0")
    funding_cost: Decimal = Decimal("0")

    _filled_utc = field_validator("filled_at")(_utc)


class ShadowPosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    position_id: str = Field(default_factory=lambda: uuid4().hex)
    candidate_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: OrderSide
    status: ShadowPositionStatus = ShadowPositionStatus.OPEN
    opened_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    quantity: Decimal = Field(..., gt=0)
    average_entry_price: Decimal = Field(..., gt=0)
    mark_price: Decimal = Field(..., gt=0)
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    funding_cost: Decimal = Decimal("0")
    stop_price: Decimal | None = Field(default=None, gt=0)
    invalidation: str = Field(default="manual verification", min_length=1)
    expires_at: datetime | None = None

    _opened_utc = field_validator("opened_at")(_utc)
    _closed_utc = field_validator("closed_at")(_optional_utc)
    _expires_utc = field_validator("expires_at")(_optional_utc)

    @model_validator(mode="after")
    def _closed_state_consistency(self) -> ShadowPosition:
        if self.status is ShadowPositionStatus.CLOSED and self.closed_at is None:
            raise ValueError("a closed shadow position requires closed_at")
        if self.status is ShadowPositionStatus.OPEN and self.closed_at is not None:
            raise ValueError("an open shadow position must not have closed_at")
        return self
