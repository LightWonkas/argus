"""Cost governor (CostGuard) tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.brain import CostGuard
from argus.core.errors import BudgetExceededError


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def test_ensure_available_ok() -> None:
    CostGuard(1.0).ensure_available()  # must not raise


def test_record_then_block() -> None:
    g = CostGuard(1.0)
    g.record(0.6)
    g.ensure_available()  # 0.6 < 1.0
    g.record(0.5)  # total 1.1 ≥ 1.0
    with pytest.raises(BudgetExceededError):
        g.ensure_available()
    assert g.remaining == 0.0


def test_remaining() -> None:
    g = CostGuard(2.0)
    g.record(0.5)
    assert g.remaining == pytest.approx(1.5)


def test_daily_rollover_resets() -> None:
    t0 = datetime(2026, 5, 29, 12, tzinfo=UTC)
    clock = _Clock(t0)
    g = CostGuard(1.0, clock=clock)
    g.record(1.0)
    with pytest.raises(BudgetExceededError):
        g.ensure_available()

    clock.t = t0 + timedelta(days=1)  # the next day
    g.ensure_available()  # reset
    assert g.spent_today == 0.0


def test_negative_budget_rejected() -> None:
    with pytest.raises(ValueError, match="budget"):
        CostGuard(-1.0)


def test_negative_cost_rejected() -> None:
    with pytest.raises(ValueError, match="cost"):
        CostGuard(1.0).record(-0.1)
