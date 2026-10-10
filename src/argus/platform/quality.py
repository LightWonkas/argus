"""Data Quality Gate — L2.

Inspects an OHLCV series before releasing it downstream: duplicate/out-of-order bars,
time gaps, excessive jumps (outliers) and staleness. Quarantines bad records and returns
the cleaned series together with a report.

Honesty: a stale/empty series is flagged ``ok=False``; downstream then reports
"DATA PENDING" / "flying blind on X" accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.crypto import CryptoAnalysisContext, LongShortRatio
from argus.core.schemas import DataStatus, utcnow

IssueKind = Literal[
    "empty",
    "stale",
    "duplicate",
    "gap",
    "disorder",
    "outlier",
    "future",
    "insufficient_history",
]


class QualityDecision(StrEnum):
    """Explicit Data Quality Gate outcome that drives downstream behaviour."""

    ACCEPT = "accept"
    ACCEPT_WITH_WARNING = "accept_with_warning"
    QUARANTINE = "quarantine"
    REJECT = "reject"

    @property
    def allows_downstream(self) -> bool:
        return self in (QualityDecision.ACCEPT, QualityDecision.ACCEPT_WITH_WARNING)


_DECISION_RANK: dict[QualityDecision, int] = {
    QualityDecision.ACCEPT: 0,
    QualityDecision.ACCEPT_WITH_WARNING: 1,
    QualityDecision.QUARANTINE: 2,
    QualityDecision.REJECT: 3,
}

# For daily-or-longer bars (traditional exchanges) the weekend is added to the tolerance:
# Friday's close is the MOST RECENT data until Monday's open — the exchange is closed on
# Sat/Sun. Without this a daily bar would wrongly count as "stale" on Monday morning
# (3 calendar days but 1 trading day).
_DAY_SECONDS = 86_400


def _weekend_days_between(start: datetime, end: datetime) -> int:
    """Number of Sat/Sun days between ``start`` (exclusive) and ``end`` (inclusive).

    The calendar-day difference is small (typically a few days), so days are counted one by
    one; over a large span the series is already flagged as gap/stale, so no
    micro-optimisation is needed.
    """
    if end <= start:
        return 0
    count = 0
    day = (start + timedelta(days=1)).date()
    last = end.date()
    while day <= last:
        if day.weekday() >= 5:  # 5=Sat, 6=Sun
            count += 1
        day += timedelta(days=1)
    return count


@dataclass(frozen=True)
class QualityIssue:
    """A single quality finding."""

    kind: IssueKind
    detail: str
    index: int | None = None


@dataclass(frozen=True)
class QualityReport:
    """Data Quality Gate result."""

    decision: QualityDecision
    series: OHLCVSeries  # cleaned (deduplicated + sorted) series
    issues: tuple[QualityIssue, ...] = ()
    quarantined: tuple[OHLCVBar, ...] = ()

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    @property
    def ok(self) -> bool:
        """Backward-compatible downstream gate: only accept decisions pass."""
        return self.decision.allows_downstream

    def issues_of(self, kind: IssueKind) -> list[QualityIssue]:
        return [i for i in self.issues if i.kind == kind]


@dataclass(frozen=True)
class QualityGate:
    """Configurable quality check for an OHLCV series.

    Thresholds scale with the timeframe (regime/TF awareness).
    """

    stale_factor: float = 3.0  # stale when the newest bar is older than stale_factor × TF
    stale_after_s: float | None = None  # explicit override (None → derive from TF)
    max_jump_pct: float = 25.0  # % jump threshold between consecutive closes (outlier)
    gap_factor: float = 1.5  # gap when the consecutive delta > gap_factor × TF
    min_bars: int = 1  # config can raise this for indicator warm-up
    max_clock_skew_s: float = 30.0  # how far exchange time may run ahead of the local clock
    empty_action: QualityDecision = QualityDecision.REJECT
    stale_action: QualityDecision = QualityDecision.QUARANTINE
    duplicate_action: QualityDecision = QualityDecision.ACCEPT_WITH_WARNING
    gap_action: QualityDecision = QualityDecision.QUARANTINE
    disorder_action: QualityDecision = QualityDecision.ACCEPT_WITH_WARNING
    outlier_action: QualityDecision = QualityDecision.QUARANTINE
    future_action: QualityDecision = QualityDecision.QUARANTINE
    insufficient_history_action: QualityDecision = QualityDecision.QUARANTINE

    def _decision_for(self, issues: list[QualityIssue]) -> QualityDecision:
        if not issues:
            return QualityDecision.ACCEPT
        actions = {
            "empty": self.empty_action,
            "stale": self.stale_action,
            "duplicate": self.duplicate_action,
            "gap": self.gap_action,
            "disorder": self.disorder_action,
            "outlier": self.outlier_action,
            "future": self.future_action,
            "insufficient_history": self.insufficient_history_action,
        }
        return max((actions[issue.kind] for issue in issues), key=_DECISION_RANK.__getitem__)

    def check_ohlcv(self, series: OHLCVSeries, *, now: datetime | None = None) -> QualityReport:
        reference = now or utcnow()
        issues: list[QualityIssue] = []

        if not series.bars:
            empty = QualityIssue(kind="empty", detail="series is empty (no bars)")
            return QualityReport(decision=self.empty_action, series=series, issues=(empty,))

        # ── dedup (same timestamp → keep the first) ────────────────────────────
        seen: dict[datetime, OHLCVBar] = {}
        quarantined: list[OHLCVBar] = []
        for i, bar in enumerate(series.bars):
            if bar.timestamp in seen:
                quarantined.append(bar)
                issues.append(
                    QualityIssue("duplicate", f"duplicate timestamp {bar.timestamp.isoformat()}", i)
                )
            else:
                seen[bar.timestamp] = bar

        kept = list(seen.values())
        ordered = sorted(kept, key=lambda b: b.timestamp)
        if [b.timestamp for b in kept] != [b.timestamp for b in ordered]:
            issues.append(QualityIssue("disorder", "bars were not in time order; sorted"))

        if len(ordered) < self.min_bars:
            issues.append(
                QualityIssue(
                    "insufficient_history",
                    f"warm-up needs {self.min_bars} bars; {len(ordered)} available",
                )
            )

        expected = series.timeframe.seconds

        # ── gap (missing bars) ─────────────────────────────────────────────────
        for i in range(1, len(ordered)):
            prev_ts, cur_ts = ordered[i - 1].timestamp, ordered[i].timestamp
            delta = (cur_ts - prev_ts).total_seconds()
            if delta > expected * self.gap_factor:
                missing = max(round(delta / expected) - 1, 1)
                span = f"{prev_ts.isoformat()} → {cur_ts.isoformat()}"
                issues.append(QualityIssue("gap", f"{missing} bars missing ({span})", i))

        # ── outlier (jump between consecutive closes) ──────────────────────────
        for i in range(1, len(ordered)):
            prev_close = ordered[i - 1].close
            if prev_close > 0:
                jump = abs(float((ordered[i].close - prev_close) / prev_close) * 100.0)
                if jump > self.max_jump_pct:
                    issues.append(QualityIssue("outlier", f"{jump:.1f}% abrupt close change", i))
                    quarantined.append(ordered[i])

        # ── staleness ──────────────────────────────────────────────────────────
        stale_after = (
            self.stale_after_s if self.stale_after_s is not None else expected * self.stale_factor
        )
        expired_after = stale_after * 2
        latest = ordered[-1]
        age = (reference - latest.timestamp).total_seconds()
        if age < -self.max_clock_skew_s:
            issues.append(
                QualityIssue(
                    "future",
                    f"newest bar is {-age:.0f}s ahead of the local clock "
                    f"(allowed {self.max_clock_skew_s:.0f}s)",
                )
            )
        # For daily-or-longer bars, discount the intervening weekend: the source
        # (BIST/metals) produces no weekend bars → Friday's close stays current until Monday's
        # open. Crypto produces bars 24/7, so its last bar already falls on the weekend and
        # weekend_allowance ≈ 0 (the distinction emerges on its own; no asset class needed).
        weekend_allowance = 0.0
        if expected >= _DAY_SECONDS:
            weekend_allowance = _weekend_days_between(latest.timestamp, reference) * _DAY_SECONDS
        effective_stale_after = stale_after + weekend_allowance
        is_stale = age > effective_stale_after
        if is_stale:
            issues.append(
                QualityIssue(
                    "stale",
                    f"newest bar is {age:.0f}s old (threshold {effective_stale_after:.0f}s)",
                )
            )

        # ── cleaned series + freshness label ───────────────────────────────────
        # The freshness label uses the weekend allowance too (consistent with the gate):
        # otherwise the gate would say "ok" while provenance says "stale" — a contradiction.
        provenance = series.provenance.model_copy(update={"timestamp": latest.timestamp})
        provenance = provenance.with_computed_freshness(
            stale_after_s=effective_stale_after,
            expired_after_s=expired_after + weekend_allowance,
            now=reference,
        )
        cleaned = series.model_copy(update={"bars": tuple(ordered), "provenance": provenance})

        return QualityReport(
            decision=self._decision_for(issues),
            series=cleaned,
            issues=tuple(issues),
            quarantined=tuple(quarantined),
        )


@dataclass(frozen=True)
class CryptoQualityIssue:
    """Structured finding in the crypto context that affects downstream."""

    kind: str
    detail: str
    action: QualityDecision


@dataclass(frozen=True)
class CryptoQualityReport:
    decision: QualityDecision
    issues: tuple[CryptoQualityIssue, ...] = ()

    @property
    def allows_downstream(self) -> bool:
        return self.decision.allows_downstream


@dataclass(frozen=True)
class CryptoQualityGate:
    """Checks the spot/perpetual/order-book context as a real downstream gate."""

    max_age_s: float = 30.0
    long_short_ratio_max_age_s: float = 660.0
    max_clock_skew_s: float = 30.0
    max_spread_bps: float = 20.0
    max_spot_perpetual_deviation_bps: float = 100.0
    require_spot: bool = True
    missing_required_action: QualityDecision = QualityDecision.REJECT
    stale_action: QualityDecision = QualityDecision.QUARANTINE
    book_action: QualityDecision = QualityDecision.QUARANTINE
    divergence_action: QualityDecision = QualityDecision.QUARANTINE
    optional_missing_action: QualityDecision = QualityDecision.ACCEPT_WITH_WARNING

    def check(
        self,
        context: CryptoAnalysisContext,
        *,
        expected_book_sequence: int | None = None,
        now: datetime | None = None,
    ) -> CryptoQualityReport:
        reference = now or utcnow()
        issues: list[CryptoQualityIssue] = []

        def add(kind: str, detail: str, action: QualityDecision) -> None:
            issues.append(CryptoQualityIssue(kind, detail, action))

        if context.data_status is DataStatus.REJECTED:
            add("data_status", "context data status is rejected", QualityDecision.REJECT)
        elif context.data_status is DataStatus.QUARANTINED:
            add("data_status", "context data status is quarantined", QualityDecision.QUARANTINE)

        if self.require_spot and context.spot is None:
            add("missing_spot", "required spot ticker is missing", self.missing_required_action)

        records = [
            record
            for record in (
                context.spot,
                context.futures,
                context.order_book,
                context.funding,
                context.open_interest,
                context.basis,
                context.long_short_ratio,
            )
            if record is not None
        ]
        for record in records:
            age = (reference - record.event_timestamp).total_seconds()
            max_age_s = (
                self.long_short_ratio_max_age_s
                if isinstance(record, LongShortRatio)
                else self.max_age_s
            )
            if age > max_age_s:
                add(
                    "stale",
                    f"{type(record).__name__} is {age:.1f}s old (threshold {max_age_s:.1f}s)",
                    self.stale_action,
                )
            elif age < -self.max_clock_skew_s:
                add(
                    "clock_skew",
                    f"{type(record).__name__} is {-age:.1f}s ahead of the local clock",
                    self.stale_action,
                )
            if record.data_status is DataStatus.REJECTED:
                add("record_rejected", type(record).__name__, QualityDecision.REJECT)
            elif record.data_status is DataStatus.QUARANTINED:
                add("record_quarantined", type(record).__name__, QualityDecision.QUARANTINE)

        book = context.order_book
        if book is None:
            add("missing_order_book", "order book is missing", self.optional_missing_action)
        else:
            if book.crossed:
                add("crossed_book", "best bid >= best ask", self.book_action)
            if expected_book_sequence is not None and book.sequence != expected_book_sequence:
                add(
                    "sequence_gap",
                    f"expected book sequence {expected_book_sequence}, got {book.sequence}",
                    self.book_action,
                )
            if book.best_bid is not None and book.best_ask is not None:
                mid = (book.best_bid + book.best_ask) / 2
                spread_bps = abs(float((book.best_ask - book.best_bid) / mid * 10_000))
                if spread_bps > self.max_spread_bps:
                    add(
                        "excessive_spread",
                        f"spread {spread_bps:.2f} bps",
                        self.book_action,
                    )

        if context.spot is not None and context.futures is not None:
            perpetual = context.futures.mark_price or context.futures.last_price
            deviation = abs(float((perpetual - context.spot.last_price) / context.spot.last_price))
            deviation_bps = deviation * 10_000
            if deviation_bps > self.max_spot_perpetual_deviation_bps:
                add(
                    "spot_perpetual_divergence",
                    f"spot/perpetual deviation {deviation_bps:.2f} bps",
                    self.divergence_action,
                )

        decision = (
            max((issue.action for issue in issues), key=_DECISION_RANK.__getitem__)
            if issues
            else QualityDecision.ACCEPT
        )
        return CryptoQualityReport(decision=decision, issues=tuple(issues))
