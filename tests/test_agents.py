"""Agent tests for the multi-agent brain (L3) — local/deterministic, offline."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from argus.brain.agents import (
    AnalysisContext,
    MacroIntermarketAgent,
    NewsSentimentAgent,
    PortfolioAgent,
    PositioningAgent,
    RiskContextAgent,
    TechnicalAgent,
    default_agents,
)
from argus.core.contracts import Disclosure, NewsItem, OHLCVBar, OHLCVSeries
from argus.core.enums import AssetClass, EvidenceTier, Stance, Timeframe
from argus.core.schemas import DataStatus, Provenance

BASE = datetime(2026, 5, 29, tzinfo=UTC)

# Empirically verified series (measured with the indicators module):
#   bull: EMA12>EMA26 + RSI≈62 (neutral) → a single up vote → BULLISH
#   bear: EMA12<EMA26 + RSI≈38 (neutral) → a single down vote → BEARISH
_BULL = [100.0 + i for i in range(26)] + [
    124,
    122,
    120,
    121,
    123,
    122,
    124,
    123,
    122,
    124,
    123,
    125,
    124,
    123,
    125,
    124,
]
_BEAR = [140.0 - i for i in range(26)] + [
    116,
    118,
    120,
    119,
    117,
    118,
    116,
    117,
    118,
    116,
    117,
    115,
    116,
    117,
    115,
    116,
]
_MONO_UP = [100.0 + i * 0.5 for i in range(40)]  # monotonic → EMA↔RSI cancel out → NEUTRAL


def _series(closes: Sequence[float], *, tf: Timeframe = Timeframe.H1) -> OHLCVSeries:
    bars: list[OHLCVBar] = []
    for i, c in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else c
        bars.append(
            OHLCVBar(
                timestamp=BASE + timedelta(hours=i),
                open=open_,
                high=max(open_, c) + 1,
                low=min(open_, c) - 1,
                close=c,
                volume=1,
            )
        )
    last_ts = bars[-1].timestamp if bars else BASE
    prov = Provenance(source="test", timestamp=last_ts, confidence=0.9)
    return OHLCVSeries(symbol="X", timeframe=tf, bars=tuple(bars), provenance=prov)


def _ctx(closes: Sequence[float], **kw: object) -> AnalysisContext:
    return AnalysisContext(symbol="X", timeframe=Timeframe.H1, series=_series(closes), **kw)  # type: ignore[arg-type]


def _disclosure() -> Disclosure:
    return Disclosure(
        disclosure_id="1",
        symbol="X",
        title="Financial results published",
        published_at=BASE,
        provenance=Provenance(source="kap", timestamp=BASE, confidence=0.95),
    )


def _news(
    sentiment: float | None, *, symbols: tuple[str, ...] = ("X",), nid: str = "n1"
) -> NewsItem:
    return NewsItem(
        id=nid,
        title="News",
        published_at=BASE,
        symbols=symbols,
        sentiment=sentiment,
        provenance=Provenance(source="finnhub", timestamp=BASE, confidence=0.8),
    )


# --- TechnicalAgent ---


def test_technical_too_short_is_no_data() -> None:
    op = TechnicalAgent().assess(_ctx([100.0]))
    assert op.stance is Stance.NO_DATA
    assert op.data_status is DataStatus.PENDING
    assert "DATA PENDING" in op.rationale


def test_technical_not_warmed_is_no_data() -> None:
    op = TechnicalAgent().assess(_ctx([100.0, 101.0, 102.0]))  # indicators not warmed up
    assert op.stance is Stance.NO_DATA


def test_technical_bullish() -> None:
    op = TechnicalAgent().assess(_ctx(_BULL))
    assert op.stance is Stance.BULLISH
    assert op.evidence_tier is EvidenceTier.A
    assert op.data_status is DataStatus.OK
    assert op.confidence_pct == 65


def test_technical_bearish() -> None:
    op = TechnicalAgent().assess(_ctx(_BEAR))
    assert op.stance is Stance.BEARISH
    assert op.confidence_pct == 65


def test_technical_monotonic_is_neutral() -> None:
    # Monotonic rise: EMA up (+1) but RSI overbought (-1) → cancel out → NEUTRAL.
    op = TechnicalAgent().assess(_ctx(_MONO_UP))
    assert op.stance is Stance.NEUTRAL


# --- RiskContextAgent ---


def test_risk_context_reports_regime() -> None:
    op = RiskContextAgent().assess(_ctx(_BULL))
    assert op.stance is Stance.NEUTRAL  # context, not direction
    assert op.evidence_tier is EvidenceTier.A
    assert "volatility regime" in op.rationale


def test_risk_context_no_data_when_empty() -> None:
    op = RiskContextAgent().assess(_ctx([100.0]))  # ATR cannot be computed
    assert op.stance is Stance.NO_DATA


# --- NewsSentimentAgent ---


def test_news_no_disclosures_is_no_data() -> None:
    assert NewsSentimentAgent().assess(_ctx(_BULL)).stance is Stance.NO_DATA


def test_news_with_disclosures_is_neutral_context() -> None:
    op = NewsSentimentAgent().assess(_ctx(_BULL, disclosures=(_disclosure(),)))
    assert op.stance is Stance.NEUTRAL
    assert op.evidence_tier is EvidenceTier.B


def test_news_positive_sentiment_is_bullish() -> None:
    op = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(0.6),)))
    assert op.stance is Stance.BULLISH
    assert op.evidence_tier is EvidenceTier.B
    assert "+0.60" in op.rationale


def test_news_negative_sentiment_is_bearish() -> None:
    op = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(-0.5),)))
    assert op.stance is Stance.BEARISH


def test_news_weak_sentiment_in_band_is_neutral() -> None:
    op = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(0.05),)))
    assert op.stance is Stance.NEUTRAL


def test_news_more_items_raises_confidence() -> None:
    one = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(0.6, nid="a"),)))
    many = NewsSentimentAgent().assess(
        _ctx(_BULL, news=(_news(0.6, nid="a"), _news(0.6, nid="b"), _news(0.6, nid="c")))
    )
    assert many.confidence_pct > one.confidence_pct


def test_news_unrelated_symbol_filtered_out() -> None:
    # News tagged only for 'Y' → irrelevant to 'X' → NO_DATA without a disclosure either.
    op = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(0.9, symbols=("Y",)),)))
    assert op.stance is Stance.NO_DATA


def test_news_untagged_item_counts_as_relevant() -> None:
    op = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(0.6, symbols=()),)))
    assert op.stance is Stance.BULLISH


def test_news_sentiment_takes_precedence_over_disclosures() -> None:
    op = NewsSentimentAgent().assess(_ctx(_BULL, news=(_news(-0.5),), disclosures=(_disclosure(),)))
    assert op.stance is Stance.BEARISH


# --- MacroIntermarketAgent ---


def test_macro_without_data_is_no_data() -> None:
    assert MacroIntermarketAgent().assess(_ctx(_BULL)).stance is Stance.NO_DATA


def test_macro_with_data_but_no_asset_class_is_neutral_context() -> None:
    # Without asset_class the relation cannot be applied → context report.
    op = MacroIntermarketAgent().assess(_ctx(_BULL, macro={"DXY": 104.2}))
    assert op.stance is Stance.NEUTRAL
    assert "DXY" in op.rationale


def test_macro_dxy_up_pressures_gold() -> None:
    # DXY trend +1 (rising), inverse to gold → BEARISH.
    op = MacroIntermarketAgent().assess(
        _ctx(_BULL, macro={"dxy": 1.0}, asset_class=AssetClass.METAL)
    )
    assert op.stance is Stance.BEARISH
    assert "DXY↑→pressure" in op.rationale


def test_macro_dxy_down_supports_gold() -> None:
    op = MacroIntermarketAgent().assess(
        _ctx(_BULL, macro={"dxy": -1.0}, asset_class=AssetClass.METAL)
    )
    assert op.stance is Stance.BULLISH


def test_macro_spx_up_supports_crypto() -> None:
    op = MacroIntermarketAgent().assess(
        _ctx(_BULL, macro={"spx": 1.0}, asset_class=AssetClass.CRYPTO)
    )
    assert op.stance is Stance.BULLISH


def test_macro_relation_not_applicable_to_asset_is_neutral() -> None:
    # real_rate is defined for METAL only; not applied to CRYPTO → context report.
    op = MacroIntermarketAgent().assess(
        _ctx(_BULL, macro={"real_rate": 1.0}, asset_class=AssetClass.CRYPTO)
    )
    assert op.stance is Stance.NEUTRAL
    assert "no direction could be derived" in op.rationale


def test_macro_conflicting_signals_net_out() -> None:
    # DXY↑ (pressure -1) + VIX↓ (support +1) on crypto → net 0 → NEUTRAL.
    op = MacroIntermarketAgent().assess(
        _ctx(_BULL, macro={"dxy": 1.0, "vix": -1.0}, asset_class=AssetClass.CRYPTO)
    )
    assert op.stance is Stance.NEUTRAL


# --- Agents without a data layer yet ---


def test_positioning_and_portfolio_are_no_data() -> None:
    assert PositioningAgent().assess(_ctx(_BULL)).stance is Stance.NO_DATA
    assert PortfolioAgent().assess(_ctx(_BULL)).stance is Stance.NO_DATA


def test_default_agents_set() -> None:
    agents = default_agents()
    assert len(agents) == 6
    names = [a.name for a in agents]
    assert len(set(names)) == 6  # unique
    assert "technical" in names
