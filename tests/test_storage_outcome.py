"""Hit rate v2 tests — ATR threshold, touch/close dual metric, baselines, evaluator.

Known-outcome tests on synthetic series (no network): the touch/close distinction, an
honest SKIP while ATR is not warmed up, baseline determinism, and the evaluator writing
system + baseline rows in one pass and scoring proposals with the same metric.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.contracts import AnalysisOutput, OHLCVBar, OHLCVSeries, Provenance
from argus.core.enums import EvidenceTier, Timeframe
from argus.storage.base import StoredAnalysis, StoredProposal
from argus.storage.evaluator import OutcomeEvaluator
from argus.storage.outcome import (
    Metric,
    OutcomeVerdict,
    evaluate_direction,
    momentum_sign,
    pit_atr,
    random_sign,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)
# Short warm-up for tests: ATR period 3 (instead of the default 14) keeps series short.
ATR_P = 3


def _series(
    prices: list[float], *, start: datetime = BASE, tf: Timeframe = Timeframe.D1
) -> OHLCVSeries:
    """Daily bar series: each price is one bar's close; high/low = ±1 (ATR ≈ 2)."""
    step = timedelta(seconds=tf.seconds)
    bars = tuple(
        OHLCVBar(
            timestamp=start + step * i,
            open=Decimal(str(p)),
            high=Decimal(str(p + 1)),
            low=Decimal(str(p - 1)),
            close=Decimal(str(p)),
            volume=Decimal("1"),
        )
        for i, p in enumerate(prices)
    )
    prov = Provenance(source="test", timestamp=bars[-1].timestamp, confidence=0.9)
    return OHLCVSeries(symbol="X", timeframe=tf, bars=bars, provenance=prov)


def _analysis(bias: str | None) -> AnalysisOutput:
    return AnalysisOutput(
        symbol="X",
        timeframe=Timeframe.D1,
        summary="test",
        bias=bias,
        evidence_tier=EvidenceTier.A,
        confidence_pct=70,
        invalidation="x",
        model="m",
    )


# Standard setup: 10 warm-up bars (idx 0-9, all ~100) + 5 horizon bars (idx 10-14).
# created_at = bar idx 9; horizon 5 bars → target = bar idx 14. ATR(±1 bars) ≈ 2 →
# threshold = 2 (k=1).
CREATED = BASE + DAY * 9
NOW = BASE + DAY * 30


def _eval(prices: list[float], sign: int, metric: Metric, *, now: datetime = NOW) -> OutcomeVerdict:
    return evaluate_direction(
        sign,
        CREATED,
        _series(prices),
        now=now,
        horizon=DAY * 5,
        metric=metric,
        k_atr=1.0,
        atr_period=ATR_P,
    ).verdict


WARMUP = [100.0] * 10


# ── evaluate_direction: touch ────────────────────────────────────────────────


def test_touch_correct_when_high_reaches_threshold() -> None:
    # A bar inside the horizon touches 102 (high 103 ≥ 100+2) → CORRECT; the close reverts.
    prices = [*WARMUP, 101, 102, 101, 100, 100]
    assert _eval(prices, +1, Metric.TOUCH) is OutcomeVerdict.CORRECT
    # Same series, close metric stays inside the threshold (last close 100) → indecisive
    # SKIP (the dual-metric difference).
    assert _eval(prices, +1, Metric.CLOSE) is OutcomeVerdict.SKIP


def test_touch_incorrect_when_never_reached() -> None:
    # Flat series: the threshold was never touched and the horizon closed → INCORRECT.
    assert _eval([100.0] * 15, +1, Metric.TOUCH) is OutcomeVerdict.INCORRECT


def test_touch_bearish_uses_low() -> None:
    # Bearish direction: low 97 ≤ 100-2 → CORRECT (the low is measured, not the high).
    prices = [*WARMUP, 99, 98, 99, 100, 100]
    assert _eval(prices, -1, Metric.TOUCH) is OutcomeVerdict.CORRECT


# ── evaluate_direction: close ────────────────────────────────────────────────


def test_close_correct_when_settles_beyond_threshold() -> None:
    prices = [*WARMUP, 101, 102, 103, 104, 103]  # horizon close 103 ≥ 102
    assert _eval(prices, +1, Metric.CLOSE) is OutcomeVerdict.CORRECT


