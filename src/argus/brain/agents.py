"""Multi-agent brain (ai-hedge-fund style) — the L3 agent set.

TechnicalAgent · NewsSentimentAgent · MacroIntermarketAgent · PositioningAgent ·
RiskContextAgent · PortfolioAgent. Each agent produces an :class:`AgentOpinion`;
:class:`~argus.decision.aggregator.SignalAggregator` merges them via confluence.

**Cost decision:** the agents are **local/deterministic** (no LLM) → testable offline and
budget-safe. Without data they honestly report ``NO_DATA`` (DATA PENDING). The richer LLM
narrative stays in :class:`~argus.brain.analyst.TechnicalAnalyst`; LLM-assisted agents plug
into the same :class:`Agent` interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from argus.core.contracts import Disclosure, NewsItem, OHLCVSeries
from argus.core.enums import AssetClass, EvidenceTier, Stance, Timeframe
from argus.core.schemas import DATA_PENDING, DataStatus
from argus.platform.indicators import atr, ema, ohlcv_floats, rsi
from argus.platform.triggers import TriggerSignal


class AgentOpinion(BaseModel):
    """The opinion of a single agent."""

    model_config = ConfigDict(frozen=True)

    agent: str
    stance: Stance
    confidence_pct: int = Field(..., ge=0, le=100)
    rationale: str
    evidence_tier: EvidenceTier = EvidenceTier.C
    data_status: DataStatus = DataStatus.OK
    # Evidence sources used by the agent (e.g. {"ind:rsi14","ind:ema12"}, {"news:kap"},
    # {"macro:dxy"}). The aggregator counts Jaccard-overlapping opinions as ONE VOTE —
    # correlated opinions cannot inflate consensus as if independent. Empty set = no
    # evidence declared (cannot be clustered, counted as independent).
    evidence_sources: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AnalysisContext:
    """Input shared by the agents; each agent uses what it needs."""

    symbol: str
    timeframe: Timeframe
    series: OHLCVSeries
    signals: tuple[TriggerSignal, ...] = ()
    disclosures: tuple[Disclosure, ...] = ()
    news: tuple[NewsItem, ...] = ()
    macro: Mapping[str, float] | None = None
    asset_class: AssetClass | None = None
    # Multi-timeframe: the symbol's series on other timeframes (higher-timeframe context).
    # Empty = single timeframe. The LLM gets COMPUTED [A] numbers from these, not images.
    series_by_tf: Mapping[Timeframe, OHLCVSeries] = field(default_factory=dict)
    # Positioning indicators (e.g. crypto_fng: Fear&Greed level 0–100).
    # Unlike macro, these are LEVEL values, not trend directions.
    positioning: Mapping[str, float] | None = None


class Agent(ABC):
    """L3 agent interface."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def assess(self, ctx: AnalysisContext) -> AgentOpinion: ...

    def _no_data(self, reason: str) -> AgentOpinion:
        return AgentOpinion(
            agent=self.name,
            stance=Stance.NO_DATA,
            confidence_pct=0,
            rationale=f"{DATA_PENDING}: {reason}",
            evidence_tier=EvidenceTier.C,
            data_status=DataStatus.PENDING,
        )


class TechnicalAgent(Agent):
    """Technical direction opinion from local indicators (EMA trend + RSI extremes)."""

    name = "technical"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        _, _, closes = ohlcv_floats(ctx.series)
        if len(closes) < 2:
            return self._no_data("not enough candles")
        rsi_v = rsi(closes)[-1]
        ema_fast = ema(closes, 12)[-1]
        ema_slow = ema(closes, 26)[-1]

        votes = 0
        reasons: list[str] = []
        sources: set[str] = set()
        if ema_fast is not None and ema_slow is not None:
            sources |= {"ind:ema12", "ind:ema26"}
            if ema_fast > ema_slow:
                votes += 1
                reasons.append("EMA12>EMA26 (uptrend)")
            elif ema_fast < ema_slow:
                votes -= 1
                reasons.append("EMA12<EMA26 (downtrend)")
        if rsi_v is not None:
            sources.add("ind:rsi14")
            if rsi_v <= 30:
                votes += 1
                reasons.append(f"RSI {rsi_v:.0f} oversold (rebound potential)")
            elif rsi_v >= 70:
                votes -= 1
                reasons.append(f"RSI {rsi_v:.0f} overbought (pullback risk)")

        if not reasons:
            return self._no_data("indicators not warmed up")
        stance = Stance.BULLISH if votes > 0 else Stance.BEARISH if votes < 0 else Stance.NEUTRAL
        return AgentOpinion(
            agent=self.name,
            stance=stance,
            confidence_pct=min(90, 45 + 20 * abs(votes)),
            rationale="; ".join(reasons),
            evidence_tier=EvidenceTier.A,
            evidence_sources=frozenset(sources),
        )


