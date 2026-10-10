"""Cost-aware crypto backtest, leakage, walk-forward and baseline tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import MarketRegimeKind, Timeframe
from argus.core.schemas import Provenance
from argus.decision.crypto_backtest import (
    BacktestIntent,
    CryptoBacktestCosts,
    execute_spot_intents,
    run_crypto_backtest,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _series(symbol: str = "BTCUSDT", *, slope: float = 1.0, n: int = 150) -> OHLCVSeries:
    bars: list[OHLCVBar] = []
    for index in range(n):
        close = 100 + slope * index
        bars.append(
            OHLCVBar(
                timestamp=START + timedelta(hours=index),
                open=close - slope * 0.25,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1000,
            )
        )
    return OHLCVSeries(
        symbol=symbol,
        timeframe=Timeframe.H1,
        bars=tuple(bars),
        provenance=Provenance(source="pit:test", timestamp=bars[-1].timestamp, confidence=1),
    )


def _intent(series: OHLCVSeries, index: int, *, horizon: int = 5) -> BacktestIntent:
    return BacktestIntent(
        index=index,
        direction=1,
        horizon_bars=horizon,
        feature_end_index=index,
        evidence_groups=frozenset({"technical", "orderflow"}),
        generated_at=series.bars[index].timestamp,
    )


def test_point_in_time_guard_rejects_feature_and_timestamp_leakage() -> None:
    series = _series()
    leaked_feature = _intent(series, 20)
    leaked_feature = BacktestIntent(**{**leaked_feature.__dict__, "feature_end_index": 21})
    with pytest.raises(ValueError, match="look-ahead"):
        execute_spot_intents(series, [leaked_feature])

    leaked_time = _intent(series, 20)
    leaked_time = BacktestIntent(
        **{
            **leaked_time.__dict__,
            "generated_at": series.bars[22].timestamp,
        }
    )
    with pytest.raises(ValueError, match="look-ahead"):
        execute_spot_intents(series, [leaked_time])


def test_execution_uses_next_bar_costs_and_deduplicates_overlaps() -> None:
    series = _series()
    duplicate = _intent(series, 20)
    overlapping = _intent(series, 22)
    later = _intent(series, 40)
    zero_cost = execute_spot_intents(
        series,
        [duplicate, duplicate, overlapping, later],
        costs=CryptoBacktestCosts(
            commission_bps=0,
            spread_bps=0,
            slippage_bps=0,
            latency_bars=1,
        ),
    )
    costly = execute_spot_intents(series, [duplicate, later])

    assert len(zero_cost) == 2
    assert zero_cost[0].entry_index == 21
    assert zero_cost[0].exit_index == 26
    assert costly[0].net_return_pct < zero_cost[0].net_return_pct
    assert costly[0].cost_pct > 0


def test_full_report_has_splits_regimes_baselines_and_same_frequency_random() -> None:
    series = _series()
    eth = _series("ETHUSDT", slope=0.5)
    intents = [_intent(series, index) for index in (20, 45, 70, 95, 120)]
    regimes = {
        20: MarketRegimeKind.RANGE,
        45: MarketRegimeKind.TREND_UP,
        70: MarketRegimeKind.TREND_UP,
        95: MarketRegimeKind.HIGH_VOLATILITY,
        120: MarketRegimeKind.RANGE,
    }
    report = run_crypto_backtest(
        series,
        intents,
        regimes_by_index=regimes,
        benchmarks={"ETHUSDT": eth},
    )

    assert report.point_in_time_enforced and report.negative_results_preserved
    assert set(report.split_metrics) == {"train", "validation", "test"}
    assert "trend_up" in report.regime_metrics and "range" in report.regime_metrics
    assert "BTCUSDT_buy_and_hold" in report.baselines
    assert "ETHUSDT_buy_and_hold" in report.baselines
    assert "cash_no_trade" in report.baselines
    assert "momentum" in report.baselines
    assert report.baselines["random_same_frequency"].trade_count == report.metrics.trade_count
    assert report.baseline_delta_pct.keys() == report.baselines.keys()
    assert all(fold.parameters_locked_before_test for fold in report.walk_forward_folds)
    assert report.low_sample_warning is not None


def test_negative_result_is_reported_without_beautification() -> None:
    series = _series(slope=-0.5)
    report = run_crypto_backtest(
        series,
        [_intent(series, index) for index in (20, 45, 70, 95, 120)],
    )
    assert report.metrics.net_return_pct < 0
    assert report.negative_results_preserved
    assert report.metrics.total_cost_pct > 0


def test_partial_fill_changes_exposure_turnover_and_funding_cost() -> None:
    series = _series()
    intent = _intent(series, 30, horizon=10)
    full = run_crypto_backtest(series, [intent], costs=CryptoBacktestCosts(fill_ratio=1))
    partial = run_crypto_backtest(
        series,
        [intent],
        costs=CryptoBacktestCosts(fill_ratio=0.5, funding_rate_per_bar=0.0001),
    )
    assert partial.metrics.exposure_pct == pytest.approx(full.metrics.exposure_pct / 2, abs=0.0001)
    assert partial.metrics.turnover_pct == pytest.approx(full.metrics.turnover_pct / 2)
    assert partial.trades[0].cost_pct > full.trades[0].cost_pct / 2
