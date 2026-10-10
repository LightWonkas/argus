"""Daily briefing — collects deferred (digest) alerts into a single summary message.

Plain English text; every briefing carries the "not financial advice" note.
"""

from __future__ import annotations

from datetime import datetime

from argus.core.contracts import NOT_ADVICE, AnalysisOutput

_TELEGRAM_MAX = 4096


def analysis_brief_line(analysis: AnalysisOutput) -> str:
    """One-line briefing summary of an analysis."""
    summary = analysis.summary if len(analysis.summary) <= 100 else analysis.summary[:99] + "…"
    bias = analysis.bias or "unclear"
    return (
        f"• {analysis.symbol} · {analysis.timeframe.value} "
        f"[{analysis.evidence_tier.value}] {analysis.confidence_pct}% {bias} — {summary}"
    )


def format_briefing(now: datetime, entries: list[str], *, title: str = "Daily briefing") -> str:
    """Build the briefing message from digest lines."""
    stamp = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    body = "\n".join(entries) if entries else "  (no alerts to report for this period)"
    message = f"📋 {title} · {stamp}\n\n{body}\n\n⚠️ {NOT_ADVICE}"
    return message if len(message) <= _TELEGRAM_MAX else message[: _TELEGRAM_MAX - 1] + "…"
