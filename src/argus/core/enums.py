"""System-wide shared constant sets (enums)."""

from __future__ import annotations

from enum import StrEnum


class AssetClass(StrEnum):
    """Asset class, in priority order.

    1) Turkish markets → 2) precious metals → 3) crypto; the rest is context.
    """

    BIST = "bist"  # BIST equities / VIOP derivatives
    METAL = "metal"  # gold, silver, commodities
    CRYPTO = "crypto"  # cryptocurrencies
    FX = "fx"  # currencies (secondary/context)
    EQUITY = "equity"  # US/global equities (secondary/context)

    @property
    def priority(self) -> int:
        """Lower number = higher priority (reported first when signals collide)."""
        return _PRIORITY[self]


_PRIORITY: dict[AssetClass, int] = {
    AssetClass.BIST: 1,
    AssetClass.METAL: 2,
    AssetClass.CRYPTO: 3,
    AssetClass.FX: 4,
    AssetClass.EQUITY: 4,
}


class Timeframe(StrEnum):
    """Timeframe — values match the ccxt timeframe strings exactly."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"
    MONTH1 = "1M"

    @property
    def seconds(self) -> int:
        """Nominal duration of one candle (seconds) — for gap/duplicate detection."""
        return _TF_SECONDS[self]


_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1_800,
    Timeframe.H1: 3_600,
    Timeframe.H4: 14_400,
    Timeframe.D1: 86_400,
    Timeframe.W1: 604_800,
    Timeframe.MONTH1: 2_592_000,  # ~30 days (approximate)
}


class EvidenceTier(StrEnum):
    """Evidence tier label.

    Precedence on conflict: A > B > C.
    """

    A = "A"  # computed data
    B = "B"  # observed on the chart
    C = "C"  # framework interpretation


class Severity(StrEnum):
    """Alert/trigger severity — for routing and prioritisation."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Sort key (larger = more severe) — for comparing severities."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Currency(StrEnum):
    """Currency — P&L is shown in both TRY and USD."""

    TRY = "TRY"
    USD = "USD"


class Stance(StrEnum):
    """Agent/proposal direction (multi-agent brain + L4 aggregator)."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    NO_DATA = "no_data"  # no data → DATA PENDING (does not count toward direction)


class MarketType(StrEnum):
    """Crypto market type; currently spot and USD-M perpetual."""

    SPOT = "spot"
    PERPETUAL = "perpetual"


class DataRepresentation(StrEnum):
    """Whether a record is raw from the provider or normalised."""

    RAW = "raw"
    TRANSFORMED = "transformed"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class MarketRegimeKind(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    HIGH_VOLATILITY = "high_volatility"
    LOW_LIQUIDITY = "low_liquidity"
    SQUEEZE_RISK = "squeeze_risk"
    EVENT_RISK = "event_risk"
    UNKNOWN = "unknown"


class RiskDecision(StrEnum):
    ALLOW_SHADOW = "allow_shadow"
    REQUIRE_HUMAN_REVIEW = "require_human_review"
    REJECT = "reject"
    HALT_SYSTEM = "halt_system"


class ShadowOrderStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ShadowPositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
