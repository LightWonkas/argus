"""L4 — confluence, deterministic GUARD and PIT crypto backtest layer.

Never grants live-order permission. The furthest decision GUARD can allow is the
human-approved, simulation-only ``ALLOW_SHADOW`` boundary.
"""

from argus.decision.aggregator import SignalAggregator, default_aggregator
from argus.decision.crypto_backtest import (
    BacktestIntent,
    CryptoBacktestCosts,
    CryptoBacktestReport,
    PerformanceMetrics,
    execute_spot_intents,
    run_crypto_backtest,
)
from argus.decision.guard import GuardRiskEngine, GuardState
from argus.decision.trade_planner import MultiHorizonTradePlanner

__all__ = [
    "BacktestIntent",
    "CryptoBacktestCosts",
    "CryptoBacktestReport",
    "GuardRiskEngine",
    "GuardState",
    "MultiHorizonTradePlanner",
    "PerformanceMetrics",
    "SignalAggregator",
    "default_aggregator",
    "execute_spot_intents",
    "run_crypto_backtest",
]
