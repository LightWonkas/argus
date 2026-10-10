"""Account and spot execution capability boundaries.

This module defines contracts only. It contains no private endpoint, request signing, API
key or concrete adapter that sends live orders. The public market-data process therefore
cannot load private capabilities by accident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from argus.core.enums import OrderSide
from argus.core.schemas import DataStatus, utcnow
from argus.core.trading import CommissionSchedule, OrderIntent, TradingMode


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be tz-aware")
    return value


class CapabilityState(StrEnum):
    AVAILABLE = "available"
    NO_DATA = "no_data"
    STALE = "stale"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    FAILED_SAFE = "failed_safe"


class AccountBalance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset: str
    free: Decimal = Field(..., ge=0)
    locked: Decimal = Field(..., ge=0)


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_ref: str
    environment: TradingMode
    observed_at: datetime = Field(default_factory=utcnow)
    status: CapabilityState
    can_trade_spot: bool = False
    withdrawals_disabled_verified: bool = False
    transfers_disabled_verified: bool = False
    balances: tuple[AccountBalance, ...] = ()
    data_status: DataStatus = DataStatus.PENDING
    reasons: tuple[str, ...] = ()

    _observed_aware = field_validator("observed_at")(_aware)


class UserDataEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: str
    event_time: datetime
    order_id: str | None = None
    client_order_id: str | None = None
    symbol: str | None = None
    data_status: DataStatus

    _event_aware = field_validator("event_time")(_aware)


class ExecutionOrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class SubmissionAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str
    intent_id: str
    client_order_id: str
    attempted_at: datetime = Field(default_factory=utcnow)
    owner_id: str
    fencing_token: int = Field(..., gt=0)
    accepted_by_exchange: bool | None = None
    exchange_order_id: str | None = None
    status: ExecutionOrderStatus = ExecutionOrderStatus.UNKNOWN
    retry_permitted: bool = False
    reason: str

    _attempted_aware = field_validator("attempted_at")(_aware)


class ExchangeOrderSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    original_quantity: Decimal = Field(..., gt=0)
    executed_quantity: Decimal = Field(..., ge=0)
    cumulative_quote_quantity: Decimal = Field(..., ge=0)
    status: ExecutionOrderStatus
    observed_at: datetime = Field(default_factory=utcnow)

    _observed_aware = field_validator("observed_at")(_aware)


class ExchangeFill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_id: str
    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    price: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., gt=0)
    commission_native: Decimal = Field(..., ge=0)
    commission_asset: str
    commission_quote: Decimal | None = Field(default=None, ge=0)
    occurred_at: datetime

    _occurred_aware = field_validator("occurred_at")(_aware)


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str
    checked_at: datetime = Field(default_factory=utcnow)
    status: CapabilityState
    account_matches: bool = False
    orders_match: bool = False
    fills_match: bool = False
    inventory_matches: bool = False
    unknown_client_order_ids: tuple[str, ...] = ()
    unexplained_assets: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    _checked_aware = field_validator("checked_at")(_aware)

    @property
    def safe_to_resume(self) -> bool:
        return (
            self.status is CapabilityState.AVAILABLE
            and self.account_matches
            and self.orders_match
            and self.fills_match
            and self.inventory_matches
            and not self.unknown_client_order_ids
            and not self.unexplained_assets
        )


@runtime_checkable
class PublicMarketDataClient(Protocol):
    async def fetch_exchange_time(self) -> datetime: ...


@runtime_checkable
class PrivateAccountDataClient(Protocol):
    async def account_snapshot(self) -> AccountSnapshot: ...


@runtime_checkable
class PrivateAccountClient(PrivateAccountDataClient, Protocol):
    """Private read-only account contract under its API-facing name."""


@runtime_checkable
class UserDataStreamClient(Protocol):
    def events(self) -> AsyncIterator[UserDataEvent]: ...


@runtime_checkable
class SpotUserDataStreamClient(UserDataStreamClient, Protocol):
    """Separate user-stream capability boundary for spot order/account events."""


@runtime_checkable
class SpotExecutionClient(Protocol):
    async def submit(self, intent: OrderIntent) -> SubmissionAttempt: ...

    async def query_by_client_order_id(self, client_order_id: str) -> ExchangeOrderSnapshot: ...

    async def cancel(self, client_order_id: str) -> ExchangeOrderSnapshot: ...


@runtime_checkable
class CommissionProvider(Protocol):
    async def commission_schedule(self, symbol: str) -> CommissionSchedule: ...


@runtime_checkable
class AccountCommissionProvider(CommissionProvider, Protocol):
    """Keeps verified account commission apart from public price data."""


@runtime_checkable
class ExchangeReconciliationService(Protocol):
    async def reconcile(self, mission_id: str) -> ReconciliationReport: ...


__all__ = [
    "AccountBalance",
    "AccountCommissionProvider",
    "AccountSnapshot",
    "CapabilityState",
    "CommissionProvider",
    "ExchangeFill",
    "ExchangeOrderSnapshot",
    "ExchangeReconciliationService",
    "ExecutionOrderStatus",
    "PrivateAccountClient",
    "PrivateAccountDataClient",
    "PublicMarketDataClient",
    "ReconciliationReport",
    "SpotExecutionClient",
    "SpotUserDataStreamClient",
    "SubmissionAttempt",
    "UserDataEvent",
    "UserDataStreamClient",
]
