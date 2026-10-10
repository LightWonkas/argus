"""AlertDispatcher + briefing tests (fake sender)."""

from __future__ import annotations

from datetime import UTC, datetime, time

from argus.core.contracts import AnalysisOutput
from argus.core.enums import EvidenceTier, Severity, Timeframe
from argus.interface import (
    AlertDispatcher,
    AlertPolicy,
    AlertRoute,
    QuietHours,
    TelegramNotifier,
    format_briefing,
)

_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # 12:00 Istanbul time


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class MarkupFakeSender(FakeSender):
    def __init__(self) -> None:
        super().__init__()
        self.markups: list[dict[str, object]] = []

    def send_message_markup(
        self, chat_id: int, text: str, *, reply_markup: dict[str, object]
    ) -> None:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)


def _analysis(symbol: str = "BTC/USDT") -> AnalysisOutput:
    return AnalysisOutput(
        symbol=symbol,
        timeframe=Timeframe.H1,
        summary="overview",
        bias="bullish",
        evidence_tier=EvidenceTier.B,
        confidence_pct=62,
        invalidation="X",
        model="m",
    )


def _dispatcher(sender: FakeSender, *, policy: AlertPolicy | None = None) -> AlertDispatcher:
    return AlertDispatcher(TelegramNotifier(sender, [1, 2]), policy=policy, clock=lambda: _NOW)


def test_instant_route_sends_now() -> None:
    sender = FakeSender()
    d = _dispatcher(sender)
    route = d.dispatch_analysis(_analysis(), Severity.HIGH)
    assert route is AlertRoute.INSTANT
    assert len(sender.sent) == 2  # two authorized chats
    assert d.pending_digest == ()


def test_digest_route_accumulates() -> None:
    sender = FakeSender()
    d = _dispatcher(sender)
    route = d.dispatch_analysis(_analysis(), Severity.MEDIUM)
    assert route is AlertRoute.DIGEST
    assert sender.sent == []  # nothing sent instantly
    assert len(d.pending_digest) == 1


def test_suppressed_route_does_nothing() -> None:
    sender = FakeSender()
    d = _dispatcher(sender)
    route = d.dispatch_analysis(_analysis(), Severity.INFO)
    assert route is AlertRoute.SUPPRESSED
    assert sender.sent == []
    assert d.pending_digest == ()


def test_flush_digest_broadcasts_and_clears() -> None:
    sender = FakeSender()
    d = _dispatcher(sender)
    d.dispatch_analysis(_analysis("BTC/USDT"), Severity.MEDIUM)
    d.dispatch_analysis(_analysis("ETH/USDT"), Severity.LOW)
    results = d.flush_digest(now=_NOW)

    assert len(results) == 2  # a briefing to both chats
    assert "Daily briefing" in sender.sent[0][1]
    assert "BTC/USDT" in sender.sent[0][1] and "ETH/USDT" in sender.sent[0][1]
    assert d.pending_digest == ()  # cleared


def test_flush_empty_digest_noop() -> None:
    sender = FakeSender()
    assert _dispatcher(sender).flush_digest(now=_NOW) == []
    assert sender.sent == []


def test_quiet_hours_defers_high_to_digest() -> None:
    sender = FakeSender()
    policy = AlertPolicy(quiet_hours=QuietHours(time(0, 0), time(23, 59)))
    d = _dispatcher(sender, policy=policy)
    assert d.dispatch_analysis(_analysis(), Severity.HIGH) is AlertRoute.DIGEST
    assert sender.sent == []
    assert len(d.pending_digest) == 1


def test_dispatcher_preserves_proposal_text_and_feedback_markup() -> None:
    sender = MarkupFakeSender()
    d = AlertDispatcher(TelegramNotifier(sender, [1]))
    markup: dict[str, object] = {"inline_keyboard": []}
    d.dispatch_analysis(_analysis(), Severity.HIGH, text="analysis + proposal", reply_markup=markup)
    assert sender.sent == [(1, "analysis + proposal")]
    assert sender.markups == [markup]


def test_digest_preserves_deferred_detail_and_feedback_markup() -> None:
    sender = MarkupFakeSender()
    d = AlertDispatcher(TelegramNotifier(sender, [1]))
    markup: dict[str, object] = {"inline_keyboard": []}
    d.dispatch_analysis(_analysis(), Severity.MEDIUM, text="full proposal", reply_markup=markup)
    d.flush_digest(now=_NOW)
    assert len(sender.sent) == 2  # the summary first, then the detail with buttons
    assert sender.sent[1] == (1, "full proposal")
    assert sender.markups == [markup]


def test_format_briefing_empty() -> None:
    msg = format_briefing(_NOW, [])
    assert "(no alerts to report for this period)" in msg
    assert "not financial advice" in msg.lower()