class RiskContextAgent(Agent):
    """ATR-based volatility regime (context, not direction)."""

    name = "risk_context"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        highs, lows, closes = ohlcv_floats(ctx.series)
        last_atr = atr(highs, lows, closes)[-1] if closes else None
        last_close = closes[-1] if closes else None
        if last_atr is None or not last_close:
            return self._no_data("ATR could not be computed")
        atr_pct = last_atr / last_close * 100
        regime = "high" if atr_pct >= 3 else "low" if atr_pct < 1 else "medium"
        return AgentOpinion(
            agent=self.name,
            stance=Stance.NEUTRAL,  # regime context — does not contribute to direction
            confidence_pct=60,
            rationale=f"ATR/price {atr_pct:.1f}% → {regime} volatility regime",
            evidence_tier=EvidenceTier.A,
            evidence_sources=frozenset({"ind:atr14"}),
        )


# Band in which the average sentiment counts as neutral (buffer for source noise).
_SENTIMENT_NEUTRAL_BAND = 0.15


class NewsSentimentAgent(Agent):
    """Sentiment opinion from news/KAP disclosures.

    If there is **news with sentiment** for the symbol it derives a direction from the
    average sentiment (source score [-1..1]); otherwise it reports the presence of KAP
    disclosures as **NEUTRAL context** (it cannot infer sentiment locally). With neither,
    ``NO_DATA``.
    """

    name = "news_sentiment"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        scored = [n for n in ctx.news if n.sentiment is not None and self._relevant(ctx.symbol, n)]
        if scored:
            avg = sum(n.sentiment for n in scored if n.sentiment is not None) / len(scored)
            if avg >= _SENTIMENT_NEUTRAL_BAND:
                stance = Stance.BULLISH
            elif avg <= -_SENTIMENT_NEUTRAL_BAND:
                stance = Stance.BEARISH
            else:
                stance = Stance.NEUTRAL
            # Confidence: sentiment strength + news count (more news = a more reliable average).
            conf = min(85, 40 + round(abs(avg) * 40) + min(15, (len(scored) - 1) * 5))
            return AgentOpinion(
                agent=self.name,
                stance=stance,
                confidence_pct=conf,
                rationale=f"average sentiment {avg:+.2f} across {len(scored)} news item(s)",
                evidence_tier=EvidenceTier.B,
                evidence_sources=frozenset(
                    {"news:sentiment"} | {f"news:{n.provenance.source.lower()}" for n in scored}
                ),
            )
        if ctx.disclosures:
            return AgentOpinion(
                agent=self.name,
                stance=Stance.NEUTRAL,
                confidence_pct=50,
                rationale=(
                    f"{len(ctx.disclosures)} new disclosure(s) (sentiment needs deep analysis)"
                ),
                evidence_tier=EvidenceTier.B,
                evidence_sources=frozenset({"news:kap"}),
            )
        return self._no_data("no news/KAP disclosures")

    @staticmethod
    def _relevant(symbol: str, item: NewsItem) -> bool:
        """Does the news belong to the symbol? Empty ``symbols`` (untagged) counts as general."""
        return not item.symbols or symbol in item.symbols


# Intermarket relation: metric **trend direction** (+1 rising / -1 falling) → asset effect.
# Sign = the relation's correlation (-1 if inverse). Applied only to affected asset classes.
#   key → (effect sign, asset classes where it is meaningful)
_MACRO_RELATIONS: dict[str, tuple[int, frozenset[AssetClass]]] = {
    # Strong USD → pressure on dollar-denominated commodities/risk assets (inverse).
    "dxy": (-1, frozenset({AssetClass.METAL, AssetClass.CRYPTO})),
    # Real rate ↑ → pressure on non-yielding gold (inverse).
    "real_rate": (-1, frozenset({AssetClass.METAL})),
    # USDTRY ↑ → supports TRY-denominated gold/silver ETFs (GLDTR/GMSTR): gram ≈ oz × FX.
    "usdtry": (+1, frozenset({AssetClass.METAL})),
    # Risk appetite (SPX/Nasdaq) ↑ → same direction as crypto and equities (direct).
    "spx": (+1, frozenset({AssetClass.CRYPTO, AssetClass.EQUITY})),
    # VIX (fear) ↑ → pressure on risk assets (inverse).
    "vix": (-1, frozenset({AssetClass.CRYPTO, AssetClass.EQUITY})),
}


