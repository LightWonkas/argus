"""Bot analyze_symbol tests — LLM analysis + L4 confluence combined (offline, monkeypatch).

`analyze_symbol` builds the adapter + analyst lazily; here they are swapped for fakes so only
the **confluence integration** (AnalysisContext construction + combined message) is verified.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from argus.brain.agents import Agent, AgentOpinion, AnalysisContext
from argus.core.contracts import AnalysisOutput, OHLCVBar, OHLCVSeries
from argus.core.enums import EvidenceTier, Stance, Timeframe
from argus.core.schemas import Provenance, utcnow
from argus.decision.aggregator import SignalAggregator
from argus.interface import bot


def _series() -> OHLCVSeries:
    now = utcnow()
    bars = tuple(
        OHLCVBar(
            timestamp=now - timedelta(hours=30 - i),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for i in range(30)
    )
    return OHLCVSeries(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        bars=bars,
        provenance=Provenance(source="test", timestamp=now, confidence=0.9),
    )


class _FakeAdapter:
    def fetch_ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        return _series()


class _FakeAnalyst:
    def analyze(self, series: OHLCVSeries, signals: list[object]) -> AnalysisOutput:
        return AnalysisOutput(
            symbol=series.symbol,
            timeframe=series.timeframe,
            summary="Test analysis.",
            bias="bullish",
            evidence_tier=EvidenceTier.B,
            confidence_pct=60,
            invalidation="—",
            scenarios=(),
            model="fake-model",
        )


class _BullAgent(Agent):
    @property
    def name(self) -> str:
        return "fake_bull"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        # Also verify the context was built correctly (symbol/asset_class passed through).
        assert ctx.symbol == "BTC/USDT"
        assert ctx.asset_class is not None
        return AgentOpinion(
            agent=self.name, stance=Stance.BULLISH, confidence_pct=80, rationale="test"
        )


def test_analyze_symbol_combines_analysis_and_confluence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argus.config.settings import Settings

    # Swap the lazily imported adapter + analyst for fakes (offline).
    monkeypatch.setattr("argus.ingestion.crypto.binance_adapter", lambda: _FakeAdapter())
    monkeypatch.setattr(bot, "build_analyst", lambda settings, **kw: _FakeAnalyst())

    aggregator = SignalAggregator([_BullAgent()])
    msg = bot.analyze_symbol("BTC/USDT", settings=Settings(), aggregator=aggregator)

    assert "Test analysis." in msg  # LLM analysis
    assert "Confluence" in msg  # L4 confluence summary alongside
    assert "fake_bull: up ▲ (80%)" in msg  # fake_bull → BULLISH direction
    assert "fake_bull" in msg  # the contributing agent is visible
