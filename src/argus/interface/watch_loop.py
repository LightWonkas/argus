"""WatchLoop — the watch loop orchestrator (L6).

Processes one watch item end to end: fetch prices → Data Quality Gate → trigger → brain
wake-up gate (cost governor) → cooldown/dedup → LLM analysis → Telegram notification →
event publication. Session-aware (skips closed markets). Every dependency is injected, so
it is tested without the network.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from argus.brain.agents import AnalysisContext
from argus.brain.analyst import TechnicalAnalyst
from argus.brain.router import TaskTier
from argus.config.models import WatchlistItem
from argus.core.contracts import AnalysisOutput, Disclosure, NewsItem, OHLCVSeries, Proposal
from argus.core.enums import AssetClass, Severity, Timeframe
from argus.core.errors import CircuitOpenError
from argus.core.events import Event, EventType
from argus.core.resilience import CircuitBreaker, CircuitState, retry
from argus.core.schemas import DataStatus, utcnow
from argus.decision.aggregator import SignalAggregator, default_aggregator
from argus.ingestion.base import MarketDataAdapter
from argus.interface.cooldown import CooldownTracker
from argus.interface.dispatcher import AlertDispatcher
from argus.interface.feedback import feedback_keyboard
from argus.interface.formatting import format_analysis_with_proposal, format_proposal
from argus.interface.notifier import TelegramNotifier
from argus.interface.prioritization import alert_severity
from argus.interface.sessions import is_market_open
from argus.observability.logging import get_logger
from argus.platform.event_bus import EventBus
from argus.platform.quality import QualityGate
from argus.platform.triggers import TriggerEngine, TriggerSignal, should_wake_brain
from argus.storage.base import AnalysisJournal, OHLCVStore, ProposalStore

_log = get_logger("argus.interface.watch_loop")


class HealthCheckpointStore(Protocol):
    """Minimal store contract carrying cross-process health heartbeats."""

    def set_checkpoint(self, key: str, value: str) -> None: ...


class ScanStatus(StrEnum):
    """Status of a scan result."""

    CLOSED = "closed"  # market closed
    SKIPPED = "skipped"  # no adapter
    ERROR = "error"  # data fetch error
    DEGRADED = "degraded"  # source circuit breaker open (flying blind)
    DATA_PENDING = "data_pending"  # insufficient quality → DATA PENDING
    NO_TRIGGER = "no_trigger"
    LOW_SEVERITY = "low_severity"  # triggered, but not enough to wake the brain
    COOLDOWN = "cooldown"  # inside the cooldown window, suppressed
    ANALYZED = "analyzed"  # brain woke up; analysis + notification done
    ANALYSIS_ERROR = "analysis_error"  # no LLM; deterministic decision path still up


@dataclass(frozen=True)
class ScanResult:
    """Result of a single (watch item × timeframe) scan."""

    symbol: str
    status: ScanStatus
    detail: str = ""
    signals: tuple[TriggerSignal, ...] = ()
    analysis: AnalysisOutput | None = None
    proposal: Proposal | None = None  # L4 confluence proposal (next to the analysis)
    timeframe: Timeframe | None = None  # scanned timeframe (None = legacy call path)


@dataclass
class WatchLoop:
    """Watch loop orchestrator that scans watch items."""

    adapters: Mapping[AssetClass, MarketDataAdapter]
    analyst: TechnicalAnalyst
    notifier: TelegramNotifier
    bus: EventBus
    quality: QualityGate = field(default_factory=QualityGate)
    triggers: TriggerEngine = field(default_factory=TriggerEngine)
    cooldown: CooldownTracker = field(default_factory=lambda: CooldownTracker(900))
    timeframe: Timeframe = Timeframe.H1
    limit: int = 100
    min_severity: Severity = Severity.MEDIUM
    fetch_retries: int = 1  # fetch retries (resilience to transient errors)
    circuit_threshold: int = 5  # consecutive failures before the source trips (degraded)
    circuit_reset_s: float = 300.0  # breaker timeout (half-open probe)
    respect_sessions: bool = True
    clock: Callable[[], datetime] = utcnow
    # Optional smart dispatch (prioritization + quiet hours + digest); else direct notify.
    dispatcher: AlertDispatcher | None = None
    # L4 confluence aggregator (six agent opinions → Proposal). Default: standard agent set.
    aggregator: SignalAggregator = field(default_factory=default_aggregator)
    # Optional decision journal (memory). When present every analysis is recorded.
    journal: AnalysisJournal | None = None
    # Optional OHLCV PIT store. When present every series passing the quality gate is
    # persisted → the dashboard (/api/quotes, chart) and backtests read REAL data.
    ohlcv_store: OHLCVStore | None = None
    # Optional proposal journal (self-monitoring). When present each Proposal is stored, so
    # the evaluator measures the confluence series with the same metric as the analyses.
    proposal_store: ProposalStore | None = None
    # Optional read-time adjustment: the store stays RAW; triggers/indicators/analysis see
    # the ADJUSTED view returned by this function (corporate-action continuity).
    adjuster: Callable[[OHLCVSeries], OHLCVSeries] | None = None
    # Optional store reader: adds the symbol's OTHER timeframe series (HTF) from the store
    # to the analysis context. None → only the scanned timeframe's series is used.
    series_loader: Callable[[str, Timeframe], OHLCVSeries | None] | None = None
    # Optional macro source: fills the context with fresh EVDS trends (stale/empty → None →
    # MacroIntermarketAgent honestly reports NO_DATA).
    macro_loader: Callable[[], Mapping[str, float] | None] | None = None
    # Optional news source: fresh archived news + KAP for the symbol → ctx.news /
    # ctx.disclosures → NewsSentimentAgent gets real input.
    news_loader: Callable[[str], tuple[tuple[NewsItem, ...], tuple[Disclosure, ...]]] | None = None
    # Optional positioning source: fresh LEVEL values (e.g. crypto_fng 0–100).
    positioning_loader: Callable[[], Mapping[str, float] | None] | None = None
    health_store: HealthCheckpointStore | None = None
    # Circuit breaker per asset class (degraded mode) — created lazily.
    _breakers: dict[AssetClass, CircuitBreaker] = field(default_factory=dict, init=False)

    def _publish(self, event_type: EventType, **payload: object) -> None:
        try:
            self.bus.publish(Event(type=event_type, source="watch_loop", payload=payload))
            self._checkpoint("health.event_bus")
        except Exception as exc:  # an event bus failure does not fail the scan
            _log.warning("event-publish-error", event_type=event_type.value, error=repr(exc))

    def _checkpoint(self, key: str) -> None:
        if self.health_store is None:
            return
        try:
            self.health_store.set_checkpoint(key, self.clock().isoformat())
        except Exception as exc:
            _log.warning("health-checkpoint-error", key=key, error_type=type(exc).__name__)

    def _breaker_for(self, asset_class: AssetClass) -> CircuitBreaker:
        breaker = self._breakers.get(asset_class)
        if breaker is None:
            breaker = CircuitBreaker(
                failure_threshold=self.circuit_threshold,
                reset_timeout_s=self.circuit_reset_s,
                clock=self.clock,
            )
            self._breakers[asset_class] = breaker
        return breaker

    def timeframes_for(self, item: WatchlistItem) -> list[Timeframe]:
        """Timeframes to scan for the item; the loop default when the config has none."""
        if item.timeframes:
            return [Timeframe(t) for t in item.timeframes]
        return [self.timeframe]

    def scan(self, item: WatchlistItem, timeframe: Timeframe | None = None) -> ScanResult:
        """Scan one (item × timeframe); without ``timeframe`` the loop default is used."""
        tf = timeframe or self.timeframe
        now = self.clock()
        if self.respect_sessions and not is_market_open(item.asset_class, now):
            return ScanResult(item.symbol, ScanStatus.CLOSED, "market closed", timeframe=tf)

        adapter = self.adapters.get(item.asset_class)
        if adapter is None:
            return ScanResult(
                item.symbol,
                ScanStatus.SKIPPED,
                f"no {item.asset_class.value} adapter",
                timeframe=tf,
            )

        breaker = self._breaker_for(item.asset_class)
        try:
            series = breaker.call(
                lambda: retry(
                    lambda: adapter.fetch_ohlcv(item.symbol, tf, limit=self.limit),
                    attempts=self.fetch_retries,
                )
            )
        except CircuitOpenError:  # source tripped → flying blind; skip without hammering it
            return ScanResult(
                item.symbol,
                ScanStatus.DEGRADED,
                "source unavailable (flying blind)",
                timeframe=tf,
            )
        except Exception as exc:  # adapter/network error (incl. unsupported timeframe) → report it
            _log.warning("scan-data-error", symbol=item.symbol, timeframe=tf.value, error=repr(exc))
            if (
                breaker.state is CircuitState.OPEN
            ):  # this failure just opened the breaker → alert once
                self._notify_degraded(item.symbol)
            return ScanResult(item.symbol, ScanStatus.ERROR, str(exc), timeframe=tf)

        self._checkpoint("health.exchange_rest")

        report = self.quality.check_ohlcv(series, now=now)
        self._publish(
            EventType.PRICE_UPDATED,
            symbol=item.symbol,
            timeframe=tf.value,
            ok=report.ok,
        )
        if not report.ok:
            self._publish(EventType.DATA_QUARANTINED, symbol=item.symbol)
            return ScanResult(
                item.symbol, ScanStatus.DATA_PENDING, "insufficient quality", timeframe=tf
            )

        # Write quality-passing OHLCV to the PIT store (triggered or not) → fresh dashboard data.
        # FIRST the raw record (PIT), THEN the analysis view — the order matters.
        if self.ohlcv_store is not None:
            try:
                self.ohlcv_store.save_series(report.series)
            except Exception as exc:  # a storage error must not fail the scan (fail-safe)
                _log.warning("ohlcv-save-error", symbol=item.symbol, error=repr(exc))

        # Indicators/triggers see the ADJUSTED view for continuity (the store stays raw).
        # No-op without an adjuster/actions; an error does not fail the analysis (fail-safe).
        analysis_series = report.series
        if self.adjuster is not None:
            try:
                analysis_series = self.adjuster(report.series)
            except Exception as exc:
                _log.warning("read-time-adjust-error", symbol=item.symbol, error=repr(exc))

        signals = self.triggers.evaluate(analysis_series)
        if not signals:
            return ScanResult(item.symbol, ScanStatus.NO_TRIGGER, timeframe=tf)
        self._publish(
            EventType.TRIGGER_FIRED,
            symbol=item.symbol,
            timeframe=tf.value,
            kinds=[s.kind.value for s in signals],
        )

        # Cost governor gate: without enough severity the brain is not woken.
        if not should_wake_brain(signals, min_severity=self.min_severity):
            return ScanResult(
                item.symbol, ScanStatus.LOW_SEVERITY, signals=tuple(signals), timeframe=tf
            )

        # Cooldown/dedup: the same (symbol × timeframe) is not re-analyzed inside the window.
        # The timeframe is part of the key, so each timeframe has its own cooldown.
        cooldown_key = f"{item.symbol}|{tf.value}"
        if not self.cooldown.should_emit(cooldown_key, now=now):
            return ScanResult(
                item.symbol, ScanStatus.COOLDOWN, signals=tuple(signals), timeframe=tf
            )
        # L4 confluence: merge the six agent opinions (deterministic, $0; next to the LLM
        # analysis). Agents without a news/macro source report NO_DATA.
        news_items, disclosures = self._news_context(item.symbol)
        ctx = AnalysisContext(
            symbol=item.symbol,
            timeframe=tf,
            series=analysis_series,
            signals=tuple(signals),
            disclosures=disclosures,
            news=news_items,
            asset_class=item.asset_class,
            series_by_tf=self._htf_context(item, tf, analysis_series),
            macro=self._macro_context(item.symbol),
            positioning=self._positioning_context(item.symbol),
        )
        proposal = self.aggregator.aggregate(ctx)
        _log.info(
            "confluence",
            symbol=item.symbol,
            direction=proposal.direction.value,
            strength=proposal.strength,
            confluence=proposal.confluence_score,
        )
        proposal_id: int | None = None
        if self.proposal_store is not None and proposal.data_status is DataStatus.OK:
            # Proposal journal (self-monitoring) — fail-safe: a write error does not fail the scan.
            try:
                proposal_id = self.proposal_store.save_proposal(proposal, source="watch")
            except Exception as exc:
                _log.warning("proposal-journal-error", symbol=item.symbol, error=repr(exc))

        # The watch scan uses the cheap ROUTINE role and leaves web search off by default.
        # An LLM failure does not stop the deterministic confluence/alert path; no cooldown
        # is recorded so a transient provider error can be retried on the next scan.
        try:
            analysis = self.analyst.analyze(
                analysis_series,
                list(signals),
                tier=TaskTier.ROUTINE,
                use_web_search=False,
            )
        except Exception as exc:
            _log.warning("scan-llm-error", symbol=item.symbol, timeframe=tf.value, error=repr(exc))
            text = "⚠️ LLM analysis unavailable; deterministic confluence kept running.\n\n"
            text += format_proposal(proposal)
            markup = (
                feedback_keyboard(
                    "proposal",
                    proposal_id,
                    symbol=item.symbol,
                    severity=alert_severity(signals).value,
                )
                if proposal_id is not None
                else None
            )
            if self.dispatcher is not None:
                self.dispatcher.dispatch_message(
                    text, alert_severity(signals), now=now, reply_markup=markup
                )
            else:
                self.notifier.broadcast(text, reply_markup=markup)
            return ScanResult(
                item.symbol,
                ScanStatus.ANALYSIS_ERROR,
                f"LLM analysis failed: {type(exc).__name__}",
                signals=tuple(signals),
                proposal=proposal,
                timeframe=tf,
            )

        self._checkpoint("health.llm")

        analysis_id: int | None = None
        if self.journal is not None:  # decision journal (memory)
            try:
                analysis_id = self.journal.record_analysis(analysis, signals=signals)
            except Exception as exc:
                _log.warning("analysis-journal-error", symbol=item.symbol, error=repr(exc))

        # The cooldown is recorded only after the analysis + deterministic decision succeed.
        self.cooldown.record(cooldown_key, now=now)
        markup = (
            feedback_keyboard(
                "analysis",
                analysis_id,
                symbol=item.symbol,
                severity=alert_severity(signals).value,
            )
            if analysis_id is not None
            else None
        )
        full_text = format_analysis_with_proposal(analysis, proposal)
        if self.dispatcher is not None:
            # Smart dispatch: severity + quiet-hours policy (instant/digest/suppress).
            self.dispatcher.dispatch_analysis(
                analysis,
                alert_severity(signals),
                now=now,
                text=full_text,
                reply_markup=markup,
            )
        else:
            # Direct notification: send the LLM analysis + confluence summary together.
            # When the analysis is journaled, 👍/👎 inline buttons are attached (no nagging).
            self.notifier.broadcast(full_text, reply_markup=markup)
        self._publish(
            EventType.ALERT_CREATED, symbol=item.symbol, confidence=analysis.confidence_pct
        )
        return ScanResult(
            item.symbol,
            ScanStatus.ANALYZED,
            signals=tuple(signals),
            analysis=analysis,
            proposal=proposal,
            timeframe=tf,
        )

    def _htf_context(
        self, item: WatchlistItem, primary_tf: Timeframe, primary_series: OHLCVSeries
    ) -> dict[Timeframe, OHLCVSeries]:
        """Collect the symbol's other timeframe series from the store for the context.

        Only the item's configured timeframes are read; a timeframe without data is left
        out (nothing is fabricated — agents only see existing HTF data). A read error does
        not fail the analysis. Read-time adjustment is applied to HTF series as well.
        """
        result: dict[Timeframe, OHLCVSeries] = {primary_tf: primary_series}
        if self.series_loader is None:
            return result
        for tf in self.timeframes_for(item):
            if tf is primary_tf:
                continue
            try:
                series = self.series_loader(item.symbol, tf)
            except Exception as exc:  # a store error drops the HTF context, not the analysis
                _log.warning(
                    "htf-context-error", symbol=item.symbol, timeframe=tf.value, error=repr(exc)
                )
                continue
            if series is None or not series.bars:
                continue
            if self.adjuster is not None:
                # Adjustment error → continue with the raw HTF view (the analysis survives).
                with contextlib.suppress(Exception):
                    series = self.adjuster(series)
            result[tf] = series
        return result

    def _macro_context(self, symbol: str) -> Mapping[str, float] | None:
        """Return fresh macro trends; None when the source is missing or fails (NO_DATA)."""
        if self.macro_loader is None:
            return None
        try:
            return self.macro_loader()
        except Exception as exc:  # a macro error does not fail the analysis (fail-safe)
            _log.warning("macro-context-error", symbol=symbol, error=repr(exc))
            return None

    def _news_context(self, symbol: str) -> tuple[tuple[NewsItem, ...], tuple[Disclosure, ...]]:
        """Fresh archived news + KAP for the symbol; error → empty (agent NO_DATA)."""
        if self.news_loader is None:
            return (), ()
        try:
            return self.news_loader(symbol)
        except Exception as exc:
            _log.warning("news-context-error", symbol=symbol, error=repr(exc))
            return (), ()

    def _positioning_context(self, symbol: str) -> Mapping[str, float] | None:
        """Fresh positioning levels (Fear & Greed); None when the source is missing or fails."""
        if self.positioning_loader is None:
            return None
        try:
            return self.positioning_loader()
        except Exception as exc:
            _log.warning("positioning-context-error", symbol=symbol, error=repr(exc))
            return None

    def _notify_degraded(self, symbol: str) -> None:
        """Send a single 'flying blind' alert when a source becomes unavailable."""
        message = (
            f"⚠️ {symbol}: data source unreachable — temporarily disabled (flying blind). "
            f"Analysis is paused for this asset."
        )
        self.notifier.broadcast(message)
        self._publish(EventType.ALERT_CREATED, symbol=symbol, kind="degraded")

    def tick(self, items: list[WatchlistItem]) -> list[ScanResult]:
        """Scan a group of watch items (one tier's watch step).

        Symbol × timeframe loop — each item is scanned on all of its configured timeframes
        (the loop default when empty; single-timeframe configs behave as before).
        """
        self._checkpoint("health.scheduler")
        results: list[ScanResult] = []
        for item in items:
            for tf in self.timeframes_for(item):
                try:
                    results.append(self.scan(item, tf))
                except Exception as exc:
                    # Last line of defence: even if one symbol/timeframe fails unexpectedly, the
                    # rest of the group is scanned. Only the error type is reported, no secrets.
                    _log.exception(
                        "scan-isolation-error",
                        symbol=item.symbol,
                        timeframe=tf.value,
                        error_type=type(exc).__name__,
                    )
                    results.append(
                        ScanResult(
                            item.symbol,
                            ScanStatus.ERROR,
                            f"unexpected scan error: {type(exc).__name__}",
                            timeframe=tf,
                        )
                    )
        return results
