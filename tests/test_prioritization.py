"""Quiet hours + alert prioritization tests."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from argus.core.enums import Severity, Timeframe
from argus.interface import AlertPolicy, AlertRoute, QuietHours, alert_severity
from argus.platform.triggers import TriggerKind, TriggerSignal

# Istanbul = UTC+3 (no DST). Local 02:30 → UTC 23:30 (previous day).
_NIGHT_UTC = datetime(2026, 5, 29, 23, 30, tzinfo=UTC)  # Istanbul ~02:30
_DAY_UTC = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # Istanbul 12:00


def test_quiet_overnight_window() -> None:
    qh = QuietHours(time(22, 0), time(8, 0))  # overnight
    assert qh.is_quiet(_NIGHT_UTC) is True
    assert qh.is_quiet(_DAY_UTC) is False


def test_quiet_daytime_window() -> None:
    qh = QuietHours(time(9, 0), time(17, 0))  # daytime window
    assert qh.is_quiet(_DAY_UTC) is True  # local 12:00
    assert qh.is_quiet(_NIGHT_UTC) is False  # local 02:30


def test_quiet_empty_window() -> None:
    assert QuietHours(time(8, 0), time(8, 0)).is_quiet(_DAY_UTC) is False


def test_quiet_naive_now_rejected() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        QuietHours(time(22, 0), time(8, 0)).is_quiet(datetime(2026, 5, 29, 2, 0))


def _sig(sev: Severity) -> TriggerSignal:
    return TriggerSignal(
        symbol="X", timeframe=Timeframe.H1, kind=TriggerKind.PRICE_MOVE, severity=sev, message=""
    )


def test_alert_severity_max() -> None:
    assert alert_severity([]) is Severity.INFO
    assert (
        alert_severity([_sig(Severity.LOW), _sig(Severity.HIGH), _sig(Severity.MEDIUM)])
        is Severity.HIGH
    )


def test_route_default_policy() -> None:
    p = AlertPolicy()  # min_instant=HIGH, no quiet hours
    assert p.route(Severity.INFO, _DAY_UTC) is AlertRoute.SUPPRESSED
    assert p.route(Severity.LOW, _DAY_UTC) is AlertRoute.DIGEST
    assert p.route(Severity.MEDIUM, _DAY_UTC) is AlertRoute.DIGEST
    assert p.route(Severity.HIGH, _DAY_UTC) is AlertRoute.INSTANT
    assert p.route(Severity.CRITICAL, _DAY_UTC) is AlertRoute.INSTANT


def test_route_during_quiet_defers_high() -> None:
    p = AlertPolicy(quiet_hours=QuietHours(time(0, 0), time(23, 59)))  # quiet almost all day
    assert p.route(Severity.HIGH, _DAY_UTC) is AlertRoute.DIGEST  # deferred
    assert (
        p.route(Severity.CRITICAL, _DAY_UTC) is AlertRoute.INSTANT
    )  # critical pierces quiet hours


def test_route_critical_no_override() -> None:
    p = AlertPolicy(
        quiet_hours=QuietHours(time(0, 0), time(23, 59)), critical_overrides_quiet=False
    )
    assert p.route(Severity.CRITICAL, _DAY_UTC) is AlertRoute.DIGEST
