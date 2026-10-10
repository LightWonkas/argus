"""Cooldown/dedup — suppresses repeats of the same alert.

Keeps the last emission time per key and blocks re-emission until the window elapses.
An optional ``store`` makes it durable (idempotent recovery): after a restart the last
emission times are read back, so the same alert is not repeated.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from argus.core.schemas import utcnow


@runtime_checkable
class CooldownPersistence(Protocol):
    """Backend that persists cooldown state (e.g. SqliteStore)."""

    def get_cooldown(self, key: str) -> datetime | None: ...

    def set_cooldown(self, key: str, when: datetime) -> None: ...


class CooldownTracker:
    """Applies a cooldown window per key (e.g. symbol); optionally durable."""

    def __init__(
        self,
        window_s: float,
        *,
        clock: Callable[[], datetime] = utcnow,
        store: CooldownPersistence | None = None,
    ) -> None:
        if window_s < 0:
            raise ValueError("window_s must not be negative")
        self._window_s = window_s
        self._clock = clock
        self._store = store
        self._last: dict[str, datetime] = {}

    def _last_for(self, key: str) -> datetime | None:
        last = self._last.get(key)
        if last is None and self._store is not None:  # read from the store after a restart
            last = self._store.get_cooldown(key)
            if last is not None:
                self._last[key] = last
        return last

    def should_emit(self, key: str, *, now: datetime | None = None) -> bool:
        """``True`` when the key's cooldown has elapsed (or nothing was emitted yet)."""
        reference = now or self._clock()
        last = self._last_for(key)
        return last is None or (reference - last).total_seconds() >= self._window_s

    def record(self, key: str, *, now: datetime | None = None) -> None:
        """Record the emission time for the key (persisting it when a store exists)."""
        when = now or self._clock()
        self._last[key] = when
        if self._store is not None:
            self._store.set_cooldown(key, when)
