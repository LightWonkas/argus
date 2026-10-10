"""Core data schemas — provenance and honesty primitives.

L1 adapter contract: **every record** carries ``source + timestamp + freshness +
confidence``. Honesty rule: no data means no fabrication → :data:`DATA_PENDING`
("DATA PENDING") is used instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Honesty requirement: without data/signal this constant is written (never fabricate).
DATA_PENDING = "DATA PENDING"


class FreshnessLabel(StrEnum):
    """Freshness label — prevents alerts from being raised on stale/broken data."""

    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


class DataStatus(StrEnum):
    """Status of a record/field (honesty gate)."""

    OK = "ok"
    PENDING = "pending"  # DATA PENDING
    QUARANTINED = "quarantined"  # failed the Data Quality Gate
    REJECTED = "rejected"  # definitively rejected by validation/policy


def utcnow() -> datetime:
    """Timezone-aware (UTC) now."""
    return datetime.now(UTC)


class Provenance(BaseModel):
    """Provenance + confidence information every data record must carry.

    Immutable (frozen): provenance does not change once produced.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(
        ..., min_length=1, description="Source name, e.g. 'borsapy', 'ccxt:binance'"
    )
    timestamp: datetime = Field(
        ..., description="Time the data refers to (point-in-time, tz-aware)"
    )
    ingested_at: datetime = Field(default_factory=utcnow, description="Time of ingestion (UTC)")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the source/measurement [0..1]"
    )
    freshness: FreshnessLabel = FreshnessLabel.FRESH
    # Is the price series back-adjusted (splits/dividends)? For PIT purity unadjusted
    # (False) is ideal; BIST is currently fetched with adjust=True → flagged True (an
    # explicit exception). Backtests report which view they ran on.
    adjusted: bool = Field(default=False, description="Whether the price series is back-adjusted")

    @field_validator("timestamp", "ingested_at")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        """Timestamps must be tz-aware (for PIT consistency)."""
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v

    def age_seconds(self, *, now: datetime | None = None) -> float:
        """Age of the data (seconds)."""
        reference = now or utcnow()
        return (reference - self.timestamp).total_seconds()

    def with_computed_freshness(
        self,
        *,
        stale_after_s: float,
        expired_after_s: float,
        now: datetime | None = None,
    ) -> Provenance:
        """Returns a new copy with the freshness label computed from age.

        ``age <= stale_after_s`` → FRESH, ``<= expired_after_s`` → STALE, above → EXPIRED.
        """
        if expired_after_s < stale_after_s:
            raise ValueError("expired_after_s must not be less than stale_after_s")
        age = self.age_seconds(now=now)
        if age <= stale_after_s:
            label = FreshnessLabel.FRESH
        elif age <= expired_after_s:
            label = FreshnessLabel.STALE
        else:
            label = FreshnessLabel.EXPIRED
        return self.model_copy(update={"freshness": label})
