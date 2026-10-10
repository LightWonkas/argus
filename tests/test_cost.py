"""Cost visibility tests — CostGuard breakdown/search cost + ledger + /api/cost."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.models import CostResponse
from argus.brain.budget import SEARCH_COST_USD, CostGuard
from argus.core.errors import BudgetExceededError
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)


# ── CostGuard: search cost + breakdown ───────────────────────────────────────


def test_record_counts_searches_into_cap() -> None:
    guard = CostGuard(1.0, clock=lambda: NOW)
    guard.record(0.10, searches=5, role="deep")  # 0.10 tokens + 0.05 search
    assert guard.spent_today == pytest.approx(0.15)
    b = guard.breakdown()
    assert b.tokens_usd == pytest.approx(0.10)
    assert b.search_usd == pytest.approx(5 * SEARCH_COST_USD)
    assert b.by_role == {"deep": pytest.approx(0.15)}


def test_breakdown_by_role_accumulates() -> None:
    guard = CostGuard(10.0, clock=lambda: NOW)
    guard.record(0.02, role="routine")
    guard.record(0.03, role="routine")
    guard.record(0.05, searches=2, role="copilot")
    b = guard.breakdown()
    assert b.by_role["routine"] == pytest.approx(0.05)
    assert b.by_role["copilot"] == pytest.approx(0.07)
    assert b.total_usd == pytest.approx(0.12)


def test_search_cost_alone_can_exhaust_budget() -> None:
    guard = CostGuard(0.05, clock=lambda: NOW)
    guard.record(0.0, searches=5, role="deep")  # search only: $0.05
    with pytest.raises(BudgetExceededError):
        guard.ensure_available()


def test_old_call_signature_still_works() -> None:
    guard = CostGuard(1.0, clock=lambda: NOW)
    guard.record(0.25)  # old signature (no role/searches) — back-compat
    assert guard.spent_today == pytest.approx(0.25)
    assert guard.breakdown().by_role == {"unknown": pytest.approx(0.25)}


# ── Persistent ledger: a cap shared across processes ────────────────────────


def test_guard_with_store_shares_ledger() -> None:
    store = SqliteStore()
    g1 = CostGuard(1.0, clock=lambda: NOW, store=store)
    g2 = CostGuard(1.0, clock=lambda: NOW, store=store)  # the guard of "another process"
    g1.record(0.30, searches=10, role="deep")  # 0.30 + 0.10
    # The second guard reads the SAME ledger → a shared daily cap.
    assert g2.spent_today == pytest.approx(0.40)
    b = g2.breakdown()
    assert b.tokens_usd == pytest.approx(0.30)
    assert b.search_usd == pytest.approx(0.10)
    g2.record(0.65, role="copilot")  # total 1.05 ≥ 1.0
    with pytest.raises(BudgetExceededError):
        g1.ensure_available()  # the other guard's spend counts towards the cap
    store.close()


def test_ledger_recent_days_fills_gaps() -> None:
    """The last 7 days are returned oldest→newest; days without records are filled with 0.0."""
    store = SqliteStore()
    store.add_llm_cost("2026-06-10", "deep", 0.2, 0.1)
    store.add_llm_cost("2026-06-12", "routine", 0.05, 0.0)
    store.add_llm_cost("2026-06-12", "deep", 0.10, 0.0)  # same day is summed
    days = store.llm_cost_recent_days(days=7, now=NOW)
    assert len(days) == 7
    assert days[0] == ("2026-06-06", pytest.approx(0.0))  # empty day → 0
    assert days[-3] == ("2026-06-10", pytest.approx(0.3))
    assert days[-1] == ("2026-06-12", pytest.approx(0.15))
    store.close()


def test_ledger_month_total() -> None:
    store = SqliteStore()
    store.add_llm_cost("2026-06-01", "deep", 1.0, 0.0)
    store.add_llm_cost("2026-06-12", "deep", 0.5, 0.1)
    store.add_llm_cost("2026-05-30", "deep", 9.9, 0.0)  # another month — not counted
    assert store.llm_cost_for_month("2026-06") == pytest.approx(1.6)
    day = store.llm_cost_for_day("2026-06-12")
    assert day["deep"] == (pytest.approx(0.5), pytest.approx(0.1))
    store.close()


# ── /api/cost ────────────────────────────────────────────────────────────────


def _cost_summary() -> CostResponse:
    return CostResponse(
        day="2026-06-12",
        budget_usd=3.0,
        spent_today_usd=0.4,
        remaining_today_usd=2.6,
        tokens_usd=0.3,
        search_usd=0.1,
        by_role={"deep": 0.4},
        month_to_date_usd=1.6,
        month_projection_usd=4.0,
    )


def test_cost_endpoint() -> None:
    client = TestClient(create_app(cost_provider=_cost_summary))
    r = client.get("/api/cost")
    assert r.status_code == 200
    body = r.json()
    assert body["spent_today_usd"] == 0.4
    assert body["search_usd"] == 0.1
    assert body["by_role"] == {"deep": 0.4}
    assert body["month_projection_usd"] == 4.0
    assert body["monthly_limit_usd"] == 100.0


def test_cost_endpoint_503_without_ledger() -> None:
    client = TestClient(create_app(cost_provider=lambda: None))
    r = client.get("/api/cost")
    assert r.status_code == 503
    assert r.json()["detail"] == "Cost ledger not connected — DATA PENDING"


def test_cost_endpoint_requires_auth() -> None:
    from argus.api.security import SessionConfig, hash_password

    app = create_app(
        cost_provider=_cost_summary,
        users={"ada": hash_password("p")},
        session=SessionConfig(secret=b"s"),
        secure_cookie=False,
    )
    assert TestClient(app).get("/api/cost").status_code == 401
