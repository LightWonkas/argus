"""Tests for SignalAggregator (L4) confluence synthesis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argus.brain.agents import Agent, AgentOpinion, AnalysisContext
from argus.core.contracts import NOT_ADVICE, OHLCVBar, OHLCVSeries
from argus.core.enums import Stance, Timeframe
from argus.core.schemas import DataStatus, Provenance
from argus.decision import SignalAggregator, default_aggregator

BASE = datetime(2026, 5, 29, tzinfo=UTC)

# Empirical series for which the technical agent returns BULLISH (see test_agents).
_BULL = [100.0 + i for i in range(26)] + [
    124,
    122,
    120,
    121,
    123,
    122,
    124,
    123,
    122,
    124,
    123,
    125,
    124,
    123,
    125,
    124,
]


def _ctx(closes: list[float] | None = None) -> AnalysisContext:
    closes = closes if closes is not None else [100.0, 101.0]
    bars = tuple(
        OHLCVBar(
            timestamp=BASE + timedelta(hours=i),
            open=closes[i - 1] if i > 0 else c,
            high=max(closes[i - 1] if i > 0 else c, c) + 1,
            low=min(closes[i - 1] if i > 0 else c, c) - 1,
            close=c,
            volume=1,
        )
        for i, c in enumerate(closes)
    )
    prov = Provenance(source="test", timestamp=bars[-1].timestamp, confidence=0.9)
    return AnalysisContext(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        series=OHLCVSeries(symbol="BTC/USDT", timeframe=Timeframe.H1, bars=bars, provenance=prov),
    )


class FakeAgent(Agent):
    """Test agent that returns a fixed opinion."""

    def __init__(
        self,
        name: str,
        stance: Stance,
        confidence_pct: int = 50,
        data_status: DataStatus = DataStatus.OK,
        evidence: frozenset[str] = frozenset(),
    ) -> None:
        self._name = name
        self._stance = stance
        self._conf = confidence_pct
        self._status = data_status
        self._evidence = evidence

    @property
    def name(self) -> str:
        return self._name

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        return AgentOpinion(
            agent=self._name,
            stance=self._stance,
            confidence_pct=self._conf,
            rationale="fixed",
            data_status=self._status,
            evidence_sources=self._evidence,
        )


def _no_data(name: str) -> FakeAgent:
    return FakeAgent(name, Stance.NO_DATA, 0, DataStatus.PENDING)


class BrokenAgent(Agent):
    name = "broken"

    def assess(self, ctx: AnalysisContext) -> AgentOpinion:
        raise RuntimeError("provider secret must not leak")


def test_bullish_majority_confluence() -> None:
    agg = SignalAggregator(
        [
            FakeAgent("a", Stance.BULLISH, 60),
            FakeAgent("b", Stance.BULLISH, 80),
            FakeAgent("c", Stance.BEARISH, 50),
        ]
    )
    p = agg.aggregate(_ctx())
    assert p.direction is Stance.BULLISH
    assert p.strength == 70  # (60+80)/2
    assert p.confluence_score == 67  # 2/3 directional agents agree
    assert p.data_status is DataStatus.OK


def test_bearish_majority() -> None:
    agg = SignalAggregator([FakeAgent("a", Stance.BEARISH, 70), FakeAgent("b", Stance.BULLISH, 40)])
    p = agg.aggregate(_ctx())
    assert p.direction is Stance.BEARISH
    assert p.strength == 70
    assert p.confluence_score == 50  # 1/2 directional agents


def test_weight_tie_is_neutral() -> None:
    agg = SignalAggregator([FakeAgent("a", Stance.BULLISH, 50), FakeAgent("b", Stance.BEARISH, 50)])
    p = agg.aggregate(_ctx())
    assert p.direction is Stance.NEUTRAL
    assert p.strength == 0


def test_neutral_context_only_no_direction() -> None:
    # Only NEUTRAL (context) opinions → no direction, but data is present.
    agg = SignalAggregator([FakeAgent("risk", Stance.NEUTRAL, 60)])
    p = agg.aggregate(_ctx())
    assert p.direction is Stance.NEUTRAL
    assert p.confluence_score == 0
    assert p.data_status is DataStatus.OK


def test_all_no_data_is_pending() -> None:
    agg = SignalAggregator([_no_data("a"), _no_data("b"), _no_data("c")])
    p = agg.aggregate(_ctx())
    assert p.direction is Stance.NO_DATA
    assert p.data_status is DataStatus.PENDING
    assert p.strength == 0
    assert "DATA PENDING" in p.rationale


def test_contributors_and_disclaimer() -> None:
    agents = [FakeAgent("a", Stance.BULLISH, 60), FakeAgent("b", Stance.NEUTRAL, 50)]
    p = SignalAggregator(agents).aggregate(_ctx())
    assert len(p.contributors) == 2
    assert {c.agent for c in p.contributors} == {"a", "b"}
    assert p.disclaimer == NOT_ADVICE


def test_one_agent_error_becomes_no_data_without_breaking_group() -> None:
    p = SignalAggregator([BrokenAgent(), FakeAgent("technical", Stance.BULLISH, 70)]).aggregate(
        _ctx()
    )
    assert p.direction is Stance.BULLISH
    broken = next(c for c in p.contributors if c.agent == "broken")
    assert broken.stance is Stance.NO_DATA
    assert "secret" not in p.rationale


def test_default_aggregator_integration_bullish() -> None:
    # Real agent set, bullish series: TechnicalAgent→BULLISH, the rest NEUTRAL/NO_DATA.
    p = default_aggregator().aggregate(_ctx(_BULL))
    assert p.direction is Stance.BULLISH
    assert p.confluence_score == 100  # a single directional agent, and it is BULLISH
    assert p.strength == 65
    assert len(p.contributors) == 6


# ── evidence deduplication — correlated opinions share one vote ──────────────

_IND = frozenset({"ind:rsi14", "ind:ema12", "ind:ema26"})
_NEWS = frozenset({"news:kap"})
_MACRO = frozenset({"macro:dxy"})


def test_same_evidence_two_agents_count_as_one_vote() -> None:
    """Two agents on the same evidence ≈ one agent's weight (consensus is not inflated)."""
    duo = SignalAggregator(
        [
            FakeAgent("technical", Stance.BULLISH, 60, evidence=_IND),
            FakeAgent("llm_technical", Stance.BULLISH, 80, evidence=_IND),  # SAME evidence
            FakeAgent("news", Stance.BEARISH, 70, evidence=_NEWS),
        ]
    ).aggregate(_ctx())
    solo = SignalAggregator(
        [
            FakeAgent(
                "technical", Stance.BULLISH, 80, evidence=_IND
            ),  # single agent, max confidence
            FakeAgent("news", Stance.BEARISH, 70, evidence=_NEWS),
        ]
    ).aggregate(_ctx())

    # Two correlated bulls = one evidence cluster: consensus 1/2 = 50% (a naive count: 67%).
    assert duo.confluence_score == 50
    assert duo.confluence_score == solo.confluence_score
    assert duo.direction is solo.direction is Stance.BULLISH
    assert duo.strength == solo.strength == 80  # cluster confidence = max(60, 80)
    # Transparency: the cluster is spelled out in the rationale.
    assert "technical+llm_technical same evidence → one vote" in duo.rationale