def test_close_incorrect_when_settles_against() -> None:
    prices = [*WARMUP, 99, 98, 97, 96, 97]  # close 97 ≤ 98 (opposite threshold)
    assert _eval(prices, +1, Metric.CLOSE) is OutcomeVerdict.INCORRECT
    # The same move is a hit for a bearish claim (direction symmetry).
    assert _eval(prices, -1, Metric.CLOSE) is OutcomeVerdict.CORRECT


# ── honesty rules ────────────────────────────────────────────────────────────


def test_no_direction_is_skip() -> None:
    assert _eval([100.0] * 15, 0, Metric.CLOSE) is OutcomeVerdict.SKIP


def test_atr_not_warmed_is_skip_not_fabricated() -> None:
    # Only 2 bars up to created_at (< atr_period=3) → no threshold can be built → SKIP.
    series = _series([100.0] * 8, start=CREATED - DAY)  # first bar at CREATED-1d → 2 bars ≤ CREATED
    r = evaluate_direction(
        +1, CREATED, series, now=NOW, horizon=DAY * 5, metric=Metric.CLOSE, atr_period=ATR_P
    )
    assert r.verdict is OutcomeVerdict.SKIP
    assert "ATR" in r.note


def test_too_early_when_horizon_open() -> None:
    prices = [*WARMUP, 101, 102]
    assert _eval(prices, +1, Metric.TOUCH, now=CREATED + DAY * 2) is OutcomeVerdict.TOO_EARLY


def test_too_early_when_no_bar_after_target() -> None:
    # The horizon has elapsed but no bar reaches the target (missing data) → not even a
    # touch is decided (the window cannot be confirmed closed).
    prices = [*WARMUP, 101, 102]  # last bar idx 11 < target idx 14
    assert _eval(prices, +1, Metric.TOUCH) is OutcomeVerdict.TOO_EARLY


# ── baseline generators ──────────────────────────────────────────────────────


def test_random_sign_is_deterministic_and_two_sided() -> None:
    assert random_sign(42) == random_sign(42)  # same id → same direction (on reruns too)
    signs = {random_sign(i) for i in range(50)}
    assert signs == {1, -1}  # both directions occur (unbiased)


def test_momentum_sign_follows_previous_candle() -> None:
    up = OHLCVBar(
        timestamp=BASE,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("102"),
        volume=Decimal("1"),
    )
    prov = Provenance(source="t", timestamp=BASE, confidence=0.9)
    series = OHLCVSeries(symbol="X", timeframe=Timeframe.D1, bars=(up,), provenance=prov)
    assert momentum_sign(series, BASE) == 1
    down = up.model_copy(update={"close": Decimal("98"), "low": Decimal("97")})
    series2 = OHLCVSeries(symbol="X", timeframe=Timeframe.D1, bars=(down,), provenance=prov)
    assert momentum_sign(series2, BASE) == -1


def test_momentum_sign_flat_is_zero() -> None:
    # Flat body + equal previous close → no direction can be made up → 0 (SKIP input).
    series = _series([100.0, 100.0])
    assert momentum_sign(series, BASE + DAY) == 0


def test_pit_atr_ignores_future_bars() -> None:
    # Huge bars after created_at must not affect ATR (no look-ahead).
    calm = [100.0] * 10
    wild = [*calm, 200.0, 50.0, 300.0]
    atr_calm = pit_atr(_series(calm), CREATED, period=ATR_P)
    atr_wild = pit_atr(_series(wild), CREATED, period=ATR_P)
    assert atr_calm == atr_wild


# ── OutcomeEvaluator (one pass with a fake journal) ──────────────────────────


class FakeJournal:
    """Fake store providing pending + load_series + record_outcome (v2 signature)."""

    def __init__(
        self,
        pending: list[StoredAnalysis],
        series: OHLCVSeries,
        proposals: list[StoredProposal] | None = None,
    ) -> None:
        self._pending = pending
        self._series = series
        self._proposals = proposals or []
        self.recorded: list[dict[str, object]] = []

    def pending_outcomes(self, *, limit: int = 200) -> list[StoredAnalysis]:
        return self._pending

    def pending_proposal_outcomes(self, *, limit: int = 200) -> list[StoredProposal]:
        return self._proposals

    def load_series(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        return self._series

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
    ) -> None:
        self.recorded.append(
            {
                "id": analysis_id,
                "correct": correct,
                "metric": metric,
                "variant": variant,
                "ref_type": ref_type,
                "verdict": verdict,
            }
        )


