"""Crypto Data Quality Gate, trigger groups and independent-evidence sufficiency tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.crypto import (
    CryptoAnalysisContext,
    FeatureSnapshot,
    FuturesTicker,
    LongShortRatio,
    MarketRegime,
    OrderBookLevel,
    OrderBookSnapshot,
    SpotTicker,
)
from argus.core.enums import MarketRegimeKind, MarketType, Severity, Stance, Timeframe
from argus.core.schemas import DataStatus
from argus.platform.crypto_triggers import (
    CryptoTriggerEngine,
    CryptoTriggerGroup,
    qualifies_trade_candidate,
)
from argus.platform.quality import CryptoQualityGate, CryptoQualityReport, QualityDecision

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _spot(*, at: datetime = NOW, price: str = "100") -> SpotTicker:
    return SpotTicker(
        exchange="binance",
        symbol="BTCUSDT",
        event_timestamp=at,
        source="test:spot",
        last_price=price,
        bid="99.9",
        ask="100.1",
    )


def _futures(*, price: str = "100.1") -> FuturesTicker:
    return FuturesTicker(
        exchange="binance",
        symbol="BTCUSDT",
        event_timestamp=NOW,
        source="test:futures",
        last_price=price,
    )


def _book(*, bid: str = "99.9", ask: str = "100.1", sequence: int = 12) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        exchange="binance",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        event_timestamp=NOW,
        source="test:book",
        sequence=sequence,
        bids=(OrderBookLevel(price=bid, quantity="2"),),
        asks=(OrderBookLevel(price=ask, quantity="2"),),
    )


def _context(**updates: object) -> CryptoAnalysisContext:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "as_of": NOW,
        "spot": _spot(),
        "futures": _futures(),
        "order_book": _book(),
        "data_status": DataStatus.OK,
    }
    data.update(updates)
    return CryptoAnalysisContext(**data)


def test_crypto_quality_accepts_consistent_fresh_context() -> None:
    report = CryptoQualityGate().check(_context(), expected_book_sequence=12, now=NOW)
    assert report.decision is QualityDecision.ACCEPT
    assert report.allows_downstream


def test_crypto_quality_uses_period_aware_long_short_ratio_freshness() -> None:
    ratio = LongShortRatio(
        exchange="binance",
        symbol="BTCUSDT",
        event_timestamp=NOW - timedelta(minutes=10),
        source="test:ratio",
        long_ratio="0.55",
        short_ratio="0.45",
    )
    gate = CryptoQualityGate(max_age_s=30, long_short_ratio_max_age_s=660)

    accepted = gate.check(_context(long_short_ratio=ratio), now=NOW)
    stale = gate.check(
        _context(
            long_short_ratio=ratio.model_copy(
                update={"event_timestamp": NOW - timedelta(minutes=12)}
            )
        ),
        now=NOW,
    )

    assert accepted.decision is QualityDecision.ACCEPT
    assert stale.decision is QualityDecision.QUARANTINE


def test_crypto_quality_blocks_stale_crossed_gap_spread_and_divergence() -> None:
    gate = CryptoQualityGate(max_age_s=10, max_spread_bps=5, max_spot_perpetual_deviation_bps=50)
    report = gate.check(
        _context(
            spot=_spot(at=NOW - timedelta(minutes=1)),
            futures=_futures(price="110"),
            order_book=_book(bid="101", ask="100", sequence=10),
        ),
        expected_book_sequence=12,
        now=NOW,
    )
    kinds = {issue.kind for issue in report.issues}
    assert report.decision is QualityDecision.QUARANTINE
    assert not report.allows_downstream
    assert {
        "stale",
        "crossed_book",
        "sequence_gap",
        "excessive_spread",
        "spot_perpetual_divergence",
    } <= kinds


def _features() -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        as_of=NOW,
        values={
            "breakout": "up",
            "ema_trend": "up",
            "mtf_direction": "up",
            "normalized_atr_pct": 2.0,
            "aggressive_buy_ratio": 0.8,
            "book_imbalance_10": 0.4,
            "funding_rate": Decimal("-0.0012"),
            "open_interest_change_pct": 10.0,
            "long_liquidation_notional": Decimal("10"),
            "short_liquidation_notional": Decimal("100"),
        },
        evidence_sources={
            "technical": ("ohlcv:btc:1h",),
            "trade_flow": ("trades:binance",),
            "order_book": ("book:binance",),
            "derivatives": ("funding:binance", "oi:binance"),
            "liquidations": ("liq:binance",),
        },
    )


def _regime() -> MarketRegime:
    return MarketRegime(
        symbol="BTCUSDT",
        as_of=NOW,
        regime=MarketRegimeKind.TREND_UP,
        confidence_pct=75,
        features_used=("ema_trend", "mtf_direction"),
    )


def test_crypto_trigger_groups_and_two_independent_source_candidate_gate() -> None:
    quality = CryptoQualityReport(QualityDecision.ACCEPT)
    triggers = CryptoTriggerEngine().evaluate(
        _features(),
        _regime(),
        quality,
        previous_regime=MarketRegimeKind.RANGE,
    )
    groups = {trigger.group for trigger in triggers}
    assert {
        CryptoTriggerGroup.TECHNICAL,
        CryptoTriggerGroup.ORDER_FLOW,
        CryptoTriggerGroup.FUNDING_OI,
        CryptoTriggerGroup.LIQUIDATION,
        CryptoTriggerGroup.REGIME,
    } <= groups
    assert qualifies_trade_candidate(triggers)

    technical_only = [item for item in triggers if item.group is CryptoTriggerGroup.TECHNICAL]
    assert not qualifies_trade_candidate(technical_only)


def test_quarantined_quality_emits_only_operational_risk_and_never_candidate() -> None:
    quality = CryptoQualityReport(QualityDecision.QUARANTINE)
    triggers = CryptoTriggerEngine().evaluate(_features(), _regime(), quality)
    assert len(triggers) == 1
    assert triggers[0].group is CryptoTriggerGroup.DATA_OPERATIONAL
    assert triggers[0].direction is None
    assert triggers[0].severity is Severity.HIGH
    assert not qualifies_trade_candidate(triggers)


def test_conflicting_directional_groups_do_not_form_candidate() -> None:
    quality = CryptoQualityReport(QualityDecision.ACCEPT)
    triggers = CryptoTriggerEngine().evaluate(
        _features(),
        _regime(),
        quality,
        news_event=(Stance.BEARISH, Severity.HIGH, "news:event-1"),
    )
    technical = next(item for item in triggers if item.group is CryptoTriggerGroup.TECHNICAL)
    news = next(item for item in triggers if item.group is CryptoTriggerGroup.NEWS_EVENT)
    assert not qualifies_trade_candidate([technical, news])
