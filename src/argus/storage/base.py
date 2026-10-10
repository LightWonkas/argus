"""Storage interfaces and value types (cross-cutting storage / memory layer).

Memory layers: time series · event/alert + decision journal · self-monitoring (hit rate).
Currently SQLite (stdlib); PostgreSQL+TimescaleDB once it grows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from argus.core.contracts import AnalysisOutput, OHLCVSeries, Proposal
from argus.platform.triggers import TriggerSignal


@dataclass(frozen=True)
class StoredAnalysis:
    """An analysis recorded in the journal (id + time + full output)."""

    id: int
    created_at: datetime
    analysis: AnalysisOutput


@dataclass(frozen=True)
class StoredProposal:
    """A confluence proposal recorded in the journal (lean fields for self-monitoring).

    The minimum set the evaluator needs: direction + time + symbol/timeframe. The full
    ``Proposal`` object is not rebuilt (contributors JSON stays archived; panel/report read it).
    """

    id: int
    symbol: str
    timeframe: str  # Timeframe.value
    created_at: datetime
    direction: str  # Stance.value (bullish|bearish|neutral|no_data)
    strength: int
    confluence_pct: int
    source: str  # watch | analyze | api (stored values)


@dataclass(frozen=True)
class HitRate:
    """Self-monitoring hit-rate summary."""

    correct: int
    total: int

    @property
    def rate(self) -> float:
        """Hit rate [0..1] (0.0 when there are no outcomes)."""
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class OutcomeBucket:
    """Hit rate v2 aggregation bucket — hits/n per (series, metric, variant).

    ``ref_type``: ``analysis`` (LLM analysis series) | ``proposal`` (confluence series).
    ``variant``: ``system`` | baselines (``random``/``bullish``/``momentum``).
    Only final verdicts are counted (correct/incorrect; skip is excluded).
    """

    ref_type: str
    metric: str  # touch | close
    variant: str  # system | random | bullish | momentum
    correct: int
    total: int

    @property
    def rate(self) -> float:
        """Hit rate [0..1] (0.0 when there are no outcomes)."""
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class AlertRecord:
    """A single trigger/alert derived from the analysis journal (for the panel feed)."""

    analysis_id: int
    created_at: datetime
    symbol: str
    severity: str
    kind: str
    message: str


@dataclass(frozen=True)
class NewsRecord:
    """An archived news item / KAP disclosure (deduplicated; for the panel news feed).

    Router labels: ``route_status`` None = never went through the router; ``"ok"`` = labeled;
    ``"budget_exceeded"``/``"error"`` = could not be classified (label fields stay None).
    """

    source: str
    ext_id: str
    kind: str  # "news" | "disclosure"
    title: str
    url: str | None
    symbol: str | None
    published_at: datetime
    sentiment: float | None = None  # source sentiment (if any; NewsSentimentAgent input)
    importance: str | None = None  # Severity.value (only when route_status == "ok")
    relevant: bool | None = None
    target_agent: str | None = None
    confidence_pct: int | None = None
    route_status: str | None = None


@dataclass(frozen=True)
class ChatTurn:
    """A single message in the Argus Copilot chat history (user/assistant)."""

    id: int
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime


@dataclass(frozen=True)
class FeedbackCount:
    """Vote counter of one breakdown key (symbol/severity)."""

    useful: int
    total: int


@dataclass(frozen=True)
class FeedbackSummary:
    """User-value summary: are alerts/analyses actually useful (👍/👎).

    Signal hit rate is necessary but not sufficient — the real measure is the user saying
    "useful".
    """

    useful: int
    total: int
    days: int
    by_symbol: dict[str, FeedbackCount]
    by_severity: dict[str, FeedbackCount]

    @property
    def useful_pct(self) -> float | None:
        """Useful ratio in % (None without votes — a zero is never invented)."""
        return round(self.useful / self.total * 100, 1) if self.total else None


class AnalysisJournal(Protocol):
    """Write interface of the analysis/decision journal (used by WatchLoop)."""

    def record_analysis(
        self, analysis: AnalysisOutput, *, signals: Sequence[TriggerSignal] = ()
    ) -> int: ...


class OHLCVStore(Protocol):
    """OHLCV (point-in-time) write interface — the watch loop persists scanned data here.

    This way the dashboard (``/api/quotes``/chart) and later backtests read **real** data.
    SqliteStore implements it; tests can inject a fake writer (no network).
    """

    def save_series(self, series: OHLCVSeries) -> int: ...


class JournalReader(Protocol):
    """Analysis/alert/hit-rate **read** interface (used by the dashboard API).

    SqliteStore implements it; tests can inject a fake reader (no network).
    """

    def recent_analyses(
        self, *, symbol: str | None = None, limit: int = 20
    ) -> list[StoredAnalysis]: ...

    def recent_alerts(self, *, limit: int = 50) -> list[AlertRecord]: ...

    def recent_news(self, *, source: str | None = None, limit: int = 50) -> list[NewsRecord]: ...

    def hit_rate(self, *, symbol: str | None = None) -> HitRate: ...

    def hit_rate_v2(self) -> list[OutcomeBucket]: ...


class ProposalStore(Protocol):
    """Write interface of the confluence proposal journal — self-monitoring input.

    WatchLoop/bot write the Proposal they produce here (**fail-safe**: a write error does
    not abort the scan); the evaluator scores it with the same metric as analyses.
    """

    def save_proposal(self, proposal: Proposal, *, source: str) -> int: ...


class ChatStore(Protocol):
    """Copilot chat history write/read interface (dashboard chat endpoint)."""

    def save_chat(self, role: str, content: str) -> int: ...

    def recent_chat(self, *, limit: int = 50) -> list[ChatTurn]: ...


class FeedbackReader(Protocol):
    """User feedback read interface (``/api/feedback/summary``)."""

    def feedback_summary(
        self, *, days: int = 30, now: datetime | None = None
    ) -> FeedbackSummary: ...


class FeedbackWriter(Protocol):
    """User feedback **write** interface (web ``POST /api/feedback`` + Telegram).

    The Telegram bot and the web panel share the same ``save_feedback`` contract → a single
    store wherever the vote comes from (idempotent: the same user may change their mind).
    """

    def save_feedback(
        self,
        ref_type: str,
        ref_id: str,
        user_id: str,
        *,
        useful: bool,
        symbol: str | None = None,
        severity: str | None = None,
        created_at: datetime | None = None,
    ) -> None: ...