def _evaluator(journal: FakeJournal) -> OutcomeEvaluator:
    return OutcomeEvaluator(journal, clock=NOW, horizon_bars=5, k_atr=1.0, atr_period=ATR_P)


def test_evaluator_writes_system_and_baselines() -> None:
    # Clear uptrend: system touch+close CORRECT; 3 baselines × 2 metrics rows are written too.
    series = _series([*WARMUP, 102, 103, 104, 105, 106])
    pending = [StoredAnalysis(id=1, created_at=CREATED, analysis=_analysis("bullish"))]
    journal = FakeJournal(pending, series)
    summary = _evaluator(journal).run()

    system_rows = [r for r in journal.recorded if r["variant"] == "system"]
    baseline_rows = [r for r in journal.recorded if r["variant"] != "system"]
    assert {r["metric"] for r in system_rows} == {"touch", "close"}
    assert all(r["verdict"] == "correct" for r in system_rows)
    assert {str(r["variant"]) for r in baseline_rows} == {"random", "bullish", "momentum"}
    assert len(baseline_rows) == 6  # 3 baselines × 2 metrics
    assert summary.evaluated == 2 and summary.correct == 2
    assert summary.baseline_rows == 6


def test_evaluator_neutral_records_skip_and_no_baselines() -> None:
    # Directionless analysis: 2 system skip rows are written (it leaves the queue) and no
    # baseline comparison is started.
    series = _series([*WARMUP, 102, 103, 104, 105, 106])
    pending = [StoredAnalysis(id=7, created_at=CREATED, analysis=_analysis("neutral"))]
    journal = FakeJournal(pending, series)
    summary = _evaluator(journal).run()

    assert len(journal.recorded) == 2
    assert all(r["verdict"] == "skip" and r["variant"] == "system" for r in journal.recorded)
    assert summary.skipped == 2 and summary.evaluated == 0 and summary.baseline_rows == 0


def test_evaluator_too_early_records_nothing() -> None:
    series = _series([*WARMUP, 102])
    pending = [StoredAnalysis(id=1, created_at=CREATED, analysis=_analysis("bullish"))]
    journal = FakeJournal(pending, series)
    summary = _evaluator(journal).run()
    assert journal.recorded == []
    assert summary.too_early == 1


def test_evaluator_scores_proposals_same_metric() -> None:
    # A confluence proposal is scored with the same metric as an analysis
    # (ref_type='proposal').
    series = _series([*WARMUP, 102, 103, 104, 105, 106])
    proposal = StoredProposal(
        id=3,
        symbol="X",
        timeframe="1d",
        created_at=CREATED,
        direction="bullish",
        strength=70,
        confluence_pct=80,
        source="watch",
    )
    journal = FakeJournal([], series, proposals=[proposal])
    summary = _evaluator(journal).run()

    prop_rows = [r for r in journal.recorded if r["ref_type"] == "proposal"]
    assert len(prop_rows) == 8  # 2 system + 6 baseline
    assert summary.proposals_evaluated == 2  # touch + close decisive verdicts


def test_evaluator_one_failure_does_not_break_others() -> None:
    # A record whose load_series raises does not take the others down (fail-safe).
    series = _series([*WARMUP, 102, 103, 104, 105, 106])

    class FlakyJournal(FakeJournal):
        def load_series(
            self, symbol: str, timeframe: Timeframe, *, limit: int = 100
        ) -> OHLCVSeries:
            if symbol == "BOOM":
                raise RuntimeError("data error")
            return self._series

    boom = _analysis("bullish").model_copy(update={"symbol": "BOOM"})
    pending = [
        StoredAnalysis(id=1, created_at=CREATED, analysis=boom),  # raises
        StoredAnalysis(id=2, created_at=CREATED, analysis=_analysis("bullish")),  # CORRECT
    ]
    journal = FlakyJournal(pending, series)
    summary = _evaluator(journal).run()
    assert summary.evaluated == 2  # only the touch+close verdicts of id=2
    assert all(r["id"] == 2 for r in journal.recorded)
