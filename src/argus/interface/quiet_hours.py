"""Quiet hours — defers non-critical alerts within a given window.

The window is defined in the user's local time (default Europe/Istanbul) and evaluated
against a UTC ``now``. Overnight windows (e.g. 23:00–08:00) are supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class QuietHours:
    """Quiet window in local time."""

    start: time
    end: time
    tz: str = "Europe/Istanbul"

    def is_quiet(self, now: datetime) -> bool:
        """Return whether ``now`` (tz-aware) falls inside the quiet window."""
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        local = now.astimezone(ZoneInfo(self.tz)).time()
        if self.start == self.end:
            return False  # empty window
        if self.start < self.end:
            return self.start <= local < self.end
        # overnight (e.g. 23:00–08:00)
        return local >= self.start or local < self.end
