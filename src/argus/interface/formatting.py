"""Telegram message formatting — plain English text (avoids Markdown escaping pitfalls).

Turns AnalysisOutput and trigger alerts into readable messages. Every analysis message
carries the "not financial advice" note.
"""

from __future__ import annotations

from argus.core.contracts import AnalysisOutput, Proposal
from argus.core.enums import Stance, Timeframe
from argus.platform.triggers import TriggerSignal

_TELEGRAM_MAX = 4096

# Direction → display label (for the confluence summary).
_DIR_LABEL: dict[Stance, str] = {
    Stance.BULLISH: "up ▲",
    Stance.BEARISH: "down ▼",
    Stance.NEUTRAL: "neutral ◆",
    Stance.NO_DATA: "data pending",
}


def _truncate(text: str, limit: int = _TELEGRAM_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_analysis(analysis: AnalysisOutput) -> str:
    """AnalysisOutput → Telegram message."""
    lines = [
        f"📊 {analysis.symbol} · {analysis.timeframe.value} analysis",
        "",
        analysis.summary,
        "",
        f"Direction: {analysis.bias or 'unclear'}  |  "
        f"Evidence: [{analysis.evidence_tier.value}]  "
        f"|  Confidence: {analysis.confidence_pct}%",
        f"Invalidation: {analysis.invalidation}",
    ]
    if analysis.scenarios:
        lines.append("")
        lines.append("Scenarios:")
        lines.extend(f"  • {s.name} ({s.probability_pct}%): {s.detail}" for s in analysis.scenarios)
    lines.append("")
    lines.append(f"Model: {analysis.model}")
    lines.append(f"⚠️ {analysis.disclaimer}")
    return _truncate("\n".join(lines))


def format_proposal(p: Proposal) -> str:
    """L4 confluence proposal → short summary (six agent votes + transparency)."""
    lines = [
        f"🧩 Confluence · {p.symbol} · {p.timeframe.value}",
        f"Direction: {_DIR_LABEL.get(p.direction, p.direction.value)}  |  "
        f"Strength: {p.strength}%  |  Agreement: {p.confluence_score}%",
        p.rationale,
    ]
    if p.contributors:
        lines.append("")
        lines.append("Agents:")
        lines.extend(
            f"  • {c.agent}: {_DIR_LABEL.get(c.stance, c.stance.value)} ({c.confidence_pct}%)"
            for c in p.contributors
        )
    lines.append("")
    lines.append(f"⚠️ {p.disclaimer}")
    return _truncate("\n".join(lines))


def format_analysis_with_proposal(analysis: AnalysisOutput, proposal: Proposal) -> str:
    """Combine the LLM analysis and the confluence proposal into one Telegram message."""
    return _truncate(f"{format_analysis(analysis)}\n\n────────\n{format_proposal(proposal)}")


def format_alert(symbol: str, timeframe: Timeframe, signals: list[TriggerSignal]) -> str:
    """Trigger signals → short alert message."""
    if not signals:
        return f"🔔 {symbol} · {timeframe.value}: no trigger."
    # Most severe signal first
    ordered = sorted(signals, key=lambda s: s.severity.rank, reverse=True)
    top = ordered[0].severity.value.upper()
    lines = [f"🔔 {symbol} · {timeframe.value} trigger ({top})", ""]
    lines.extend(f"  • [{s.severity.value}] {s.message}" for s in ordered)
    return _truncate("\n".join(lines))
