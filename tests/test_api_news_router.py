"""News-router endpoint tests (/api/news/route) — TestClient, offline (fake NewsRouter)."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.security import SessionConfig, hash_password
from argus.brain.news_router import NewsRouting
from argus.core.enums import Severity
from argus.core.errors import BudgetExceededError


class FakeNewsRouter:
    """Fake agent whose route() returns a fixed label (or raises a budget/generic error)."""

    def __init__(
        self, *, demo: bool = False, raises: bool = False, raises_generic: bool = False
    ) -> None:
        self.demo = demo
        self._raises = raises
        self._raises_generic = raises_generic

    def route(
        self,
        title: str,
        *,
        summary: str | None = None,
        source: str | None = None,
        symbols: Sequence[str] = (),
    ) -> NewsRouting:
        if self._raises:
            raise BudgetExceededError("daily cap 0")
        if self._raises_generic:
            raise RuntimeError("LLM connection dropped")
        return NewsRouting(
            importance=Severity.HIGH,
            relevant=True,
            target_agent="bist_expert",
            rationale="ACME results came in above expectations.",
            confidence_pct=80,
            model="fake-model",
        )


def _client(router: FakeNewsRouter | None = None) -> TestClient:
    return TestClient(create_app(news_router_provider=lambda: router or FakeNewsRouter()))


def test_news_route_returns_label() -> None:
    r = _client().post(
        "/api/news/route",
        json={"title": "ACME published its Q3 results", "symbols": ["ACME"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["importance"] == "high"
    assert body["relevant"] is True
    assert body["target_agent"] == "bist_expert"
    assert body["confidence_pct"] == 80
    assert body["model"] == "fake-model"
    assert "not financial advice" in body["disclaimer"].lower()


def test_news_route_surfaces_demo_flag() -> None:
    r = _client(FakeNewsRouter(demo=True)).post("/api/news/route", json={"title": "routine news"})
    assert r.status_code == 200
    assert r.json()["demo"] is True


def test_news_route_budget_exceeded_is_honest_unevaluated() -> None:
    # Budget exhausted → explicit "could not be evaluated"; NO fake low-importance label.
    r = _client(FakeNewsRouter(raises=True)).post("/api/news/route", json={"title": "x"})
    assert r.status_code == 200  # does not crash
    body = r.json()
    assert body["status"] == "budget_exceeded"
    assert body["importance"] is None  # no label produced → not returned as if it were valid
    assert body["relevant"] is None
    assert body["target_agent"] is None
    assert body["confidence_pct"] is None
    assert "budget exhausted" in body["rationale"].lower()


def test_news_route_generic_error_is_honest_unevaluated() -> None:
    # LLM/parse error → status=error, labels None (no crash).
    r = _client(FakeNewsRouter(raises_generic=True)).post("/api/news/route", json={"title": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["importance"] is None
    assert "could not be evaluated" in body["rationale"].lower()


def test_news_route_requires_title() -> None:
    r = _client().post("/api/news/route", json={"title": ""})
    assert r.status_code == 422  # an empty title is rejected (min_length=1)


def test_news_route_requires_auth() -> None:
    app = create_app(
        news_router_provider=lambda: FakeNewsRouter(),
        users={"ada": hash_password("p")},
        session=SessionConfig(secret=b"s"),
        secure_cookie=False,
    )
    r = TestClient(app).post("/api/news/route", json={"title": "x"})
    assert r.status_code == 401
