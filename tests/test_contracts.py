"""Market-data contract (Quote / OHLCVBar / OHLCVSeries) tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from argus.core.contracts import (
    NOT_ADVICE,
    AnalysisOutput,
    Disclosure,
    NewsItem,
    OHLCVBar,
    OHLCVSeries,
    Quote,
    Scenario,
)
from argus.core.enums import EvidenceTier, Timeframe
from argus.core.schemas import Provenance, utcnow


def _prov() -> Provenance:
    return Provenance(source="test", timestamp=utcnow(), confidence=0.9)


def _bar(ts: datetime, close: int = 100) -> OHLCVBar:
    return OHLCVBar(timestamp=ts, open=100, high=110, low=90, close=close, volume=5)


def test_quote_valid() -> None:
    q = Quote(symbol="BTC/USDT", price=Decimal("50000"), quote_currency="USDT", provenance=_prov())
    assert q.price == Decimal("50000")
    assert q.quote_currency == "USDT"


def test_quote_rejects_nonpositive_price() -> None:
    with pytest.raises(ValidationError):
        Quote(symbol="BTC/USDT", price=Decimal("0"), quote_currency="USDT", provenance=_prov())


def test_ohlcv_bar_requires_tz() -> None:
    with pytest.raises(ValidationError):
        OHLCVBar(
            timestamp=datetime(2026, 1, 1),
            open=1,
            high=1,
            low=1,
            close=1,
            volume=0,  # naive
        )


@pytest.mark.parametrize(
    ("o", "h", "low", "c"),
    [
        (100, 90, 95, 96),  # high < low
        (120, 110, 90, 100),  # open > high
        (100, 110, 90, 120),  # close > high
        (80, 110, 90, 100),  # open < low
    ],
)
def test_ohlcv_bar_consistency(o: int, h: int, low: int, c: int) -> None:
    with pytest.raises(ValidationError):
        OHLCVBar(timestamp=utcnow(), open=o, high=h, low=low, close=c, volume=1)


def test_ohlcv_series_latest_and_closes() -> None:
    t0 = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)
    t1 = datetime(2026, 5, 29, 11, 0, tzinfo=UTC)
    series = OHLCVSeries(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        bars=(_bar(t0, close=100), _bar(t1, close=105)),
        provenance=_prov(),
    )
    assert series.latest is not None
    assert series.latest.close == Decimal("105")
    assert series.closes() == [Decimal("100"), Decimal("105")]


def test_empty_series_latest_is_none() -> None:
    series = OHLCVSeries(symbol="X/Y", timeframe=Timeframe.H1, provenance=_prov())
    assert series.latest is None
    assert series.closes() == []


def test_disclosure_valid() -> None:
    d = Disclosure(
        disclosure_id="1530826",
        symbol="ACME",
        title="Dividend distribution",
        published_at=utcnow(),
        provenance=_prov(),
    )
    assert d.disclosure_id == "1530826"


def test_disclosure_requires_tz() -> None:
    with pytest.raises(ValidationError):
        Disclosure(
            disclosure_id="1",
            symbol="ACME",
            title="x",
            published_at=datetime(2026, 1, 1),  # naive
            provenance=_prov(),
        )


def test_news_item_sentiment_bounds() -> None:
    with pytest.raises(ValidationError):
        NewsItem(
            id="n1",
            title="headline",
            published_at=utcnow(),
            sentiment=2.0,  # outside [-1..1]
            provenance=_prov(),
        )


def test_news_item_valid_with_symbols() -> None:
    item = NewsItem(
        id="n1",
        title="BIST news",
        published_at=utcnow(),
        symbols=("THYAO", "GARAN"),
        sentiment=0.4,
        provenance=_prov(),
    )
    assert item.symbols == ("THYAO", "GARAN")


def test_analysis_output_defaults_to_not_advice() -> None:
    out = AnalysisOutput(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        summary="overview",
        evidence_tier=EvidenceTier.A,
        confidence_pct=60,
        invalidation="close below X",
        model="m",
    )
    assert out.disclaimer == NOT_ADVICE
    assert "not financial advice" in out.disclaimer.lower()


def test_analysis_output_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        AnalysisOutput(
            symbol="X",
            timeframe=Timeframe.H1,
            summary="s",
            evidence_tier=EvidenceTier.C,
            confidence_pct=120,  # > 100
            invalidation="y",
            model="m",
        )


def test_scenario_probability_bounds() -> None:
    with pytest.raises(ValidationError):
        Scenario(name="Base", probability_pct=-5, detail="x")
