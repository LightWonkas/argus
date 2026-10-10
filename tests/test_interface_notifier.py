"""TelegramNotifier tests — fake sender (no network/PTB required)."""

from __future__ import annotations

import pytest

from argus.core.contracts import AnalysisOutput
from argus.core.enums import EvidenceTier, Timeframe
from argus.interface import TelegramNotifier


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class FlakySender:
    """Fails on the first ``fail_times`` calls, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self._fail = fail_times
        self.calls = 0

    def send_message(self, chat_id: int, text: str) -> None:
        self.calls += 1
        if self.calls <= self._fail:
            raise RuntimeError("network error")


class AlwaysFail:
    def send_message(self, chat_id: int, text: str) -> None:
        raise RuntimeError("permanent error")


def test_broadcast_to_all_authorized() -> None:
    sender = FakeSender()
    notifier = TelegramNotifier(sender, [1, 2])
    results = notifier.broadcast("hello")
    assert [r.ok for r in results] == [True, True]
    assert sender.sent == [(1, "hello"), (2, "hello")]


def test_chats_deduplicated() -> None:
    assert TelegramNotifier(FakeSender(), [1, 1, 2]).chat_ids == (1, 2)


def test_retry_then_success() -> None:
    sender = FlakySender(fail_times=1)
    notifier = TelegramNotifier(sender, [1], max_retries=2)
    result = notifier.broadcast("hi")[0]
    assert result.ok is True
    assert result.attempts == 2  # 1 failed + 1 successful


def test_retry_exhausted_reports_failure() -> None:
    notifier = TelegramNotifier(AlwaysFail(), [1], max_retries=2)
    result = notifier.broadcast("hi")[0]
    assert result.ok is False
    assert result.attempts == 3  # first attempt + 2 retries
    assert result.error is not None


def test_send_analysis_formats_and_broadcasts() -> None:
    sender = FakeSender()
    notifier = TelegramNotifier(sender, [42])
    analysis = AnalysisOutput(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        summary="summary",
        evidence_tier=EvidenceTier.A,
        confidence_pct=70,
        invalidation="X",
        model="m",
    )
    notifier.send_analysis(analysis)
    assert sender.sent[0][0] == 42
    assert "not financial advice" in sender.sent[0][1].lower()


def test_negative_max_retries_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        TelegramNotifier(FakeSender(), [1], max_retries=-1)
