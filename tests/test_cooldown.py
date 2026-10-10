"""CooldownTracker tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.interface import CooldownTracker

_T0 = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


def test_first_emit_allowed() -> None:
    assert CooldownTracker(900).should_emit("BTC/USDT", now=_T0) is True


def test_within_window_suppressed() -> None:
    ct = CooldownTracker(900)
    ct.record("BTC/USDT", now=_T0)
    assert ct.should_emit("BTC/USDT", now=_T0 + timedelta(seconds=300)) is False


def test_after_window_allowed() -> None:
    ct = CooldownTracker(900)
    ct.record("BTC/USDT", now=_T0)
    assert ct.should_emit("BTC/USDT", now=_T0 + timedelta(seconds=901)) is True


def test_keys_independent() -> None:
    ct = CooldownTracker(900)
    ct.record("BTC/USDT", now=_T0)
    assert ct.should_emit("THYAO", now=_T0) is True


def test_negative_window_rejected() -> None:
    with pytest.raises(ValueError, match="window"):
        CooldownTracker(-1)


def test_persistent_cooldown_survives_restart() -> None:
    from argus.storage import SqliteStore

    store = SqliteStore()  # :memory: — reusing the same store object simulates a "restart"
    first = CooldownTracker(900, clock=lambda: _T0, store=store)
    first.record("BTC/USDT", now=_T0)

    # Restart: a new tracker (empty memory) on the same persistent store
    after_restart = CooldownTracker(900, clock=lambda: _T0, store=store)
    assert after_restart.should_emit("BTC/USDT", now=_T0 + timedelta(seconds=300)) is False
    assert after_restart.should_emit("BTC/USDT", now=_T0 + timedelta(seconds=901)) is True
