"""SignalAggregator — L4 decision layer: merges agent opinions through confluence.

Produces a ``Proposal{direction, strength, horizon, rationale}``. Runs every agent on an
:class:`~argus.brain.agents.AnalysisContext` and reduces the resulting
:class:`~argus.brain.agents.AgentOpinion` objects to a single
:class:`~argus.core.contracts.Proposal` by **weighted voting** (by confidence).

**Independence rule:** opinions fed by the same evidence are not independent — directional
opinions whose ``evidence_sources`` sets overlap with Jaccard ≥ 0.5 form one **cluster**, and
a cluster casts ONE VOTE (direction: confidence-weighted majority inside the cluster;
confidence: the winning side's max). Confluence% is computed over the number of clusters
(independent evidence) → the consensus percentage reflects evidence diversity, not agent
count. An opinion that declares no evidence (empty set) cannot be clustered and counts as
independent.

Honesty rule: direction comes only from **directional** (BULLISH/BEARISH) opinions; NEUTRAL
opinions are context (they do not contribute to direction). If no agent has data, direction
is ``NO_DATA`` and ``data_status = PENDING`` (DATA PENDING). **No execution here** — the
output is an informational research proposal only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from argus.brain.agents import Agent, AgentOpinion, AnalysisContext, default_agents
from argus.core.contracts import OpinionRef, Proposal
from argus.core.enums import Stance
from argus.core.schemas import DATA_PENDING, DataStatus

if TYPE_CHECKING:
    from argus.brain.budget import CostStore
    from argus.config.settings import Settings

# Direction → human-readable label (for the rationale text).
_DIR_LABEL: dict[Stance, str] = {
    Stance.BULLISH: "up",
    Stance.BEARISH: "down",
    Stance.NEUTRAL: "neutral",
    Stance.NO_DATA: "no data",
}

# Opinions whose evidence sets overlap at or above this threshold are correlated (one vote).
JACCARD_THRESHOLD = 0.5


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity; an empty set declares no evidence → overlap is unprovable (0)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cluster_directional(opinions: list[AgentOpinion]) -> list[list[AgentOpinion]]:
    """Clusters directional opinions by evidence overlap (deterministic union-find).

    List order is fixed (agent-set order) → same input, same clustering. Transitivity comes
    from union-find: if A~B and B~C then A, B, C form one cluster.
    """
    n = len(opinions)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(opinions[i].evidence_sources, opinions[j].evidence_sources) >= (
                JACCARD_THRESHOLD
            ):
                parent[find(j)] = find(i)

    groups: dict[int, list[AgentOpinion]] = {}
    order: list[int] = []  # stable order: clusters by first member's list position
    for i, opinion in enumerate(opinions):
        root = find(i)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(opinion)
    return [groups[root] for root in order]


@dataclass(frozen=True)
class _ClusterVote:
    """The single vote of one evidence cluster."""

    direction: Stance  # confidence-weighted majority inside the cluster
    confidence: int  # max confidence of the winning side
    members: tuple[AgentOpinion, ...]

    @property
    def label(self) -> str:
        return "+".join(o.agent for o in self.members)


def _cluster_vote(members: list[AgentOpinion]) -> _ClusterVote | None:
    """Builds the cluster vote; if weights inside the cluster tie, it abstains (None)."""
    bull = [o for o in members if o.stance is Stance.BULLISH]
    bear = [o for o in members if o.stance is Stance.BEARISH]
    bull_w = sum(o.confidence_pct for o in bull)
    bear_w = sum(o.confidence_pct for o in bear)
    if bull_w == bear_w:
        return None  # same evidence pulls both ways → conflicting evidence, no vote
    winners = bull if bull_w > bear_w else bear
    direction = Stance.BULLISH if bull_w > bear_w else Stance.BEARISH
    return _ClusterVote(
        direction=direction,
        confidence=max(o.confidence_pct for o in winners),
        members=tuple(members),
    )


class SignalAggregator:
    """Runs the agent set and merges opinions into a :class:`Proposal` with a confluence score."""

    def __init__(self, agents: Sequence[Agent] | None = None) -> None:
        self._agents = tuple(agents) if agents is not None else tuple(default_agents())

    @property
    def agents(self) -> tuple[Agent, ...]:
        return self._agents

    def aggregate(self, ctx: AnalysisContext) -> Proposal:
        """Run the agents, merge their opinions and return a Proposal."""
        opinions: list[AgentOpinion] = []
        for agent in self._agents:
            try:
                opinions.append(agent.assess(ctx))
            except Exception as exc:
                # One failing expert (LLM included) must not take down the whole confluence
                # group. The error text may carry external data/secrets, so only its type is
                # reported; no data is invented.
                opinions.append(
                    AgentOpinion(
                        agent=agent.name,
                        stance=Stance.NO_DATA,
                        confidence_pct=0,
                        rationale=f"DATA PENDING: expert error ({type(exc).__name__}).",
                        data_status=DataStatus.PENDING,
                    )
                )
        contributors = tuple(
            OpinionRef(agent=o.agent, stance=o.stance, confidence_pct=o.confidence_pct)
            for o in opinions
        )
        return self._combine(ctx, opinions, contributors)

    def _combine(
        self,
        ctx: AnalysisContext,
        opinions: list[AgentOpinion],
        contributors: tuple[OpinionRef, ...],
    ) -> Proposal:
        any_data = any(o.stance is not Stance.NO_DATA for o in opinions)
        data_status = DataStatus.OK if any_data else DataStatus.PENDING

        directional = [o for o in opinions if o.stance in (Stance.BULLISH, Stance.BEARISH)]

        # Correlated opinions share one vote — cluster by evidence sets, then vote per cluster.
        clusters = _cluster_directional(directional)
        votes = [v for v in (_cluster_vote(members) for members in clusters) if v is not None]
        cluster_note = self._cluster_note(clusters)

        # No directional vote → neutral (if any data) or DATA PENDING (no data at all).
        if not votes:
            if any_data:
                direction = Stance.NEUTRAL
                rationale = (
                    "No directional view; agents are neutral or provide context only."
                    if not directional
                    else "Directional opinions conflict within the same evidence → undecided."
                )
            else:
                direction = Stance.NO_DATA
                rationale = (
                    f"{DATA_PENDING}: not enough evidence for a direction "
                    "(all agents are waiting for data)."
                )
            return self._proposal(
                ctx, direction, 0, 0, rationale + cluster_note, contributors, data_status
            )

        bull_votes = [v for v in votes if v.direction is Stance.BULLISH]
        bear_votes = [v for v in votes if v.direction is Stance.BEARISH]
        bull_w = sum(v.confidence for v in bull_votes)
        bear_w = sum(v.confidence for v in bear_votes)

        # Weight tie → undecided (NEUTRAL, low confidence).
        if bull_w == bear_w:
            return self._proposal(
                ctx,
                Stance.NEUTRAL,
                0,
                50,
                "Directional evidence clusters are balanced (bull↔bear) → undecided."
                + cluster_note,
                contributors,
                data_status,
            )

        direction = Stance.BULLISH if bull_w > bear_w else Stance.BEARISH
        winners = bull_votes if direction is Stance.BULLISH else bear_votes
        strength = round(sum(v.confidence for v in winners) / len(winners))
        # Confluence% over independent evidence clusters — agent count cannot inflate consensus.
        confluence = round(len(winners) / len(votes) * 100)
        agree = ", ".join(f"{v.label}({v.confidence}%)" for v in winners)
        rationale = (
            f"{len(winners)}/{len(votes)} evidence clusters point {_DIR_LABEL[direction]} "
            f"(consensus {confluence}%, strength {strength}%): {agree}." + cluster_note
        )
        return self._proposal(
            ctx, direction, strength, confluence, rationale, contributors, data_status
        )

    @staticmethod
    def _cluster_note(clusters: list[list[AgentOpinion]]) -> str:
        """Writes multi-member clusters into the rationale (transparency: who shared a vote)."""
        merged = [c for c in clusters if len(c) > 1]
        if not merged:
            return ""
        parts = "; ".join(
            "+".join(o.agent for o in c) + " same evidence → one vote" for c in merged
        )
        return f" Clusters: {parts}."

    @staticmethod
    def _proposal(
        ctx: AnalysisContext,
        direction: Stance,
        strength: int,
        confluence: int,
        rationale: str,
        contributors: tuple[OpinionRef, ...],
        data_status: DataStatus,
    ) -> Proposal:
        return Proposal(
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            direction=direction,
            strength=strength,
            confluence_score=confluence,
            rationale=rationale,
            contributors=contributors,
            data_status=data_status,
        )


def default_aggregator() -> SignalAggregator:
    """Returns an aggregator with the standard **deterministic** agent set (no LLM)."""
    return SignalAggregator()


def build_aggregator(
    settings: Settings | None = None, *, cost_store: CostStore | None = None
) -> SignalAggregator:
    """Builds an aggregator with deterministic agents + the **LLM-assisted technical agent**.

    The LLM agent is added **alongside** the deterministic ``TechnicalAgent`` → both vote in
    the confluence (deterministic = safe floor, LLM = richer commentary). If the LLM agent is in
    demo mode (no real provider) it is **not added** — a constant NO_DATA vote must not pollute
    the confluence (honesty). The cost governor lives in the LLM agent's router; when
    ``cost_store`` is given, spend is written to a persistent ledger (a cap shared across
    processes).
    """
    from argus.brain.expert_bist import build_bist_expert_agent
    from argus.brain.llm_agent import build_llm_technical_agent
    from argus.config.settings import get_settings

    cfg = settings or get_settings()
    agents: list[Agent] = list(default_agents())
    llm_agent = build_llm_technical_agent(cfg, cost_store=cost_store)
    if not llm_agent.demo:  # add only when a real LLM (Claude/Ollama) is connected
        agents.append(llm_agent)
    # BIST expert — joins via a settings flag; not added in demo mode (no NO_DATA pollution).
    # Its output enters the confluence → proposal log + evaluator coverage come automatically.
    if cfg.enable_exchange_expert:
        expert = build_bist_expert_agent(cfg, cost_store=cost_store)
        if not expert.demo:
            agents.append(expert)
    return SignalAggregator(agents)
