"""News-router endpoint contracts (``POST /api/news/route``) — secret-free, typed.

Classifies a single manually supplied news item (title + summary + source + symbols) with
:class:`NewsRouter` and returns a label (importance · relevant · target agent · rationale ·
confidence). The output is a **label**, not a routing decision (see
``brain/news_router.py``).

**Status convention:** the response carries a ``status`` — ``ok`` | ``budget_exceeded`` |
``error``. If classification could NOT be produced the label fields stay ``None`` (invalid
data is never returned as if it were a valid label); consumers check ``status`` first.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.core.contracts import NOT_ADVICE

# Shared status contract of the agent endpoints.
RouteStatus = Literal["ok", "budget_exceeded", "error"]


class NewsRouteRequest(BaseModel):
    """``POST /api/news/route`` body — the single news item to classify."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(..., min_length=1, max_length=2000)
    summary: str | None = Field(default=None, max_length=8000)
    source: str | None = Field(default=None, max_length=200)
    symbols: list[str] = Field(default_factory=list)


class NewsRouteResponse(BaseModel):
    """``POST /api/news/route`` response — news-router label + status.

    ``status != "ok"`` → label fields are ``None`` (could not be evaluated; no made-up label).
    """

    model_config = ConfigDict(frozen=True)

    status: RouteStatus = "ok"
    importance: str | None = None  # Severity.value (info|low|medium|high|critical) — None unless ok
    relevant: bool | None = None
    target_agent: str | None = None
    rationale: str = ""
    confidence_pct: int | None = Field(default=None, ge=0, le=100)
    model: str = ""
    demo: bool = Field(
        default=False, description="Whether this is demo mode (no real LLM connected)"
    )
    disclaimer: str = NOT_ADVICE
