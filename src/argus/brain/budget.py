"""Cost governor — daily LLM budget cap + per-item breakdown.

Stops calls as the daily cap is reached. The gate is applied **before** a call with
``ensure_available()``; spend is recorded after the call with ``record()``. The counter
resets when the day changes.

Budget = **tokens + web search** (market data comes from separate free-tier APIs). Web
search costs $10/1000 requests → $0.01 per search; CostGuard counts searches toward the cap.

Persistence: when an optional :class:`CostStore` is injected, spend is written to the SQLite
ledger and ``spent_today``/the breakdown are read **from the ledger** → processes (watch
daemon, dashboard, poller) share the same daily cap. Without a store it is in-memory
(tests/demo).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from argus.core.errors import BudgetExceededError
from argus.core.schemas import utcnow

# Server-side web search cost: $10 / 1000 requests (ON TOP of the token cost).
SEARCH_COST_USD = 0.01


class CostStore(Protocol):
    """Cost-ledger surface (implemented by SqliteStore; a fake is passed in tests)."""

    def add_llm_cost(self, day: str, role: str, tokens_usd: float, search_usd: float) -> None: ...

    def llm_cost_for_day(self, day: str) -> dict[str, tuple[float, float]]:
        """day → {role: (tokens_usd, search_usd)}."""
        ...


@dataclass(frozen=True)
class CostBreakdown:
    """Per-item breakdown of today's spend (served by the dashboard `/api/cost`)."""

    day: str
    tokens_usd: float
    search_usd: float
    by_role: dict[str, float] = field(default_factory=dict)  # role → total USD

    @property
    def total_usd(self) -> float:
        return self.tokens_usd + self.search_usd


class CostGuard:
    """Tracks daily USD spend and rejects calls that exceed the cap."""

    def __init__(
        self,
        daily_budget_usd: float,
        *,
        clock: Callable[[], datetime] = utcnow,
        store: CostStore | None = None,
    ) -> None:
        if daily_budget_usd < 0:
            raise ValueError("daily_budget_usd cannot be negative")
        self._budget = daily_budget_usd
        self._clock = clock
        self._store = store
        self._day: date = clock().date()
        self._tokens: float = 0.0
        self._search: float = 0.0
        self._by_role: dict[str, float] = {}

    @property
    def budget(self) -> float:
        return self._budget

    def _roll_day(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day = today
            self._tokens = 0.0
            self._search = 0.0
            self._by_role = {}

    @property
    def spent_today(self) -> float:
        """Today's total (FROM THE LEDGER when a store exists — shared across processes)."""
        self._roll_day()
        if self._store is not None:
            rows = self._store.llm_cost_for_day(self._day.isoformat())
            return sum(t + s for t, s in rows.values())
        return self._tokens + self._search

    @property
    def remaining(self) -> float:
        return max(self._budget - self.spent_today, 0.0)

    def breakdown(self) -> CostBreakdown:
        """Today's breakdown (tokens/search + per role)."""
        self._roll_day()
        if self._store is not None:
            rows = self._store.llm_cost_for_day(self._day.isoformat())
            return CostBreakdown(
                day=self._day.isoformat(),
                tokens_usd=sum(t for t, _ in rows.values()),
                search_usd=sum(s for _, s in rows.values()),
                by_role={role: t + s for role, (t, s) in sorted(rows.items())},
            )
        return CostBreakdown(
            day=self._day.isoformat(),
            tokens_usd=self._tokens,
            search_usd=self._search,
            by_role=dict(sorted(self._by_role.items())),
        )

    def ensure_available(self) -> None:
        """Raise :class:`BudgetExceededError` when the budget is exhausted (before a call)."""
        spent = self.spent_today
        if spent >= self._budget:
            raise BudgetExceededError(
                f"Daily LLM budget exhausted: ${spent:.4f} / ${self._budget:.2f}"
            )

    def record(self, cost_usd: float, *, searches: int = 0, role: str = "") -> None:
        """Record the incurred cost ($0.01 per search counts toward the cap)."""
        if cost_usd < 0:
            raise ValueError("cost_usd cannot be negative")
        if searches < 0:
            raise ValueError("searches cannot be negative")
        self._roll_day()
        search_usd = searches * SEARCH_COST_USD
        self._tokens += cost_usd
        self._search += search_usd
        if cost_usd or search_usd:
            key = role or "unknown"
            self._by_role[key] = self._by_role.get(key, 0.0) + cost_usd + search_usd
        if self._store is not None and (cost_usd or search_usd):
            # A ledger error never fails the call (fail-safe; the in-memory counter advanced).
            with contextlib.suppress(Exception):
                self._store.add_llm_cost(
                    self._day.isoformat(), role or "unknown", cost_usd, search_usd
                )