def test_different_evidence_two_agents_beat_one() -> None:
    """Two agents on different evidence > one agent (independent confirmation counts)."""
    p = SignalAggregator(
        [
            FakeAgent("technical", Stance.BULLISH, 60, evidence=_IND),
            FakeAgent("macro", Stance.BULLISH, 60, evidence=_MACRO),  # DIFFERENT evidence
            FakeAgent("news", Stance.BEARISH, 70, evidence=_NEWS),
        ]
    ).aggregate(_ctx())
    assert p.direction is Stance.BULLISH
    assert p.confluence_score == 67  # 2/3 independent evidence clusters agree
    assert "one vote" not in p.rationale  # no cluster merge


def test_partial_overlap_below_threshold_stays_independent() -> None:
    # Jaccard < 0.5 (1 shared / 4 union = 0.25) → separate votes.
    a = frozenset({"ind:rsi14", "ind:ema12"})
    b = frozenset({"ind:rsi14", "macro:dxy", "news:kap"})
    p = SignalAggregator(
        [
            FakeAgent("x", Stance.BULLISH, 60, evidence=a),
            FakeAgent("y", Stance.BULLISH, 60, evidence=b),
        ]
    ).aggregate(_ctx())
    assert p.confluence_score == 100  # two independent clusters, both bullish


def test_conflicted_cluster_abstains() -> None:
    # The same evidence pulling both ways is conflicting → the cluster abstains.
    p = SignalAggregator(
        [
            FakeAgent("x", Stance.BULLISH, 60, evidence=_IND),
            FakeAgent("y", Stance.BEARISH, 60, evidence=_IND),
        ]
    ).aggregate(_ctx())
    assert p.direction is Stance.NEUTRAL
    assert "conflict within the same evidence" in p.rationale


def test_empty_evidence_cannot_cluster() -> None:
    # Opinions that report no evidence cannot cluster → they count as independent.
    p = SignalAggregator(
        [
            FakeAgent("a", Stance.BULLISH, 60),
            FakeAgent("b", Stance.BULLISH, 70),
        ]
    ).aggregate(_ctx())
    assert p.confluence_score == 100
    assert "one vote" not in p.rationale
