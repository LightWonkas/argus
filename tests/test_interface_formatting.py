"""Telegram message formatting + authorization tests."""

from __future__ import annotations

from argus.core.contracts import AnalysisOutput, OpinionRef, Proposal, Scenario
from argus.core.enums import EvidenceTier, Severity, Stance, Timeframe
from argus.interface import (
    format_alert,
    format_analysis,
    format_analysis_with_proposal,
    format_proposal,
    is_authorized,
)
from argus.platform.triggers import TriggerKind, TriggerSignal


def _analysis() -> AnalysisOutput:
    return AnalysisOutput(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        summary="Uptrend continues.",
        bias="bullish",
        evidence_tier=EvidenceTier.B,
        confidence_pct=62,
        invalidation="close below 100",
        scenarios=(Scenario(name="Base", probability_pct=60, detail="test of 108"),),
        model="claude-opus-4-8",
    )


def test_format_analysis_contains_key_fields() -> None:
    msg = format_analysis(_analysis())
    assert "BTC/USDT" in msg
    assert "Uptrend continues" in msg
    assert "[B]" in msg
    assert "62%" in msg
    assert "close below 100" in msg
    assert "Base (60%)" in msg
    assert "not financial advice" in msg.lower()


def _proposal() -> Proposal:
    return Proposal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        direction=Stance.BULLISH,
        strength=68,
        confluence_score=75,
        rationale="3/4 directional agents point up.",
        contributors=(
            OpinionRef(agent="technical", stance=Stance.BULLISH, confidence_pct=70),
            OpinionRef(agent="news_sentiment", stance=Stance.NO_DATA, confidence_pct=0),
        ),
    )


def test_format_proposal_contains_confluence_fields() -> None:
    msg = format_proposal(_proposal())
    assert "Confluence" in msg
    assert "BTC/USDT" in msg
    assert "Strength: 68%" in msg
    assert "Agreement: 75%" in msg
    assert "technical" in msg  # contributing agent
    assert "up ▲" in msg  # direction label
    assert "not financial advice" in msg.lower()  # disclaimer


def test_format_analysis_with_proposal_combines_both() -> None:
    msg = format_analysis_with_proposal(_analysis(), _proposal())
    # LLM analysis (summary) + confluence summary side by side, with a separator.
    assert "Uptrend continues" in msg  # analysis
    assert "Confluence" in msg  # proposal
    assert msg.index("Uptrend continues") < msg.index("Confluence")  # analysis first


def test_format_alert_orders_by_severity() -> None:
    sigs = [
        TriggerSignal(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            kind=TriggerKind.RSI_OVERBOUGHT,
            severity=Severity.MEDIUM,
            message="RSI 77",
        ),
        TriggerSignal(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            kind=TriggerKind.PRICE_MOVE,
            severity=Severity.HIGH,
            message="14% move",
        ),
    ]
    msg = format_alert("BTC/USDT", Timeframe.H1, sigs)
    assert "HIGH" in msg
    # the most severe (HIGH) item comes first
    assert msg.index("14% move") < msg.index("RSI 77")


def test_format_alert_empty() -> None:
    assert "no trigger" in format_alert("ACME", Timeframe.D1, [])


def test_is_authorized() -> None:
    assert is_authorized(111, {111, 222}) is True
    assert is_authorized(999, {111, 222}) is False
    assert is_authorized(1, frozenset()) is False
