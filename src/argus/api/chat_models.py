"""Argus Copilot endpoint contracts (chat) — secret-free, typed.

``/api/chat`` (POST) takes a message list plus optional context (the selected asset) and
returns the brain's reply. ``/api/chat/history`` (GET) returns the persisted chat history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.core.contracts import NOT_ADVICE
from argus.storage.base import ChatTurn


class ChatMessageIn(BaseModel):
    """A single message in the chat history (request body)."""

    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    """``POST /api/chat`` body — conversation + optional cockpit context."""

    model_config = ConfigDict(frozen=True)

    messages: list[ChatMessageIn] = Field(..., min_length=1)
    context_symbol: str | None = Field(default=None, description="Selected asset (for context)")


class ChatResponse(BaseModel):
    """``POST /api/chat`` response — brain reply + metadata + status.

    ``status``: ``ok`` | ``budget_exceeded`` | ``error`` — when no reply could be produced
    the "could not be evaluated" semantics are explicit (consumers read the status rather
    than a fake model name).
    """

    model_config = ConfigDict(frozen=True)

    reply: str
    model: str
    cost_usd: float
    demo: bool = Field(..., description="Whether this is demo mode (no real LLM connected)")
    status: Literal["ok", "budget_exceeded", "error"] = "ok"
    disclaimer: str = NOT_ADVICE


class ChatTurnView(BaseModel):
    """``GET /api/chat/history`` item."""

    model_config = ConfigDict(frozen=True)

    role: str
    content: str
    created_at: datetime

    @classmethod
    def from_turn(cls, turn: ChatTurn) -> ChatTurnView:
        return cls(role=turn.role, content=turn.content, created_at=turn.created_at)
