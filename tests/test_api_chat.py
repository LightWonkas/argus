"""Argus Copilot endpoint tests (/api/chat) — TestClient, offline (fake engine + store)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.security import SessionConfig, hash_password
from argus.brain.budget import CostGuard
from argus.brain.chat import ChatEngine
from argus.brain.llm import LLMClient, LLMMessage, LLMResponse
from argus.brain.router import LLMRouter
from argus.core.errors import BudgetExceededError
from argus.storage.base import ChatTurn

BASE = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


class FakeLLM(LLMClient):
    def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: str,
        max_tokens: int = 8000,
        use_web_search: bool = False,
        effort: str = "high",
    ) -> LLMResponse:
        return LLMResponse(text="Argus reply", model=model, cost_usd=0.002)


class FakeChatStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []
        self.history = [ChatTurn(id=1, role="user", content="hi", created_at=BASE)]

    def save_chat(self, role: str, content: str) -> int:
        self.saved.append((role, content))
        return len(self.saved)

    def recent_chat(self, *, limit: int = 50) -> list[ChatTurn]:
        return self.history[:limit]


def _engine(demo: bool = True) -> ChatEngine:
    router = LLMRouter.from_single_client(
        FakeLLM(), model_routine="m", model_deep="m", guard=CostGuard(10.0)
    )
    return ChatEngine(router, demo=demo)


def _client(*, engine: ChatEngine | None = None, store: FakeChatStore | None = None) -> TestClient:
    return TestClient(
        create_app(
            chat_provider=lambda: engine or _engine(),
            chat_store_provider=lambda: store,
        )
    )


# --- POST /api/chat ---


def test_chat_returns_reply() -> None:
    r = _client().post("/api/chat", json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "Argus reply"
    assert body["demo"] is True
    assert "not financial advice" in body["disclaimer"].lower()


def test_chat_persists_turns() -> None:
    store = FakeChatStore()
    _client(store=store).post(
        "/api/chat", json={"messages": [{"role": "user", "content": "question?"}]}
    )
    assert ("user", "question?") in store.saved
    assert any(role == "assistant" for role, _ in store.saved)


def test_chat_budget_exceeded_is_graceful() -> None:
    class BrokeLLM(FakeLLM):
        pass

    guard = CostGuard(0.0)  # cap 0 → exhausted immediately
    router = LLMRouter.from_single_client(
        BrokeLLM(), model_routine="m", model_deep="m", guard=guard
    )
    engine = ChatEngine(router, demo=False)
    r = _client(engine=engine).post(
        "/api/chat", json={"messages": [{"role": "user", "content": "x"}]}
    )
    assert r.status_code == 200  # does not crash
    assert "budget exhausted" in r.json()["reply"].lower()
    assert r.json()["status"] == "budget_exceeded"  # explicit status


def test_chat_requires_message() -> None:
    r = _client().post("/api/chat", json={"messages": []})
    assert r.status_code == 422  # an empty message list is rejected


def test_chat_requires_auth() -> None:
    app = create_app(
        chat_provider=lambda: _engine(),
        chat_store_provider=lambda: None,
        users={"ada": hash_password("p")},
        session=SessionConfig(secret=b"s"),
        secure_cookie=False,
    )
    assert (
        TestClient(app)
        .post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
        .status_code
        == 401
    )


# --- GET /api/chat/history ---


def test_chat_history() -> None:
    store = FakeChatStore()
    r = _client(store=store).get("/api/chat/history")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["role"] == "user"
    assert body[0]["content"] == "hi"


def test_chat_history_empty_without_store() -> None:
    r = _client(store=None).get("/api/chat/history")
    assert r.status_code == 200
    assert r.json() == []


def test_budget_exceeded_when_guard_full() -> None:
    # BudgetExceededError really is raised (at the engine level).
    guard = CostGuard(0.0)
    router = LLMRouter.from_single_client(FakeLLM(), model_routine="m", model_deep="m", guard=guard)
    engine = ChatEngine(router)
    try:
        engine.respond([LLMMessage(role="user", content="x")])
        raised = False
    except BudgetExceededError:
        raised = True
    assert raised
