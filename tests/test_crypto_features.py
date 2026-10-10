"""Crypto feature and regime classification tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.crypto import (
    BasisSnapshot,
    FeatureSnapshot,
    FundingRate,
    LiquidationEvent,
    LongShortRatio,
    OpenInterest,
    OrderBookLevel,
    OrderBookSnapshot,
    Trade,
)
from argus.core.enums import MarketRegimeKind, MarketType, OrderSide, Timeframe
from argus.core.schemas import DataStatus, Provenance
from argus.platform.crypto_features import classify_market_regime, compute_crypto_features

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _series(*, direction: int = 1, last_jump: float = 0) -> OHLCVSeries:
    bars: list[OHLCVBar] = []
    for index in range(60):
        close = 100 + direction * index * 0.2
        if index == 59:
            close += last_jump
        bars.append(
            OHLCVBar(
                timestamp=NOW - timedelta(hours=59 - index),
                open=close - direction * 0.1,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=100 if index < 59 else 500,
            )
        )
    return OHLCVSeries(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        bars=tuple(bars),
        provenance=Provenance(source="test:ohlcv", timestamp=NOW, confidence=1),
        exchange="binance",
        market_type=MarketType.SPOT,
    )


def _market_kwargs() -> dict[str, object]:
    return {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "event_timestamp": NOW,
        "source": "test",
    }


def _book(*, bid: str = "109", ask: str = "110") -> OrderBookSnapshot:
    return OrderBookSnapshot(
        **_market_kwargs(),
        market_type=MarketType.SPOT,
        sequence=10,
        bids=(
            OrderBookLevel(price=bid, quantity="10"),
            OrderBookLevel(price=str(Decimal(bid) - 1), quantity="1"),
            OrderBookLevel(price=str(Decimal(bid) - 2), quantity="1"),
        ),
        asks=(
            OrderBookLevel(price=ask, quantity="1"),
            OrderBookLevel(price=str(Decimal(ask) + 1), quantity="1"),
            OrderBookLevel(price=str(Decimal(ask) + 2), quantity="1"),
        ),
    )


def test_crypto_features_include_technical_derivatives_and_order_flow() -> None:
    common = _market_kwargs()
    funding_old = FundingRate(**common, rate="0.0005")
    funding = FundingRate(**common, rate="0.0012")
    oi_old = OpenInterest(**common, contracts="100")
    oi = OpenInterest(**common, contracts="110")
    basis = BasisSnapshot(**common, spot_price="110", perpetual_price="111")
    ratio = LongShortRatio(**common, long_ratio="0.6", short_ratio="0.4", account_ratio="1.5")
    trades = (
        Trade(
            **common,
            market_type=MarketType.SPOT,
            trade_id="t1",
            side=OrderSide.BUY,
            price="110",
            quantity="8",
        ),
        Trade(
            **common,
            market_type=MarketType.SPOT,
            trade_id="t2",
            side=OrderSide.SELL,
            price="110",
            quantity="2",
        ),
    )
    liquidations = (
        LiquidationEvent(
            **common,
            liquidation_id="l1",
            side=OrderSide.SELL,
            price="110",
            quantity="2",
        ),
    )
    book = _book()

    snapshot = compute_crypto_features(
        _series(last_jump=5),
        funding=funding,
        previous_funding=funding_old,
        open_interest=oi,
        previous_open_interest=oi_old,
        basis=basis,
        long_short_ratio=ratio,
        order_book=book,
        book_history=(book, book),
        trades=trades,
        liquidations=liquidations,
    )

    assert snapshot.data_status is DataStatus.OK
    assert snapshot.values["ema_trend"] == "up"
    assert snapshot.values["breakout"] == "up"
    assert snapshot.values["funding_change"] == Decimal("0.0007")
    assert snapshot.values["open_interest_change_pct"] == pytest.approx(10)
    assert snapshot.values["basis_pct"] == pytest.approx(Decimal("0.9090909"))
    assert snapshot.values["aggressive_buy_ratio"] == pytest.approx(0.8)
    assert snapshot.values["cvd_base"] == Decimal("6")
    assert snapshot.values["persistent_wall_count"] == 1
    assert snapshot.values["squeeze_risk"] is True
    assert "technical" in snapshot.evidence_sources


def _features(**values: object) -> FeatureSnapshot:
    defaults: dict[str, object] = {
        "normalized_atr_pct": 1.0,
        "spread_bps": 2.0,
        "ema_trend": "flat",
        "mtf_direction": "mixed",
        "breakout": "range",
        "range_position": 0.5,
        "squeeze_risk": False,
    }
    defaults.update(values)
    return FeatureSnapshot(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        as_of=NOW,
        values=defaults,
    )


@pytest.mark.parametrize(
    ("updates", "event_risk", "expected"),
    [
        (
            {"ema_trend": "up", "mtf_direction": "up", "breakout": "up"},
            False,
            MarketRegimeKind.TREND_UP,
        ),
        (
            {"ema_trend": "down", "mtf_direction": "down", "breakout": "down"},
            False,
            MarketRegimeKind.TREND_DOWN,
        ),
        ({}, False, MarketRegimeKind.RANGE),
        ({"normalized_atr_pct": 5.0}, False, MarketRegimeKind.HIGH_VOLATILITY),
        ({"spread_bps": 30.0}, False, MarketRegimeKind.LOW_LIQUIDITY),
        ({"squeeze_risk": True, "breakout": "none"}, False, MarketRegimeKind.SQUEEZE_RISK),
        ({}, True, MarketRegimeKind.EVENT_RISK),
    ],
)
def test_market_regime_classification(
    updates: dict[str, object], event_risk: bool, expected: MarketRegimeKind
) -> None:
    regime = classify_market_regime(_features(**updates), event_risk=event_risk)
    assert regime.regime is expected
    assert regime.confidence_pct > 0


def test_feature_layer_never_fills_missing_external_data_with_zero() -> None:
    snapshot = compute_crypto_features(_series())
    assert snapshot.values["funding_rate"] is None
    assert snapshot.values["open_interest"] is None
    assert snapshot.values["spread_bps"] is None
    assert "funding" in snapshot.missing_data and "order_book" in snapshot.missing_data
