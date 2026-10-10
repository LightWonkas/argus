"""TechnicalAnalyst tests — fake LLMClient (no anthropic package or network needed)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from argus.brain import CostGuard, LLMMessage, LLMResponse, LLMRouter, TaskTier, TechnicalAnalyst
from argus.brain.llm import LLMClient
from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import EvidenceTier, Severity, Timeframe
from argus.core.schemas import Provenance
from argus.platform.triggers import TriggerKind, TriggerSignal

_CANNED = (
    '{"summary":"BTC leaning upward","bias":"bullish","evidence_tier":"A","confidence_pct":65,'
    '"invalidation":"close below 100",'
    '"scenarios":[{"name":"Base","probability_pct":60,"detail":"x"}]}'
)


class FakeLLM(LLMClient):
    def __init__(self) -> None:
        self.last: dict[str, object] = {}

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: str,
        max_tokens: int = 8000,
        use_web_search: bool = False,
        effort: str = "high",
    ) -> LLMResponse:
        self.last = {
            "system": system,
            "user": messages[0].content,
            "model": model,
            "web": use_web_search,
        }
        return LLMResponse(text=_CANNED, model=model, cost_usd=0.01)


def _series() -> OHLCVSeries:
    base = datetime(2026, 5, 29, tzinfo=UTC)
    bars = [
        OHLCVBar(
            timestamp=base + timedelta(hours=i), open=100, high=200, low=50, close=100 + i, volume=1
        )
        for i in range(30)
    ]
    prov = Provenance(source="ccxt:binance", timestamp=bars[-1].timestamp, confidence=0.95)
    return OHLCVSeries(symbol="BTC/USDT", timeframe=Timeframe.H1, bars=tuple(bars), provenance=prov)


def _analyst(fake: LLMClient) -> TechnicalAnalyst:
    router = LLMRouter.from_single_client(
        fake, model_routine="claude-sonnet-4-6", model_deep="claude-opus-4-8", guard=CostGuard(10.0)
    )
    return TechnicalAnalyst(router)


def test_analyze_returns_output_and_uses_opus_for_deep() -> None:
    fake = FakeLLM()
    analyst = _analyst(fake)
    sig = TriggerSignal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        kind=TriggerKind.RSI_OVERBOUGHT,
        severity=Severity.MEDIUM,
        message="RSI 77",
    )
    out = analyst.analyze(_series(), [sig])

    assert out.symbol == "BTC/USDT"
    assert out.evidence_tier is EvidenceTier.A
    assert out.confidence_pct == 65
    assert "advice" in out.disclaimer.lower()
    assert fake.last["model"] == "claude-opus-4-8"  # DEEP → Opus
    assert "RSI 77" in str(fake.last["user"])  # the trigger reached the prompt
    assert fake.last["web"] is True


def test_analyze_routine_tier_uses_sonnet() -> None:
    fake = FakeLLM()
    _analyst(fake).analyze(_series(), [], tier=TaskTier.ROUTINE, use_web_search=False)
    assert fake.last["model"] == "claude-sonnet-4-6"
    assert fake.last["web"] is False