class MacroIntermarketAgent(Agent):
    """Intermarket/macro opinion.

    ``macro`` values are each metric's **trend direction** (positive=rising, negative=falling;
    magnitude = strength). It derives a direction from known relations (DXY/real rate inverse
    to gold, SPX direct with crypto…) and ``asset_class``. Unknown relation → NEUTRAL context;
    no data → NO_DATA.
    """

    name = "macro_intermarket"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        if not ctx.macro:
            return self._no_data("no intermarket/macro data")

        score = 0.0
        reasons: list[str] = []
        used: set[str] = set()
        if ctx.asset_class is not None:
            for raw_key, trend in ctx.macro.items():
                relation = _MACRO_RELATIONS.get(raw_key.lower())
                if relation is None or trend == 0:
                    continue
                sign, classes = relation
                if ctx.asset_class not in classes:
                    continue
                contrib = sign * trend
                score += contrib
                used.add(f"macro:{raw_key.lower()}")
                arrow = "↑" if trend > 0 else "↓"
                effect = "support" if contrib > 0 else "pressure"
                reasons.append(f"{raw_key.upper()}{arrow}→{effect}")

        if reasons:
            stance = (
                Stance.BULLISH if score > 0 else Stance.BEARISH if score < 0 else Stance.NEUTRAL
            )
            return AgentOpinion(
                agent=self.name,
                stance=stance,
                confidence_pct=min(80, 45 + round(abs(score) * 15)),
                rationale="; ".join(reasons),
                evidence_tier=EvidenceTier.B,
                evidence_sources=frozenset(used),
            )

        keys = ", ".join(sorted(ctx.macro))
        return AgentOpinion(
            agent=self.name,
            stance=Stance.NEUTRAL,
            confidence_pct=50,
            rationale=f"Macro context available ({keys}) — no direction could be derived",
            evidence_tier=EvidenceTier.B,
            evidence_sources=frozenset(f"macro:{k.lower()}" for k in ctx.macro),
        )


# Fear&Greed contrarian thresholds (common reading: extremes are reversal zones — this is
# a hypothesis too; the evaluator scores it per method).
_FNG_EXTREME_FEAR = 25.0
_FNG_EXTREME_GREED = 75.0


class PositioningAgent(Agent):
    """Positioning opinion — crypto Fear&Greed for now; COT/on-chain later.

    Contrarian reading: ≤25 extreme fear → rebound potential (BULLISH); ≥75 extreme greed →
    pullback risk (BEARISH); in between NEUTRAL context. Meaningful for crypto assets only.
    """

    name = "positioning"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        fng = (ctx.positioning or {}).get("crypto_fng")
        if fng is None:
            return self._no_data("no COT/on-chain/F&G data")
        if ctx.asset_class is not AssetClass.CRYPTO:
            return self._no_data("F&G is only meaningful for crypto (no COT/on-chain yet)")
        evidence = frozenset({"positioning:fng"})
        if fng <= _FNG_EXTREME_FEAR:
            return AgentOpinion(
                agent=self.name,
                stance=Stance.BULLISH,
                confidence_pct=60,
                rationale=f"Fear&Greed {fng:.0f} — extreme fear (contrarian rebound potential)",
                evidence_tier=EvidenceTier.B,
                evidence_sources=evidence,
            )
        if fng >= _FNG_EXTREME_GREED:
            return AgentOpinion(
                agent=self.name,
                stance=Stance.BEARISH,
                confidence_pct=60,
                rationale=f"Fear&Greed {fng:.0f} — extreme greed (pullback risk)",
                evidence_tier=EvidenceTier.B,
                evidence_sources=evidence,
            )
        return AgentOpinion(
            agent=self.name,
            stance=Stance.NEUTRAL,
            confidence_pct=50,
            rationale=f"Fear&Greed {fng:.0f} — neutral zone (no extreme positioning)",
            evidence_tier=EvidenceTier.B,
            evidence_sources=evidence,
        )


class PortfolioAgent(Agent):
    """Portfolio exposure/correlation — portfolio state (L4) is not available yet."""

    name = "portfolio"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        return self._no_data("portfolio state (L4) not available yet")


def default_agents() -> list[Agent]:
    """Return the standard agent set."""
    return [
        TechnicalAgent(),
        RiskContextAgent(),
        NewsSentimentAgent(),
        MacroIntermarketAgent(),
        PositioningAgent(),
        PortfolioAgent(),
    ]
