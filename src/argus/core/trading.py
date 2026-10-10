"""Execution-independent typed contracts for the autonomous research/mission domain.

This module contains no exchange client. In particular ``OrderIntent`` is bounded by
``submission_authorized=False``; the existence of the LIVE_SPOT enum value does not
create a code path that can submit orders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from argus.core.enums import OrderSide
from argus.core.schemas import DataStatus, utcnow


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be tz-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


class TradingMode(StrEnum):
    PUBLIC_MONITOR = "public_monitor"
    SHADOW = "shadow"
    TESTNET = "testnet"
    LIVE_SPOT = "live_spot"


class MissionStatus(StrEnum):
    CREATED = "created"
    DISARMED = "disarmed"
    ARMING = "arming"
    ARMED = "armed"
    RUNNING = "running"
    DAILY_TARGET_REACHED = "daily_target_reached"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    EXIT_ONLY = "exit_only"
    HALTING = "halting"
    HALTED = "halted"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED_SAFE = "failed_safe"


class MissionEndPolicy(StrEnum):
    CLOSE_ALL = "close_all"
    MANAGE_TO_EXIT = "manage_to_exit"
    LEAVE_POSITIONS = "leave_positions"


class ResearchStatus(StrEnum):
    OK = "ok"
    NO_DATA = "no_data"
    STALE = "stale"
    REJECT = "reject"
    ERROR = "error"


class AssetCategory(StrEnum):
    CRYPTO = "crypto"
    STABLECOIN = "stablecoin"
    LEVERAGED_TOKEN = "leveraged_token"
    FIAT = "fiat"
    UNKNOWN = "unknown"


class UniverseRejectReason(StrEnum):
    QUOTE_NOT_USDT = "quote_not_usdt"
    NOT_TRADING = "not_trading"
    SPOT_NOT_ALLOWED = "spot_not_allowed"
    STABLECOIN = "stablecoin"
    LEVERAGED_TOKEN = "leveraged_token"
    BLACKLISTED = "blacklisted"
    NEW_LISTING = "new_listing"
    LISTING_AGE_UNKNOWN = "listing_age_unknown"
    INSUFFICIENT_HISTORY = "insufficient_history"
    LOW_LIQUIDITY = "low_liquidity"
    HIGH_SPREAD = "high_spread"
    LOW_DEPTH = "low_depth"
    HIGH_SLIPPAGE = "high_slippage"
    MIN_NOTIONAL_INCOMPATIBLE = "min_notional_incompatible"
    DATA_STALE = "data_stale"
    DATA_QUARANTINED = "data_quarantined"
    DATA_REJECTED = "data_rejected"
    MARKET_ANOMALY = "market_anomaly"
    FILTERS_UNKNOWN = "filters_unknown"
    METRIC_NO_DATA = "metric_no_data"


class FeeSource(StrEnum):
    ACTUAL_FILL = "actual_fill"
    ACCOUNT_VERIFIED = "account_verified"
    CONFIG_CONSERVATIVE = "config_conservative"
    NO_DATA = "no_data"


class AllocationStatus(StrEnum):
    PROPOSED = "proposed"
    REJECTED = "rejected"


class MissionGuardDecision(StrEnum):
    ALLOW_INTENT = "allow_intent"
    BLOCK_NEW_ENTRIES = "block_new_entries"
    EXIT_ONLY = "exit_only"
    HALTED = "halted"
    FAILED_SAFE = "failed_safe"


class CircuitBreakerLevel(StrEnum):
    WARN = "warn"
    BLOCK_NEW_ENTRIES = "block_new_entries"
    EXIT_ONLY = "exit_only"
    HALTED = "halted"
    FAILED_SAFE = "failed_safe"


class OrderIntentStatus(StrEnum):
    PLANNED = "planned"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class LedgerEntryKind(StrEnum):
    CAPITAL_ALLOCATED = "capital_allocated"
    CASH_RESERVED = "cash_reserved"
    CASH_RELEASED = "cash_released"
    BUY_FILL = "buy_fill"
    SELL_FILL = "sell_fill"
    COMMISSION = "commission"
    SLIPPAGE = "slippage"
    REALIZED_PNL = "realized_pnl"
    MARK_TO_MARKET = "mark_to_market"


class ExchangeFilterSet(BaseModel):
    """Normalised view of the Binance price/quantity/notional filters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_tick: Decimal | None = Field(default=None, gt=0)
    min_price: Decimal | None = Field(default=None, ge=0)
    max_price: Decimal | None = Field(default=None, gt=0)
    lot_step: Decimal | None = Field(default=None, gt=0)
    min_quantity: Decimal | None = Field(default=None, ge=0)
    max_quantity: Decimal | None = Field(default=None, gt=0)
    min_notional: Decimal | None = Field(default=None, ge=0)
    max_notional: Decimal | None = Field(default=None, gt=0)

    @property
    def complete_for_spot_order(self) -> bool:
        return (
            self.price_tick is not None
            and self.lot_step is not None
            and self.min_notional is not None
        )


class AssetTaxonomyEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset: str = Field(..., min_length=1)
    category: AssetCategory
    source: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    aliases: tuple[str, ...] = ()

    @field_validator("asset")
    @classmethod
    def _asset_upper(cls, value: str) -> str:
        return value.strip().upper()


class UniverseMarketObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1)
    base_asset: str = Field(..., min_length=1)
    quote_asset: str = Field(..., min_length=1)
    exchange_status: str = Field(..., min_length=1)
    spot_allowed: bool
    observed_at: datetime = Field(default_factory=utcnow)
    listing_time: datetime | None = None
    listing_time_source: str | None = None
    quote_volume_24h: Decimal | None = Field(default=None, ge=0)
    trade_count_24h: int | None = Field(default=None, ge=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    estimated_depth_quote: Decimal | None = Field(default=None, ge=0)
    estimated_slippage_bps: Decimal | None = Field(default=None, ge=0)
    realized_volatility_pct: Decimal | None = Field(default=None, ge=0)
    history_bars: int | None = Field(default=None, ge=0)
    filters: ExchangeFilterSet | None = None
    data_status: DataStatus = DataStatus.PENDING
    anomaly_flags: tuple[str, ...] = ()

    _observed_utc = field_validator("observed_at")(_utc)
    _listing_utc = field_validator("listing_time")(_optional_utc)

    @field_validator("symbol", "base_asset", "quote_asset", "exchange_status")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spread_bps(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / Decimal("2")
        return (self.ask - self.bid) / mid * Decimal("10000")


class UniverseEligibilitySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    base_asset: str
    quote_asset: str
    discovered_at: datetime
    eligible: bool
    rejection_reasons: tuple[UniverseRejectReason, ...] = ()
    listing_age_days: Decimal | None = Field(default=None, ge=0)
    listing_age_source: str | None = None
    quote_volume_24h: Decimal | None = Field(default=None, ge=0)
    trade_count_24h: int | None = Field(default=None, ge=0)
    spread_bps: Decimal | None = Field(default=None, ge=0)
    estimated_depth_quote: Decimal | None = Field(default=None, ge=0)
    estimated_slippage_bps: Decimal | None = Field(default=None, ge=0)
    realized_volatility_pct: Decimal | None = Field(default=None, ge=0)
    history_bars: int | None = Field(default=None, ge=0)
    filters: ExchangeFilterSet | None = None
    quality_status: DataStatus
    taxonomy_category: AssetCategory
    policy_version: str

    _discovered_utc = field_validator("discovered_at")(_utc)

    @model_validator(mode="after")
    def _decision_consistent(self) -> UniverseEligibilitySnapshot:
        if self.eligible and self.rejection_reasons:
            raise ValueError("an eligible symbol cannot carry a rejection reason")
        if not self.eligible and not self.rejection_reasons:
            raise ValueError("a rejected symbol must carry at least one reason")
        return self


class SymbolResearchScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    as_of: datetime
    status: ResearchStatus
    eligibility_score: Decimal | None = Field(default=None, ge=0, le=100)
    liquidity_score: Decimal | None = Field(default=None, ge=0, le=100)
    trend_score: Decimal | None = Field(default=None, ge=0, le=100)
    momentum_score: Decimal | None = Field(default=None, ge=0, le=100)
    order_flow_score: Decimal | None = Field(default=None, ge=0, le=100)
    regime_compatibility_score: Decimal | None = Field(default=None, ge=0, le=100)
    execution_quality_score: Decimal | None = Field(default=None, ge=0, le=100)
    risk_score: Decimal | None = Field(default=None, ge=0, le=100)
    manipulation_risk_score: Decimal | None = Field(default=None, ge=0, le=100)
    cost_adjusted_edge_score: Decimal | None = Field(default=None, ge=0, le=100)
    portfolio_fit_score: Decimal | None = Field(default=None, ge=0, le=100)
    final_research_score: Decimal | None = Field(default=None, ge=0, le=100)
    evidence: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    data_sources: tuple[str, ...] = ()
    data_freshness_s: Decimal | None = Field(default=None, ge=0)
    model_or_rule_version: str = Field(..., min_length=1)

    _as_of_utc = field_validator("as_of")(_utc)


class CommissionSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    maker_rate: Decimal | None = Field(default=None, ge=0, le=1)
    taker_rate: Decimal | None = Field(default=None, ge=0, le=1)
    source: FeeSource = FeeSource.NO_DATA
    fetched_at: datetime
    valid_until: datetime
    discount_verified: bool = False
    commission_asset: str | None = None

    _fetched_utc = field_validator("fetched_at")(_utc)
    _valid_utc = field_validator("valid_until")(_utc)

    @model_validator(mode="after")
    def _complete_or_no_data(self) -> CommissionSchedule:
        if self.valid_until <= self.fetched_at:
            raise ValueError("commission valid_until must be in the future")
        if self.source is FeeSource.NO_DATA:
            if self.maker_rate is not None or self.taker_rate is not None:
                raise ValueError("a NO_DATA commission cannot carry rates")
        elif self.maker_rate is None or self.taker_rate is None:
            raise ValueError("a verified/config commission must carry both rates")
        if self.discount_verified and self.source not in {
            FeeSource.ACCOUNT_VERIFIED,
            FeeSource.ACTUAL_FILL,
        }:
            raise ValueError("a commission discount can only be verified from account/fill data")
        return self


class BookFillEstimate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    side: OrderSide
    requested_quantity: Decimal = Field(..., gt=0)
    filled_quantity: Decimal = Field(..., ge=0)
    average_price: Decimal | None = Field(default=None, gt=0)
    best_price: Decimal | None = Field(default=None, gt=0)
    notional: Decimal = Field(default=Decimal("0"), ge=0)
    slippage_bps: Decimal | None = Field(default=None, ge=0)
    fully_fillable: bool


class CostEstimate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: Decimal = Field(..., gt=0)
    expected_buy_fill_price: Decimal = Field(..., gt=0)
    expected_sell_fill_price: Decimal = Field(..., gt=0)
    expected_entry_commission_quote: Decimal = Field(..., ge=0)
    expected_exit_commission_quote: Decimal = Field(..., ge=0)
    estimated_slippage_quote: Decimal = Field(..., ge=0)
    spread_cost_quote: Decimal = Field(..., ge=0)
    additional_execution_buffer: Decimal = Field(..., ge=0)
    expected_entry_cost: Decimal = Field(..., gt=0)
    expected_exit_proceeds: Decimal = Field(..., ge=0)
    expected_net_pnl: Decimal
    expected_net_return: Decimal
    expected_net_edge_bps: Decimal
    minimum_required_edge_bps: Decimal = Field(..., ge=0)
    passes_edge: bool
    fee_source: FeeSource
    calculated_at: datetime = Field(default_factory=utcnow)

    _calculated_utc = field_validator("calculated_at")(_utc)


class MissionInventoryItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: Decimal = Field(..., ge=0)
    average_cost: Decimal = Field(..., ge=0)
    mark_price: Decimal = Field(..., ge=0)
    provenance_fill_ids: tuple[str, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.mark_price


class MissionLedgerSnapshot(BaseModel):
    """Mission capital envelope built only from fills verified by Argus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str
    as_of: datetime = Field(default_factory=utcnow)
    capital_limit: Decimal = Field(..., gt=0)
    available_cash: Decimal = Field(..., ge=0)
    reserved_cash: Decimal = Field(default=Decimal("0"), ge=0)
    deployed_capital: Decimal = Field(default=Decimal("0"), ge=0)
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    commissions: Decimal = Field(default=Decimal("0"), ge=0)
    slippage_cost: Decimal = Field(default=Decimal("0"), ge=0)
    mission_inventory: tuple[MissionInventoryItem, ...] = ()
    external_inventory_symbols: tuple[str, ...] = ()

    _as_of_utc = field_validator("as_of")(_utc)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def inventory_market_value(self) -> Decimal:
        return sum((item.market_value for item in self.mission_inventory), Decimal("0"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mission_equity(self) -> Decimal:
        return self.available_cash + self.reserved_cash + self.inventory_market_value

    @model_validator(mode="after")
    def _capital_envelope(self) -> MissionLedgerSnapshot:
        committed = self.available_cash + self.reserved_cash + self.deployed_capital
        # Realized PnL may increase or decrease the envelope; no new external capital may appear.
        allowed = self.capital_limit + self.realized_pnl
        if committed > max(allowed, Decimal("0")):
            raise ValueError("mission capital envelope exceeded")
        mission_symbols = {item.symbol for item in self.mission_inventory if item.quantity > 0}
        if mission_symbols.intersection(self.external_inventory_symbols):
            raise ValueError(
                "mission and external inventory cannot be separated for the same symbol"
            )
        return self


class TradingMission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str = Field(default_factory=lambda: uuid4().hex)
    status: MissionStatus = MissionStatus.DISARMED
    mode: TradingMode = TradingMode.PUBLIC_MONITOR
    quote_asset: Literal["USDT"] = "USDT"
    display_currency: Literal["TRY"] = "TRY"
    input_budget_try: Decimal | None = Field(default=None, gt=0)
    try_per_usdt: Decimal | None = Field(default=None, gt=0)
    conversion_rate_source: str | None = None
    conversion_rate_at: datetime | None = None
    capital_limit: Decimal = Field(..., gt=0)
    starting_equity: Decimal = Field(..., gt=0)
    available_mission_cash: Decimal = Field(..., ge=0)
    capital_limit_usdt: Decimal | None = Field(default=None, gt=0)
    starting_equity_usdt: Decimal | None = Field(default=None, gt=0)
    current_equity_usdt: Decimal | None = Field(default=None, ge=0)
    available_cash_usdt: Decimal | None = Field(default=None, ge=0)
    daily_starting_equity_usdt: Decimal | None = Field(default=None, gt=0)
    daily_realized_net_pnl: Decimal = Decimal("0")
    daily_profit_stop_pct: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    daily_loss_stop_pct: Decimal = Field(default=Decimal("2"), gt=0, le=100)
    maximum_drawdown_pct: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    timezone: str = Field(default="Europe/Istanbul", min_length=1)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    scheduled_end_at: datetime
    actual_end_at: datetime | None = None
    risk_policy_version: str = "risk-v1"
    strategy_version: str = "research-v1"
    universe_policy_version: str = "universe-v1"
    fee_policy_version: str = "fee-v1"
    end_policy: MissionEndPolicy = MissionEndPolicy.MANAGE_TO_EXIT
    armed_at: datetime | None = None
    halted_at: datetime | None = None
    halt_reason: str | None = None
    last_reconciled_at: datetime | None = None
    revision: int = Field(default=1, ge=1)

    _created_utc = field_validator("created_at")(_utc)
    _conversion_utc = field_validator("conversion_rate_at")(_optional_utc)
    _started_utc = field_validator("started_at")(_optional_utc)
    _scheduled_utc = field_validator("scheduled_end_at")(_utc)
    _actual_utc = field_validator("actual_end_at")(_optional_utc)
    _armed_utc = field_validator("armed_at")(_optional_utc)
    _halted_utc = field_validator("halted_at")(_optional_utc)
    _reconciled_utc = field_validator("last_reconciled_at")(_optional_utc)

    @model_validator(mode="after")
    def _mission_consistency(self) -> TradingMission:
        if self.starting_equity > self.capital_limit:
            raise ValueError("starting_equity must not exceed capital_limit")
        if self.available_mission_cash > self.capital_limit:
            raise ValueError("available mission cash must not exceed the capital limit")
        for alias, legacy in (
            (self.capital_limit_usdt, self.capital_limit),
            (self.starting_equity_usdt, self.starting_equity),
            (self.available_cash_usdt, self.available_mission_cash),
        ):
            if alias is not None and alias != legacy:
                raise ValueError(
                    "USDT mission fields must match the backwards-compatible capital fields"
                )
        if self.current_equity_usdt is not None and self.current_equity_usdt < 0:
            raise ValueError("current_equity_usdt must not be negative")
        if (self.input_budget_try is None) != (self.try_per_usdt is None):
            raise ValueError("the TRY reference budget and conversion rate must be set together")
        if self.try_per_usdt is not None and (
            not self.conversion_rate_source or self.conversion_rate_at is None
        ):
            raise ValueError("the TRY/USDT conversion must carry a source and a time")
        if self.scheduled_end_at <= self.created_at:
            raise ValueError("scheduled_end_at must be after created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not be before created_at")
        if self.actual_end_at is not None and self.actual_end_at < self.created_at:
            raise ValueError("actual_end_at must not be before created_at")
        if self.halted_at is not None and self.halted_at < self.created_at:
            raise ValueError("halted_at must not be before created_at")
        return self


class ArmPreflight(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    explicit_user_arm: bool = False
    live_execution_enabled: bool = False
    live_environment_confirmed: bool = False
    account_readable: bool = False
    spot_trade_permission: bool = False
    withdrawals_disabled_verified: bool = False
    transfers_disabled_verified: bool = False
    expected_account_verified: bool = False
    reconciliation_ok: bool = False
    commission_current: bool = False
    user_stream_healthy: bool = False
    clock_skew_ok: bool = False
    leader_lease_held: bool = False
    storage_healthy: bool = False


class AllocationProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str
    symbol: str
    status: AllocationStatus
    quantity: Decimal = Field(default=Decimal("0"), ge=0)
    reference_price: Decimal = Field(..., gt=0)
    stop_price: Decimal = Field(..., gt=0)
    notional: Decimal = Field(default=Decimal("0"), ge=0)
    risk_amount: Decimal = Field(default=Decimal("0"), ge=0)
    fee_reserve: Decimal = Field(default=Decimal("0"), ge=0)
    reasons: tuple[str, ...] = ()
    policy_version: str

    @model_validator(mode="after")
    def _status_consistency(self) -> AllocationProposal:
        if self.status is AllocationStatus.PROPOSED and self.quantity <= 0:
            raise ValueError("a proposed allocation must carry a positive quantity")
        if self.status is AllocationStatus.REJECTED and not self.reasons:
            raise ValueError("a rejected allocation must carry a reason")
        return self


class MissionGuardResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(default_factory=lambda: uuid4().hex)
    mission_id: str
    symbol: str
    assessed_at: datetime = Field(default_factory=utcnow)
    decision: MissionGuardDecision
    checks: dict[str, bool | str | Decimal | None]
    reasons: tuple[str, ...]

    _assessed_utc = field_validator("assessed_at")(_utc)


class ExitPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_stop: Decimal = Field(..., gt=0)
    take_profit_policy: str = Field(..., min_length=1)
    trailing_policy: str = Field(..., min_length=1)
    time_stop: datetime
    thesis_invalidation: str = Field(..., min_length=1)
    maximum_holding_seconds: int = Field(..., gt=0)
    emergency_exit_policy: str = Field(..., min_length=1)

    _time_stop_utc = field_validator("time_stop")(_utc)


class OrderIntent(BaseModel):
    """Persistent intent; it can never carry submission authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str = Field(default_factory=lambda: uuid4().hex)
    client_order_id: str = Field(..., min_length=1, max_length=36)
    mission_id: str
    candidate_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal = Field(..., gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    mode: TradingMode
    status: OrderIntentStatus = OrderIntentStatus.PLANNED
    created_at: datetime = Field(default_factory=utcnow)
    owner_id: str
    fencing_token: int = Field(..., gt=0)
    guard_result_id: str
    exit_plan: ExitPlan
    submission_authorized: Literal[False] = False

    _created_utc = field_validator("created_at")(_utc)


class MissionLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(default_factory=lambda: uuid4().hex)
    mission_id: str
    kind: LedgerEntryKind
    occurred_at: datetime = Field(default_factory=utcnow)
    amount_quote: Decimal
    symbol: str | None = None
    quantity: Decimal | None = None
    exchange_ref: str | None = None
    idempotency_key: str
    metadata: dict[str, str] = Field(default_factory=dict)

    _occurred_utc = field_validator("occurred_at")(_utc)


class MissionAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    mission_id: str
    event_type: str = Field(..., min_length=1)
    occurred_at: datetime = Field(default_factory=utcnow)
    idempotency_key: str = Field(..., min_length=1)
    details: dict[str, str] = Field(default_factory=dict)

    _occurred_utc = field_validator("occurred_at")(_utc)


class LeaderLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str
    owner_id: str
    fencing_token: int = Field(..., gt=0)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    _acquired_utc = field_validator("acquired_at")(_utc)
    _heartbeat_utc = field_validator("heartbeat_at")(_utc)
    _expires_utc = field_validator("expires_at")(_utc)

    @model_validator(mode="after")
    def _lease_order(self) -> LeaderLease:
        if not (self.acquired_at <= self.heartbeat_at < self.expires_at):
            raise ValueError("leader lease time ordering is invalid")
        return self


class TradeRollup1m(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str
    market_type: str
    symbol: str
    bucket_start: datetime
    trade_count: int = Field(..., ge=0)
    buy_quantity: Decimal = Field(..., ge=0)
    sell_quantity: Decimal = Field(..., ge=0)
    quote_volume: Decimal = Field(..., ge=0)
    open_price: Decimal = Field(..., gt=0)
    high_price: Decimal = Field(..., gt=0)
    low_price: Decimal = Field(..., gt=0)
    close_price: Decimal = Field(..., gt=0)
    source_event_count: int = Field(..., ge=1)

    _bucket_utc = field_validator("bucket_start")(_utc)


class BookMetricRollup1m(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str
    market_type: str
    symbol: str
    bucket_start: datetime
    samples: int = Field(..., ge=1)
    average_spread_bps: Decimal = Field(..., ge=0)
    max_spread_bps: Decimal = Field(..., ge=0)
    average_bid_depth_quote: Decimal = Field(..., ge=0)
    average_ask_depth_quote: Decimal = Field(..., ge=0)
    source_event_count: int = Field(..., ge=1)

    _bucket_utc = field_validator("bucket_start")(_utc)


class StorageHealthSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime = Field(default_factory=utcnow)
    database_bytes: int | None = Field(default=None, ge=0)
    wal_bytes: int | None = Field(default=None, ge=0)
    free_disk_bytes: int | None = Field(default=None, ge=0)
    raw_trade_events: int = Field(default=0, ge=0)
    raw_depth_events: int = Field(default=0, ge=0)
    raw_ticker_events: int = Field(default=0, ge=0)
    retention_backlog_events: int = Field(default=0, ge=0)
    retention_enabled: bool = False
    healthy: bool = False
    reasons: tuple[str, ...] = ()

    _checked_utc = field_validator("checked_at")(_utc)


class RetentionRunReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default_factory=lambda: uuid4().hex)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime
    enabled: bool
    success: bool
    trade_events_rolled_up: int = Field(default=0, ge=0)
    trade_buckets_written: int = Field(default=0, ge=0)
    book_events_rolled_up: int = Field(default=0, ge=0)
    book_buckets_written: int = Field(default=0, ge=0)
    trade_events_deleted: int = Field(default=0, ge=0)
    book_snapshots_deleted: int = Field(default=0, ge=0)
    book_deltas_deleted: int = Field(default=0, ge=0)
    ticker_events_deleted: int = Field(default=0, ge=0)
    reason: str | None = None

    _started_utc = field_validator("started_at")(_utc)
    _finished_utc = field_validator("finished_at")(_utc)

    @model_validator(mode="after")
    def _time_order(self) -> RetentionRunReport:
        if self.finished_at < self.started_at:
            raise ValueError("retention finished_at must not be before started_at")
        if not self.success and not self.reason:
            raise ValueError("a failed retention run must carry an explicit reason")
        return self
