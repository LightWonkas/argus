"""Status panel (format_status) tests."""

from __future__ import annotations

from argus.observability import SystemStatus, format_status


def test_format_status_basic() -> None:
    out = format_status(
        SystemStatus(
            version="0.1.0",
            env="dev",
            authorized_count=2,
            budget_total_usd=3.0,
            hit_correct=2,
            hit_total=3,
        )
    )
    assert "0.1.0" in out
    assert "dev" in out
    assert "Hit rate: 2/3 (67%)" in out
    assert "$3.00" in out
    assert "not financial advice" in out.lower()


def test_format_status_no_results_yet() -> None:
    out = format_status(SystemStatus(version="0.1.0", env="dev", authorized_count=1, hit_total=0))
    assert "Hit rate: no outcomes yet" in out


def test_format_status_degraded_sources() -> None:
    out = format_status(
        SystemStatus(version="0.1.0", env="dev", authorized_count=1, degraded_sources=("BTC/USDT",))
    )
    assert "Flying blind on sources:" in out
    assert "BTC/USDT" in out
