"""L2 — Data Platform.

Normalization · Data Quality Gate (schema/range/outlier/gap/duplicate, corporate-action
adjustment, quarantine) · point-in-time store · indicator service · event bus.
"""

from argus.platform.crypto_features import classify_market_regime, compute_crypto_features
from argus.platform.crypto_triggers import (
    CryptoTrigger,
    CryptoTriggerEngine,
    CryptoTriggerGroup,
    qualifies_trade_candidate,
)
from argus.platform.event_bus import EventBus, InMemoryEventBus, RedisEventBus
from argus.platform.indicators import atr, ema, macd, rsi, sma, true_ranges
from argus.platform.intraday_features import (
    REQUIRED_INTRADAY_TIMEFRAMES,
    analyze_closed_timeframe,
    analyze_intraday_timeframes,
    closed_bars,
)
from argus.platform.news_intelligence import NewsIntelligenceEngine, NewsIntelligencePolicy
from argus.platform.news_reaction import NewsMarketReactionAnalyzer
from argus.platform.quality import (
    CryptoQualityGate,
    CryptoQualityIssue,
    CryptoQualityReport,
    QualityDecision,
    QualityGate,
    QualityIssue,
    QualityReport,
)
from argus.platform.triggers import (
    TriggerConfig,
    TriggerEngine,
    TriggerKind,
    TriggerSignal,
    should_wake_brain,
)

__all__ = [
    "REQUIRED_INTRADAY_TIMEFRAMES",
    "CryptoQualityGate",
    "CryptoQualityIssue",
    "CryptoQualityReport",
    "CryptoTrigger",
    "CryptoTriggerEngine",
    "CryptoTriggerGroup",
    "EventBus",
    "InMemoryEventBus",
    "NewsIntelligenceEngine",
    "NewsIntelligencePolicy",
    "NewsMarketReactionAnalyzer",
    "QualityDecision",
    "QualityGate",
    "QualityIssue",
    "QualityReport",
    "RedisEventBus",
    "TriggerConfig",
    "TriggerEngine",
    "TriggerKind",
    "TriggerSignal",
    "analyze_closed_timeframe",
    "analyze_intraday_timeframes",
    "atr",
    "classify_market_regime",
    "closed_bars",
    "compute_crypto_features",
    "ema",
    "macd",
    "qualifies_trade_candidate",
    "rsi",
    "should_wake_brain",
    "sma",
    "true_ranges",
]
