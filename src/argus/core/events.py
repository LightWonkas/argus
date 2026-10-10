"""Event contract — base types for the L2 event bus.

The system is event-driven and loosely coupled. Adapters/services publish this
envelope; subscriber services consume it. The transport (Redis etc.) lives in the
platform layer — only the serialisable contract is defined here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from argus.core.schemas import utcnow


class EventType(StrEnum):
    """Known event types carried on the L2 event bus. Extended as the system grows."""

    PRICE_UPDATED = "price.updated"
    TRIGGER_FIRED = "trigger.fired"
    NEWS_RECEIVED = "news.received"
    DISCLOSURE_KAP = "disclosure.kap"
    ALERT_CREATED = "alert.created"
    PROPOSAL_CREATED = "proposal.created"
    ORDER_REQUESTED = "order.requested"
    # Argus-specific internal events:
    DATA_QUARANTINED = "data.quarantined"


class Event(BaseModel):
    """Base envelope travelling on the event bus.

    ``payload`` is a free-form dict to keep layers loosely coupled.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: EventType
    source: str = Field(..., min_length=1, description="Component that produced the event")
    created_at: datetime = Field(default_factory=utcnow)
    payload: dict[str, object] = Field(default_factory=dict)

    @property
    def topic(self) -> str:
        """Bus topic name (e.g. Redis stream/pub-sub channel)."""
        return f"argus.{self.type.value}"
