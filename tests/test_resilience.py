"""Resilience (retry + circuit breaker) tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.core.errors import CircuitOpenError
from argus.core.resilience import CircuitBreaker, CircuitState, retry


def test_retry_success_first_try() -> None:
    calls: list[int] = []
    result = retry(lambda: (calls.append(1), "ok")[1], attempts=3)
    assert result == "ok"
    assert len(calls) == 1


def test_retry_recovers_after_failures() -> None:
    state = {"n": 0}

    def fn() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert retry(fn, attempts=3) == "ok"
    assert state["n"] == 3


def test_retry_exhausts_and_reraises() -> None:
    with pytest.raises(RuntimeError, match="always"):
        retry(lambda: (_ for _ in ()).throw(RuntimeError("always")), attempts=2)


def test_retry_invalid_attempts() -> None:
    with pytest.raises(ValueError, match="attempts"):
        retry(lambda: 1, attempts=0)


def test_retry_unmatched_exception_propagates_immediately() -> None:
    state = {"n": 0}

    def fn() -> int:
        state["n"] += 1
        raise KeyError("k")

    with pytest.raises(KeyError):
        retry(fn, attempts=3, exceptions=(ValueError,))
    assert state["n"] == 1  # unmatched exception → not retried


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _boom() -> None:
    raise RuntimeError("x")


def test_circuit_opens_after_threshold_and_half_opens() -> None:
    clock = _Clock(datetime(2026, 5, 29, 12, 0, tzinfo=UTC))
    cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=60, clock=clock)

    with pytest.raises(RuntimeError):
        cb.call(_boom)
    assert cb.state is CircuitState.CLOSED  # 1 < threshold

    with pytest.raises(RuntimeError):
        cb.call(_boom)
    assert cb.state is CircuitState.OPEN  # threshold reached

    with pytest.raises(CircuitOpenError):
        cb.call(_boom)  # open → blocked without calling fn

    clock.t += timedelta(seconds=61)  # timeout elapsed
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.call(lambda: 42) == 42  # success → closes
    assert cb.state is CircuitState.CLOSED


def test_circuit_success_resets_failures() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    cb.call(lambda: 1)  # success resets the counter
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    assert cb.state is CircuitState.CLOSED  # 2 < 3 (because it was reset)
