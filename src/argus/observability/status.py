"""Status/observability summary — for /status and --status.

Collects the version, environment, authorized-chat count, LLM budget, hit rate and the
sources we are flying blind on into a single text. :func:`format_status` is pure (and
tested); the glue that gathers the values lives in ``interface.factory.build_status``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from argus.core.contracts import NOT_ADVICE


@dataclass(frozen=True)
class SystemStatus:
    """Snapshot of the system status."""

    version: str
    env: str
    authorized_count: int
    budget_remaining_usd: float | None = None
    budget_total_usd: float | None = None
    hit_correct: int | None = None
    hit_total: int | None = None
    degraded_sources: tuple[str, ...] = field(default_factory=tuple)


def format_status(status: SystemStatus) -> str:
    """SystemStatus → /status message."""
    lines = [
        f"🟢 Argus {status.version} · env={status.env}",
        f"Authorized chats: {status.authorized_count}",
    ]
    if status.budget_remaining_usd is not None:
        total = f" / ${status.budget_total_usd:.2f}" if status.budget_total_usd is not None else ""
        lines.append(f"LLM budget (remaining): ${status.budget_remaining_usd:.2f}{total}")
    elif status.budget_total_usd is not None:
        lines.append(f"LLM budget (daily cap): ${status.budget_total_usd:.2f}")
    if status.hit_total is not None:
        if status.hit_total == 0:
            lines.append("Hit rate: no outcomes yet")
        else:
            rate = (status.hit_correct or 0) / status.hit_total * 100
            lines.append(f"Hit rate: {status.hit_correct}/{status.hit_total} ({rate:.0f}%)")
    if status.degraded_sources:
        lines.append(f"⚠️ Flying blind on sources: {', '.join(status.degraded_sources)}")
    lines.append(f"No live orders — no real-money transactions. {NOT_ADVICE}")
    return "\n".join(lines)
