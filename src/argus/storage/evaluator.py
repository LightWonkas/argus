"""OutcomeEvaluator v2 — scores pending analyses + proposals against baselines.

The **self-monitoring** feedback of the watch loop. For every pending record (analysis or
confluence proposal) the PIT OHLCV of the same symbol is read and, **once the horizon has
closed**, the following are written:

- **system** verdict: two metrics, ``touch`` + ``close`` (k×ATR threshold).
- **Baselines** (only when the system is directional — a fair race on the same opportunity
  set): ``random`` (id-seeded, deterministic) · ``bullish`` · ``momentum``; each again with
  touch+close.

Verdict classes:

- ``TOO_EARLY`` → nothing is written; it is checked again on the next round.
- Final ``SKIP`` (no direction / ATR not warmed up / close inside the threshold) → a
  ``verdict='skip'`` row is written: it does not enter the hit-rate total but **does not
  clog the pending queue** (the verdict can never change for the same input, so
  re-evaluating is waste — this is where the v1 queue clog is closed).
- ``CORRECT``/``INCORRECT`` → persistent record.

Collects NO new data — it only reads the OHLCV already in the store. If evaluating one
record blows up, the others continue (fail-safe).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from argus.core.contracts import AnalysisOutput, OHLCVSeries
from argus.core.enums import Timeframe
from argus.core.schemas import utcnow
from argus.observability.logging import get_logger
from argus.storage.base import StoredAnalysis, StoredProposal
from argus.storage.outcome import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_HORIZON_BARS,
    DEFAULT_K_ATR,
    Metric,
    OutcomeResult,
    OutcomeVerdict,
    Variant,
    bias_sign,
    evaluate_direction,
    momentum_sign,
    random_sign,
)

_log = get_logger("argus.storage.evaluator")

# Baseline variants (everything except system).
_BASELINES: tuple[Variant, ...] = (Variant.RANDOM, Variant.BULLISH, Variant.MOMENTUM)


class OutcomeJournal(Protocol):
    """The store surface the evaluator needs (a fake can be passed in tests)."""

    def pending_outcomes(self, *, limit: int = 200) -> list[StoredAnalysis]: ...
    def pending_proposal_outcomes(self, *, limit: int = 200) -> list[StoredProposal]: ...
    def load_series(
        self, symbol: str, timeframe: Timeframe, *, limit: int = 100
    ) -> OHLCVSeries: ...
    def record_outcome(
        self,
        analysis_id: int,
        *,
        correct: bool,
        note: str = "",
        metric: str = "close",
        variant: str = "system",
        ref_type: str = "analysis",
        verdict: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class EvaluationSummary:
    """Summary of one evaluation round (over system verdicts)."""

    evaluated: int  # final system verdicts recorded (correct+incorrect, both metrics summed)
    correct: int
    incorrect: int
    too_early: int  # horizon not reached yet (retry later)
    skipped: int  # final skip (no direction / no ATR / inside threshold) → recorded, left the queue
    baseline_rows: int = 0  # number of baseline rows written
    proposals_evaluated: int = 0  # number of proposal-metric pairs with a final verdict


@dataclass(frozen=True)
class _PendingItem:
    """Shared evaluation view of an analysis and a proposal."""

    ref_type: str  # "analysis" | "proposal"
    ref_id: int
    symbol: str
    timeframe: Timeframe
    created_at: datetime
    direction: str | None  # bias / direction text


@dataclass
class OutcomeEvaluator:
    """Evaluates pending analyses + proposals; writes the verdicts to the journal."""

    journal: OutcomeJournal
    horizon_bars: int = DEFAULT_HORIZON_BARS
    k_atr: float = DEFAULT_K_ATR
    atr_period: int = DEFAULT_ATR_PERIOD
    series_limit: int = 200  # candles fetched per symbol for evaluation
    clock: datetime | None = None
    # The store is RAW (PIT); evaluation wants the ADJUSTED view for economic continuity
    # (on a split day the raw price gap produces a fake hit/miss). None → raw.
    adjuster: Callable[[OHLCVSeries], OHLCVSeries] | None = None

    def run(self, *, limit: int = 200) -> EvaluationSummary:
        """Runs one evaluation round and returns the summary."""
        now = self.clock or utcnow()
        items = [self._from_analysis(s) for s in self.journal.pending_outcomes(limit=limit)]
        try:
            proposals = self.journal.pending_proposal_outcomes(limit=limit)
        except AttributeError:  # legacy fake/journal — no proposal series (backward compat)
            proposals = []
        items += [self._from_proposal(p) for p in proposals]

        evaluated = correct = incorrect = too_early = skipped = 0
        baseline_rows = proposals_evaluated = 0

        for item in items:
            try:
                results = self._evaluate_item(item, now=now)
            except Exception as exc:  # if one record blows up the others continue (fail-safe)
                _log.warning(
                    "outcome-evaluation-error",
                    ref_type=item.ref_type,
                    ref_id=item.ref_id,
                    error=repr(exc),
                )
                continue
            if results is None:
                too_early += 1
                continue

            system_results, baseline_results = results
            # Final verdicts are written in a single pass (TOO_EARLY never reaches here).
            for metric, result in system_results.items():
                is_skip = result.verdict is OutcomeVerdict.SKIP
                self.journal.record_outcome(
                    item.ref_id,
                    correct=result.verdict is OutcomeVerdict.CORRECT,
                    note=result.note,
                    metric=metric.value,
                    variant=Variant.SYSTEM.value,
                    ref_type=item.ref_type,
                    verdict=result.verdict.value,
                )
                if is_skip:
                    skipped += 1
                    continue
                evaluated += 1
                if item.ref_type == "proposal":
                    proposals_evaluated += 1
                if result.verdict is OutcomeVerdict.CORRECT:
                    correct += 1
                else:
                    incorrect += 1
            for (metric, variant), result in baseline_results.items():
                self.journal.record_outcome(
                    item.ref_id,
                    correct=result.verdict is OutcomeVerdict.CORRECT,
                    note=result.note,
                    metric=metric.value,
                    variant=variant.value,
                    ref_type=item.ref_type,
                    verdict=result.verdict.value,
                )
                baseline_rows += 1

        _log.info(
            "outcome-round",
            evaluated=evaluated,
            correct=correct,
            incorrect=incorrect,
            too_early=too_early,
            skipped=skipped,
            baseline_rows=baseline_rows,
            proposals=proposals_evaluated,
        )
        return EvaluationSummary(
            evaluated,
            correct,
            incorrect,
            too_early,
            skipped,
            baseline_rows=baseline_rows,
            proposals_evaluated=proposals_evaluated,
        )

    # ── internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _from_analysis(stored: StoredAnalysis) -> _PendingItem:
        analysis: AnalysisOutput = stored.analysis
        return _PendingItem(
            ref_type="analysis",
            ref_id=stored.id,
            symbol=analysis.symbol,
            timeframe=analysis.timeframe,
            created_at=stored.created_at,
            direction=analysis.bias,
        )

    @staticmethod
    def _from_proposal(stored: StoredProposal) -> _PendingItem:
        return _PendingItem(
            ref_type="proposal",
            ref_id=stored.id,
            symbol=stored.symbol,
            timeframe=Timeframe(stored.timeframe),
            created_at=stored.created_at,
            direction=stored.direction,
        )

    def _evaluate_item(
        self, item: _PendingItem, *, now: datetime
    ) -> tuple[dict[Metric, OutcomeResult], dict[tuple[Metric, Variant], OutcomeResult]] | None:
        """Produces the system + baseline verdicts of one record.

        ``None`` → TOO_EARLY (nothing is written). Otherwise every verdict is **final**
        (correct/incorrect/skip) — written in one go, and the record leaves the queue.
        """
        series = self.journal.load_series(item.symbol, item.timeframe, limit=self.series_limit)
        if self.adjuster is not None:
            series = self.adjuster(series)  # read-time adjustment (the store stays raw)
        horizon = timedelta(seconds=item.timeframe.seconds * self.horizon_bars)
        sign = bias_sign(item.direction)

        system: dict[Metric, OutcomeResult] = {}
        for metric in (Metric.TOUCH, Metric.CLOSE):
            result = self._direction_result(
                sign, item, series, now=now, horizon=horizon, metric=metric
            )
            if result.verdict is OutcomeVerdict.TOO_EARLY:
                return None  # horizon/data not complete → whole record goes to the next round
            system[metric] = result

        baselines: dict[tuple[Metric, Variant], OutcomeResult] = {}
        if sign != 0 and any(
            r.verdict in (OutcomeVerdict.CORRECT, OutcomeVerdict.INCORRECT) for r in system.values()
        ):
            # Fair race: baselines only run on opportunities the system could be scored on.
            for variant in _BASELINES:
                b_sign = self._baseline_sign(variant, item, series)
                for metric in (Metric.TOUCH, Metric.CLOSE):
                    result = self._direction_result(
                        b_sign, item, series, now=now, horizon=horizon, metric=metric
                    )
                    if result.verdict is OutcomeVerdict.TOO_EARLY:
                        # A baseline cannot be TOO_EARLY when system is final (same series/horizon);
                        # defensive: skip the baseline, still write the system.
                        continue
                    baselines[(metric, variant)] = result
        return system, baselines

    def _direction_result(
        self,
        sign: int,
        item: _PendingItem,
        series: OHLCVSeries,
        *,
        now: datetime,
        horizon: timedelta,
        metric: Metric,
    ) -> OutcomeResult:
        return evaluate_direction(
            sign,
            item.created_at,
            series,
            now=now,
            horizon=horizon,
            metric=metric,
            k_atr=self.k_atr,
            atr_period=self.atr_period,
        )

    @staticmethod
    def _baseline_sign(variant: Variant, item: _PendingItem, series: OHLCVSeries) -> int:
        if variant is Variant.RANDOM:
            return random_sign(item.ref_id)
        if variant is Variant.BULLISH:
            return 1
        return momentum_sign(series, item.created_at)
