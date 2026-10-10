"""TechnicalAnalyst — the Argus counterpart of the technical agent in ai-hedge-fund.

Takes an OHLCV series + triggers, summarises the indicators and has the LLM produce an
English :class:`~argus.core.contracts.AnalysisOutput` following the analysis methodology.
The cost governor and model selection are applied via :class:`~argus.brain.router.LLMRouter`.
"""

from __future__ import annotations

from argus.brain.llm import LLMMessage
from argus.brain.prompts import build_system_prompt, build_user_prompt, parse_analysis
from argus.brain.router import LLMRouter, TaskTier
from argus.core.contracts import AnalysisOutput, OHLCVSeries
from argus.platform.indicators import atr, ema, ohlcv_floats, rsi
from argus.platform.triggers import TriggerSignal


class TechnicalAnalyst:
    """Produces an LLM technical analysis from the trigger + indicator context."""

    def __init__(self, router: LLMRouter, *, max_tokens: int = 8000) -> None:
        self._router = router
        self._max_tokens = max_tokens

    def analyze(
        self,
        series: OHLCVSeries,
        signals: list[TriggerSignal],
        *,
        tier: TaskTier = TaskTier.DEEP,
        use_web_search: bool = True,
    ) -> AnalysisOutput:
        highs, lows, closes = ohlcv_floats(series)
        indicators: dict[str, float | None] = {
            "RSI(14)": rsi(closes)[-1] if closes else None,
            "ATR(14)": atr(highs, lows, closes)[-1] if closes else None,
            "EMA(12)": ema(closes, 12)[-1] if closes else None,
            "EMA(26)": ema(closes, 26)[-1] if closes else None,
        }
        triggers = [f"{s.kind.value} [{s.severity.value}] {s.message}" for s in signals]

        user_prompt = build_user_prompt(
            symbol=series.symbol,
            timeframe=series.timeframe,
            last_close=closes[-1] if closes else None,
            triggers=triggers,
            indicators=indicators,
        )
        response = self._router.complete(
            tier=tier,
            system=build_system_prompt(),
            messages=[LLMMessage(role="user", content=user_prompt)],
            max_tokens=self._max_tokens,
            use_web_search=use_web_search,
        )
        return parse_analysis(
            response.text, symbol=series.symbol, timeframe=series.timeframe, model=response.model
        )
