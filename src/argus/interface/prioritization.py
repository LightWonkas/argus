"""Alert prioritization — routing by severity + quiet hours.

Prevents alert fatigue: only sufficiently severe alerts arrive instantly; the rest are
collected into the digest (daily briefing); noise (INFO) is suppressed. During quiet hours
non-critical alerts are deferred; CRITICAL (optionally) breaks through.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from argus.core.enums import Severity
from argus.interface.quiet_hours import QuietHours
from argus.platform.triggers import TriggerSignal


class AlertRoute(StrEnum):
    """Routing decision for an alert."""

    INSTANT = "instant"  # send instantly
    DIGEST = "digest"  # collect into the daily briefing
    SUPPRESSED = "suppressed"  # noise → drop


def alert_severity(signals: Iterable[TriggerSignal]) -> Severity:
    """Highest severity among the signals (INFO when empty)."""
    best = Severity.INFO
    for s in signals:
        if s.severity.rank > best.rank:
            best = s.severity
    return best


@dataclass(frozen=True)
class AlertPolicy:
    """Prioritization + quiet-hours policy."""

    min_instant_severity: Severity = Severity.HIGH  # ≥ this → instant
    quiet_hours: QuietHours | None = None
    critical_overrides_quiet: bool = True

    def route(self, severity: Severity, now: datetime) -> AlertRoute:
        if severity is Severity.INFO:
            return AlertRoute.SUPPRESSED  # noise
        if self.quiet_hours is not None and self.quiet_hours.is_quiet(now):
            if severity is Severity.CRITICAL and self.critical_overrides_quiet:
                return AlertRoute.INSTANT
            return AlertRoute.DIGEST  # defer during quiet hours
        return (
            AlertRoute.INSTANT
            if severity.rank >= self.min_instant_severity.rank
            else AlertRoute.DIGEST
        )
