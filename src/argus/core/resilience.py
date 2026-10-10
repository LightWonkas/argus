"""Resilience tools — retry + circuit breaker (fail-safe, degraded mode).

Retries and a circuit breaker for external calls (adapter/LLM/Telegram). The breaker
temporarily disables a source that keeps failing → "flying blind on X" (graceful
degradation) without hammering the source.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from argus.core.errors import CircuitOpenError
from argus.core.schemas import utcnow

T = TypeVar("T")


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = _time.sleep,
) -> T:
    """Retries ``fn`` with exponential backoff; re-raises the last error."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if i < attempts - 1 and base_delay > 0:
                sleep(base_delay * (2**i))
    assert last_exc is not None  # the loop ran at least once
    raise last_exc


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal
    OPEN = "open"  # disabled (failure threshold exceeded)
    HALF_OPEN = "half_open"  # trial call allowed (after the timeout)


@dataclass
class CircuitBreaker:
    """Circuit breaker that opens past a failure threshold; half-open trial after the timeout."""

    failure_threshold: int = 5
    reset_timeout_s: float = 60.0
    clock: Callable[[], datetime] = utcnow
    _failures: int = field(default=0, init=False)
    _opened_at: datetime | None = field(default=None, init=False)

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        elapsed = (self.clock() - self._opened_at).total_seconds()
        return CircuitState.HALF_OPEN if elapsed >= self.reset_timeout_s else CircuitState.OPEN

    def call(self, fn: Callable[[], T]) -> T:
        """Calls ``fn`` when closed/half-open; raises :class:`CircuitOpenError` when open."""
        if self.state is CircuitState.OPEN:
            raise CircuitOpenError(f"Circuit open ({self._failures} consecutive failures)")
        try:
            result = fn()
        except Exception:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self.clock()
            raise
        # Success → reset (closed)
        self._failures = 0
        self._opened_at = None
        return result
