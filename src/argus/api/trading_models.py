"""Secret-free API contracts of the autonomous trading control plane."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus.core.crypto import ShadowPosition
from argus.core.intelligence import (
    CoinResearchResult,
    CryptoNewsRecord,
    MissionDailyCheckpoint,
    NewsMarketReaction,
    TradePlan,
)
from argus.core.schemas import DataStatus
from argus.core.trading import (
    CommissionSchedule,
    MissionAuditEvent,
    MissionEndPolicy,
    MissionGuardResult,
    MissionLedgerSnapshot,
    OrderIntent,
    ResearchStatus,
    RetentionRunReport,
    StorageHealthSnapshot,
    SymbolResearchScore,
    TradingMission,
    TradingMode,
    UniverseEligibilitySnapshot,
)
from argus.execution.contracts import AccountSnapshot, ExchangeFill, ExchangeOrderSnapshot
from argus.observability.readiness import MemoryHealthSnapshot


class MissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capital_limit: Decimal | None = Field(default=None, gt=0)
    capital_limit_usdt: Decimal | None = Field(default=None, gt=0)
    display_currency: Literal["TRY"] = "TRY"
    input_budget_try: Decimal | None = Field(default=None, gt=0)
    try_per_usdt: Decimal | None = Field(default=None, gt=0)
    conversion_rate_source: str | None = Field(default=None, min_length=1)
    conversion_rate_at: datetime | None = None
    daily_profit_stop_pct: Decimal | None = Field(default=None, gt=0, le=100)
    daily_loss_stop_pct: Decimal | None = Field(default=None, gt=0, le=100)
    maximum_drawdown_pct: Decimal | None = Field(default=None, gt=0, le=100)
    timezone: str | None = Field(default=None, min_length=1)
    duration_days: int = Field(default=7, gt=0, le=7)
    mode: TradingMode = TradingMode.PUBLIC_MONITOR
    end_policy: MissionEndPolicy = MissionEndPolicy.MANAGE_TO_EXIT

    @field_validator("conversion_rate_at")
    @classmethod
    def _conversion_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("conversion_rate_at must be tz-aware")
        return value

    @model_validator(mode="after")
    def _budget_consistency(self) -> MissionCreateRequest:
        if self.capital_limit is None and self.capital_limit_usdt is None:
            raise ValueError("canonical USDT capital_limit is required")
        if (
            self.capital_limit is not None
            and self.capital_limit_usdt is not None
            and self.capital_limit != self.capital_limit_usdt
        ):
            raise ValueError("capital_limit and capital_limit_usdt must match")
        reference_fields = (
            self.input_budget_try,
            self.try_per_usdt,
            self.conversion_rate_source,
            self.conversion_rate_at,
        )
        if any(value is not None for value in reference_fields) and not all(
            value is not None for value in reference_fields
        ):
            raise ValueError(
                "TRY reference must be supplied together with rate, source and timestamp"
            )
        if self.input_budget_try is not None and self.try_per_usdt is not None:
            expected_usdt = self.input_budget_try / self.try_per_usdt
            actual = self.canonical_capital_usdt
            if abs(expected_usdt - actual) / actual > Decimal("0.01"):
                raise ValueError(
                    "TRY reference and canonical USDT budget must match within 1 percent"
                )
        return self

    @property
    def canonical_capital_usdt(self) -> Decimal:
        value = self.capital_limit_usdt or self.capital_limit
        if value is None:  # the model validator prevents this case
            raise ValueError("no canonical USDT budget")
        return value


class MissionArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]
    expected_revision: int = Field(..., ge=1)


class MissionHaltRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=500)
    expected_revision: int = Field(..., ge=1)


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Literal[True]
    reason: str = Field(..., min_length=3, max_length=500)


class TradingStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: TradingMode
    armed: bool
    kill_switch: bool
    active_mission_id: str | None = None
    active_mission_status: str | None = None
    public_market_data_available: bool
    private_account_available: bool = False
    user_data_stream_available: bool = False
    spot_execution_available: bool = False
    live_execution_enabled: bool = False
    execution_backend: str
    source: Literal["stored_point_in_time"] = "stored_point_in_time"
    data_status: DataStatus
    reasons: tuple[str, ...] = ()


class TradingAccountResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ResearchStatus
    connected: bool = False
    snapshot: AccountSnapshot | None = None
    reasons: tuple[str, ...]
    secret_exposed: Literal[False] = False


class TradingUniverseResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime | None = None
    scanned_count: int = Field(default=0, ge=0)
    eligible_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    page_limit: int = Field(default=100, ge=1, le=500)
    next_cursor: str | None = None
    snapshots: tuple[UniverseEligibilitySnapshot, ...] = ()
    data_status: DataStatus
    source: Literal["stored_point_in_time"] = "stored_point_in_time"


class TradingResearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scores: tuple[SymbolResearchScore, ...] = ()
    coin_results: tuple[CoinResearchResult, ...] = ()
    data_status: DataStatus
    source: Literal["stored_point_in_time"] = "stored_point_in_time"


class TradingWatchlistResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = ()
    changed_at: datetime | None = None
    dynamic_discovery_enabled: bool
    data_status: DataStatus
    source: Literal["stored_checkpoint", "configured_fallback"]


class MissionListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    missions: tuple[TradingMission, ...] = ()


class MissionDetailResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission: TradingMission
    ledger: MissionLedgerSnapshot | None = None
    events: tuple[MissionAuditEvent, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()
    daily_checkpoint: MissionDailyCheckpoint | None = None
    plans: tuple[TradePlan, ...] = ()


class TradingNewsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[CryptoNewsRecord, ...] = ()
    reactions: tuple[NewsMarketReaction, ...] = ()
    provider_health: dict[str, object] | None = None
    data_status: DataStatus
    source: Literal["stored_point_in_time"] = "stored_point_in_time"


class TradingPlansResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plans: tuple[TradePlan, ...] = ()
    data_status: DataStatus
    source: Literal["stored_point_in_time"] = "stored_point_in_time"


class TradingPositionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str | None = None
    mission_inventory: tuple[dict[str, object], ...] = ()
    shadow_positions: tuple[ShadowPosition, ...] = ()
    data_status: DataStatus
    reason: str


class TradingOrdersResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str | None = None
    intents: tuple[OrderIntent, ...] = ()
    exchange_orders: tuple[ExchangeOrderSnapshot, ...] = ()
    data_status: DataStatus
    reason: str


class TradingFillsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str | None = None
    fills: tuple[ExchangeFill, ...] = ()
    data_status: DataStatus
    reason: str


class TradingPerformanceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str | None = None
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    total_commission: Decimal | None = None
    estimated_slippage: Decimal | None = None
    turnover: Decimal | None = None
    fee_drag_pct: Decimal | None = None
    data_status: DataStatus
    reason: str


class TradingCostsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedules: tuple[CommissionSchedule, ...] = ()
    conservative_estimate_enabled: bool = False
    conservative_maker_bps: Decimal | None = None
    conservative_taker_bps: Decimal | None = None
    uncertainty_buffer_bps: Decimal | None = None
    live_eligible: Literal[False] = False
    data_status: DataStatus
    reason: str


class TradingRiskResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kill_switch: bool
    guard_results: tuple[MissionGuardResult, ...] = ()
    data_status: DataStatus
    reason: str


class TradingStorageResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    health: StorageHealthSnapshot
    last_retention: RetentionRunReport | None = None


class TradingHealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime
    overall: Literal["ok", "degraded", "fail"]
    last_market_data_at: datetime | None = None
    public_market_data: DataStatus
    news_providers: DataStatus
    private_account: Literal["no_data"] = "no_data"
    user_data_stream: Literal["no_data"] = "no_data"
    reconciliation: Literal["no_data"] = "no_data"
    execution: Literal["disabled"] = "disabled"
    storage: StorageHealthSnapshot
    memory: MemoryHealthSnapshot | None = None
    reasons: tuple[str, ...] = ()


class MissionMutationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission: TradingMission
    message: str


class KillSwitchResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: Literal[True]
    halted_mission_ids: tuple[str, ...] = ()
    message: str


class TradingPositionRow(BaseModel):
    """Reserved for stable OpenAPI shape when actual inventory is available."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: Decimal = Field(..., ge=0)
    average_cost: Decimal = Field(..., ge=0)
    mark_price: Decimal = Field(..., ge=0)
    provenance_fill_ids: tuple[str, ...] = ()


class TradingMutationPolicy(BaseModel):
    """Documents the fail-closed API policy without exposing control secrets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authentication_required: Literal[True] = True
    csrf_required: Literal[True] = True
    local_control_only: Literal[True] = True
    live_submission_available: Literal[False] = False

    @model_validator(mode="after")
    def _always_closed(self) -> TradingMutationPolicy:
        if self.live_submission_available:
            raise ValueError("this build does not offer live submission")
        return self


__all__ = [
    "KillSwitchRequest",
    "KillSwitchResponse",
    "MissionArmRequest",
    "MissionCreateRequest",
    "MissionDetailResponse",
    "MissionHaltRequest",
    "MissionListResponse",
    "MissionMutationResponse",
    "TradingAccountResponse",
    "TradingCostsResponse",
    "TradingFillsResponse",
    "TradingHealthResponse",
    "TradingNewsResponse",
    "TradingOrdersResponse",
    "TradingPerformanceResponse",
    "TradingPlansResponse",
    "TradingPositionsResponse",
    "TradingResearchResponse",
    "TradingRiskResponse",
    "TradingStatusResponse",
    "TradingStorageResponse",
    "TradingUniverseResponse",
    "TradingWatchlistResponse",
]
