"""API contracts for the news/event hub."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TradingViewWebhookRequest(BaseModel):
    """External technical signal body only; carries no secrets."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["tradingview-signal-v1"] = "tradingview-signal-v1"
    symbol: str = Field(..., min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    signal_type: str = Field(..., min_length=1, max_length=100)
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h"]
    direction: Literal["bullish", "bearish", "neutral"]
    timestamp: datetime
    nonce: str = Field(..., min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    idempotency_key: str = Field(..., min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("attributes")
    @classmethod
    def _bounded_attributes(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        if len(value) > 30:
            raise ValueError("attributes may carry at most 30 fields")
        if any(len(str(key)) > 64 or len(str(item)) > 500 for key, item in value.items()):
            raise ValueError("attributes field exceeds the size limit")
        return value


class TradingViewWebhookAck(BaseModel):
    accepted: Literal[True] = True
    signal_id: str
    submission_authorized: Literal[False] = False
    message: str = "ExternalTechnicalSignal recorded; no order was created"


__all__ = ["TradingViewWebhookAck", "TradingViewWebhookRequest"]
