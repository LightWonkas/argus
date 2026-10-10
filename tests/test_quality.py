"""Data Quality Gate (QualityGate) tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import Timeframe
from argus.core.schemas import FreshnessLabel, Provenance
from argus.platform import QualityGate
from argus.platform.quality import QualityDecision

BASE = datetime(2026, 5, 29, 0, 0, tzinfo=UTC)


def _bar(
    hours: float, *, close: int = 100, open_: int = 100, high: int = 110, low: int = 90
) -> OHLCVBar:
    return OHLCVBar(
        timestamp=BASE + timedelta(hours=hours),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1,
    )


def _series(bars: list[OHLCVBar]) -> OHLCVSeries:
    last_ts = bars[-1].timestamp if bars else BASE
    prov = Provenance(source="ccxt:binance", timestamp=last_ts, confidence=0.95)
    return OHLCVSeries(symbol="BTC/USDT", timeframe=Timeframe.H1, bars=tuple(bars), provenance=prov)


def test_clean_series_ok() -> None:
    bars = [_bar(h) for h in range(5)]
    now = BASE + timedelta(hours=4, minutes=1)
    report = QualityGate().check_ohlcv(_series(bars), now=now)

    assert report.ok is True
    assert not report.has_issues
    assert report.series.provenance.freshness is FreshnessLabel.FRESH
    assert len(report.series.bars) == 5


def test_empty_series_not_ok() -> None:
    report = QualityGate().check_ohlcv(_series([]))
    assert report.ok is False
    assert report.issues_of("empty")


def test_duplicate_quarantined() -> None:
    bars = [_bar(0), _bar(1), _bar(1, close=101)]  # second hour duplicated
    now = BASE + timedelta(hours=1, minutes=1)
    report = QualityGate().check_ohlcv(_series(bars), now=now)

    assert report.issues_of("duplicate")
    assert len(report.quarantined) == 1
    # cleaned series has unique timestamps
    ts = [b.timestamp for b in report.series.bars]
    assert len(ts) == len(set(ts)) == 2


def test_disorder_sorted() -> None:
    bars = [_bar(2), _bar(0), _bar(1)]
    now = BASE + timedelta(hours=2, minutes=1)
    report = QualityGate().check_ohlcv(_series(bars), now=now)

    assert report.issues_of("disorder")
    ordered = [b.timestamp for b in report.series.bars]
    assert ordered == sorted(ordered)


def test_gap_detected() -> None:
    bars = [_bar(0), _bar(5)]  # 4 bars missing on the 1h timeframe
    now = BASE + timedelta(hours=5, minutes=1)
    report = QualityGate().check_ohlcv(_series(bars), now=now)

    gaps = report.issues_of("gap")
    assert gaps
    assert "4 bars missing" in gaps[0].detail
    assert report.decision is QualityDecision.QUARANTINE
    assert report.ok is False


def test_outlier_detected() -> None:
    bars = [_bar(0, close=100), _bar(1, close=200, open_=100, high=210, low=99)]
    now = BASE + timedelta(hours=1, minutes=1)
    report = QualityGate(max_jump_pct=25.0).check_ohlcv(_series(bars), now=now)

    assert report.issues_of("outlier")
    assert report.decision is QualityDecision.QUARANTINE
    assert report.ok is False


def test_warning_policy_can_accept_cleaned_duplicate() -> None:
    bars = [_bar(0), _bar(1), _bar(1, close=101)]
    report = QualityGate().check_ohlcv(_series(bars), now=BASE + timedelta(hours=1))
    assert report.decision is QualityDecision.ACCEPT_WITH_WARNING
    assert report.ok is True


def test_insufficient_history_and_future_clock_skew_block() -> None:
    short = QualityGate(min_bars=3).check_ohlcv(_series([_bar(0)]), now=BASE)
    assert short.decision is QualityDecision.QUARANTINE
    assert short.issues_of("insufficient_history")

    future = QualityGate().check_ohlcv(_series([_bar(1)]), now=BASE)
    assert future.decision is QualityDecision.QUARANTINE
    assert future.issues_of("future")


def test_stale_series_not_ok() -> None:
    bars = [_bar(h) for h in range(3)]
    now = BASE + timedelta(hours=12)  # newest bar is ~10 hours old
    report = QualityGate().check_ohlcv(_series(bars), now=now)

    assert report.ok is False
    assert report.issues_of("stale")
    assert report.series.provenance.freshness is FreshnessLabel.EXPIRED


# ── daily timeframe + weekend tolerance (BIST/metal Friday→Monday) ───────────


def _daily_bar(d: datetime, close: int = 100) -> OHLCVBar:
    return OHLCVBar(timestamp=d, open=close, high=close + 10, low=close - 10, close=close, volume=1)


def _daily_series(days: list[datetime]) -> OHLCVSeries:
    bars = tuple(_daily_bar(d) for d in days)
    last = days[-1] if days else BASE
    prov = Provenance(source="borsapy", timestamp=last, confidence=0.95)
    return OHLCVSeries(symbol="XU100", timeframe=Timeframe.D1, bars=bars, provenance=prov)


def test_daily_friday_close_fresh_on_monday_morning() -> None:
    # Regression scenario: Friday close → Monday morning. Three calendar days, but two
    # of them are weekend days (exchange closed Sat/Sun), so the series is NOT stale.
    # Rejecting it would freeze daily BIST data at the last Friday bar.
    fridays = [
        datetime(2026, 6, 10, 6, tzinfo=UTC),  # Wed
        datetime(2026, 6, 11, 6, tzinfo=UTC),  # Thu
        datetime(2026, 6, 12, 6, tzinfo=UTC),  # Fri (last bar)
    ]
    monday_morning = datetime(2026, 6, 15, 9, tzinfo=UTC)
    report = QualityGate().check_ohlcv(_daily_series(fridays), now=monday_morning)

    assert report.ok is True  # weekend tolerance → valid data
    assert not report.issues_of("stale")
    assert report.series.provenance.freshness is FreshnessLabel.FRESH


def test_daily_genuinely_stale_midweek_still_flagged() -> None:
    # Genuine midweek delay (feed stopped): no weekend excuse → stays stale.
    days = [
        datetime(2026, 6, 8, 6, tzinfo=UTC),  # Mon
        datetime(2026, 6, 9, 6, tzinfo=UTC),  # Tue (last bar)
    ]
    friday_after = datetime(2026, 6, 12, 12, tzinfo=UTC)  # Fri — 3 weekdays after Tue
    report = QualityGate().check_ohlcv(_daily_series(days), now=friday_after)

    assert report.ok is False  # genuine staleness (feed stopped)
    assert report.issues_of("stale")
