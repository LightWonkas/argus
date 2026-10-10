"""Dispatch policy factory tests (to_alert_policy — pure)."""

from __future__ import annotations

from datetime import time

from argus.config.models import AlertConfig, QualityConfig, TriggerThresholds
from argus.core.enums import Severity
from argus.interface.factory import to_alert_policy, to_quality_gate, to_trigger_config


def test_policy_without_quiet_hours() -> None:
    policy = to_alert_policy(AlertConfig())
    assert policy.min_instant_severity is Severity.HIGH
    assert policy.quiet_hours is None
    assert policy.critical_overrides_quiet is True


def test_policy_with_quiet_hours() -> None:
    policy = to_alert_policy(
        AlertConfig(min_instant_severity="medium", quiet_start="23:00", quiet_end="08:00")
    )
    assert policy.min_instant_severity is Severity.MEDIUM
    assert policy.quiet_hours is not None
    assert policy.quiet_hours.start == time(23, 0)
    assert policy.quiet_hours.end == time(8, 0)


def test_policy_partial_quiet_ignored() -> None:
    # with only a start time, quiet hours are not set up
    policy = to_alert_policy(AlertConfig(quiet_start="23:00"))
    assert policy.quiet_hours is None


def test_to_trigger_config_maps_fields() -> None:
    tc = to_trigger_config(
        TriggerThresholds(rsi_overbought=75, price_move_pct=8, ema_fast=5, ema_slow=20)
    )
    assert tc.rsi_overbought == 75
    assert tc.price_move_pct == 8
    assert tc.ema_fast == 5
    assert tc.ema_slow == 20


def test_to_quality_gate_maps_fields() -> None:
    qg = to_quality_gate(QualityConfig(stale_factor=4, max_jump_pct=30, gap_factor=2))
    assert qg.stale_factor == 4
    assert qg.max_jump_pct == 30
    assert qg.gap_factor == 2
