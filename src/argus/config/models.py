"""Typed models for the editable "data" config.

These are NOT secrets; they live in ``config/*.yaml``, are committed to git and can be
changed at runtime through hot-reload.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus.core.enums import AssetClass
from argus.core.intelligence import (
    NewsEventType,
    NewsReliabilityTier,
    NewsSourceType,
    SourceAvailability,
    SourceGroup,
)
from argus.core.trading import MissionEndPolicy, TradingMode

# Tiered scan levels of the watch loop.
WatchTier = Literal["holdings", "watchlist", "broad"]
# Matches the Timeframe enum values one-to-one (ccxt strings).
TimeframeName = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]
MarketTypeName = Literal["spot", "perpetual"]


def _default_market_types() -> list[MarketTypeName]:
    return ["spot", "perpetual"]


def _default_timeframes() -> list[TimeframeName]:
    return ["1m", "5m", "15m", "30m", "1h", "4h"]


class WatchlistItem(BaseModel):
    """A single instrument to watch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(
        ..., min_length=1, description="Instrument, e.g. 'THYAO', 'XAUUSD', 'BTCUSDT'"
    )
    asset_class: AssetClass
    tier: WatchTier = "watchlist"
    note: str = ""
    # Multi-timeframe: the timeframes this instrument is scanned on. EMPTY = loop.timeframe
    # (backwards compatible: existing configs keep their behaviour). Cost awareness: multi-TF
    # is recommended for the holdings tier only; other tiers should stay single-TF. BIST/borsapy
    # has no 4h — an unsupported TF raises an explicit AdapterError during the scan (no silent
    # fallback).
    timeframes: list[TimeframeName] = Field(
        default_factory=list, description="Timeframes to scan (empty → loop.timeframe)"
    )


class HoldingItem(BaseModel):
    """A single portfolio holding — kept in config (no execution; entered manually).

    Cost is in ``cost_currency``; value and P&L are computed from the latest price.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1)
    asset_class: AssetClass
    quantity: float = Field(..., gt=0, description="Quantity (shares/lots/units)")
    avg_cost: float = Field(..., ge=0, description="Average unit cost (cost_currency)")
    cost_currency: Literal["TRY", "USD"] = "USD"
    owner: str = Field(default="", description="Optional owner label")


AlertSeverityName = Literal["info", "low", "medium", "high", "critical"]


class TriggerThresholds(BaseModel):
    """Technical trigger thresholds (config = data; not hard-coded)."""

    model_config = ConfigDict(extra="forbid")

    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    price_move_pct: float = 5.0
    atr_period: int = 14
    atr_spike_mult: float = 2.0
    ema_fast: int = 12
    ema_slow: int = 26


class QualityConfig(BaseModel):
    """Data Quality Gate thresholds (config = data)."""

    model_config = ConfigDict(extra="forbid")

    stale_factor: float = 3.0
    max_jump_pct: float = 25.0
    gap_factor: float = 1.5
    min_bars: int = Field(default=1, gt=0)
    max_clock_skew_s: float = Field(default=30.0, ge=0)
    crypto_max_age_s: float = Field(default=30.0, gt=0)
    # Binance's 5m ratio record carries the bucket start; when the last completed bucket
    # is published its apparent age can approach two periods.
    long_short_ratio_max_age_s: float = Field(default=660.0, gt=0)
    crypto_max_spread_bps: float = Field(default=20.0, gt=0)
    max_spot_perpetual_deviation_bps: float = Field(default=100.0, gt=0)
    crypto_missing_required_action: Literal[
        "accept", "accept_with_warning", "quarantine", "reject"
    ] = "reject"
    crypto_book_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = (
        "quarantine"
    )
    crypto_divergence_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = (
        "quarantine"
    )
    empty_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = "reject"
    stale_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = "quarantine"
    duplicate_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = (
        "accept_with_warning"
    )
    gap_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = "quarantine"
    disorder_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = (
        "accept_with_warning"
    )
    outlier_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = "quarantine"
    future_action: Literal["accept", "accept_with_warning", "quarantine", "reject"] = "quarantine"
    insufficient_history_action: Literal[
        "accept", "accept_with_warning", "quarantine", "reject"
    ] = "quarantine"


class LoopConfig(BaseModel):
    """Watch loop parameters (config = data)."""

    model_config = ConfigDict(extra="forbid")

    timeframe: TimeframeName = "1h"
    limit: int = Field(default=100, gt=0)
    min_severity: AlertSeverityName = "medium"  # gate for waking the brain
    cooldown_s: float = Field(default=900.0, ge=0)
    fetch_retries: int = Field(default=2, ge=1)  # data fetch attempts (transient errors)
    circuit_threshold: int = Field(default=5, ge=1)  # consecutive errors → source disabled
    circuit_reset_s: float = Field(default=300.0, gt=0)  # circuit breaker timeout
    holdings_interval_s: int = Field(default=300, gt=0)
    watchlist_interval_s: int = Field(default=900, gt=0)
    broad_interval_s: int = Field(default=3600, gt=0)

    def interval_for(self, tier: WatchTier) -> int:
        return {
            "holdings": self.holdings_interval_s,
            "watchlist": self.watchlist_interval_s,
            "broad": self.broad_interval_s,
        }[tier]


class AlertConfig(BaseModel):
    """Alert prioritisation + quiet hours settings (editable data)."""

    model_config = ConfigDict(extra="forbid")

    min_instant_severity: AlertSeverityName = "high"  # ≥ this → instant; below → digest
    quiet_start: str | None = None  # local "HH:MM" (None → no quiet hours)
    quiet_end: str | None = None
    quiet_tz: str = "Europe/Istanbul"
    critical_overrides_quiet: bool = True
    briefing_interval_s: int = 86_400  # digest flush interval (default: once a day)


class MacroConfig(BaseModel):
    """EVDS macro feed — series codes are config = data (not hard-coded).

    Keys are the metric names :class:`~argus.brain.agents.MacroIntermarketAgent`
    understands; values are CBRT EVDS series codes. Verify the default codes in your own
    smoke run (if an EVDS code changes, fix it here; no code change needed). ``real_rate``
    is derived separately: ``policy_rate − cpi_yoy`` (when both are available).
    """

    model_config = ConfigDict(extra="forbid")

    series: dict[str, str] = Field(
        default_factory=lambda: {
            "usdtry": "TP.DK.USD.S.YTL",  # USD/TRY selling rate
            "policy_rate": "TP.APIFON4",  # CBRT average funding cost
            "cpi_yoy": "TP.FE.OKTG01",  # CPI year-over-year % change
        },
        description="metric name → EVDS series code",
    )
    stale_after_h: int = Field(default=48, gt=0, description="Macro data older than this → NO_DATA")
    lookback: int = Field(
        default=12, gt=1, description="Number of observations to fetch for the trend"
    )


class OutcomeConfig(BaseModel):
    """Self-monitoring (hit rate v2) parameters (config = data, not hard-coded).

    The threshold is volatility-normalised: ``k_atr × ATR at analysis time`` (not a fixed %).
    Samples below ``min_n_warn`` are flagged "statistically insignificant" in the cockpit.
    """

    model_config = ConfigDict(extra="forbid")

    k_atr: float = Field(
        default=1.0, gt=0, description="Threshold multiplier (threshold = k × ATR)"
    )
    atr_period: int = Field(default=14, gt=0, description="ATR period at analysis time (PIT)")
    horizon_bars: int = Field(default=5, gt=0, description="Horizon = timeframe × this multiplier")
    min_n_warn: int = Field(
        default=30, gt=0, description="Below this n a 'low sample' warning is shown"
    )


class UniverseConfig(BaseModel):
    """Allowed crypto universe; strict is only enabled in the crypto-focused config."""

    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"], min_length=1)
    strict: bool = False

    @field_validator("symbols")
    @classmethod
    def _unique_upper_symbols(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not value for value in normalized):
            raise ValueError("universe symbols must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("universe symbols must be unique")
        return normalized


class ExchangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="binance", min_length=1)
    enabled: bool = True
    public_data_only: bool = True
    rest_enabled: bool = True
    websocket_enabled: bool = True


class WebSocketCompressionConfig(BaseModel):
    """Explicit WebSocket compression policy for reproducible A/B runs."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class WebSocketConfig(BaseModel):
    """Bounded public stream transport and liveness settings.

    ``read_timeout_s`` is an application-level no-message guard. Ping/pong is
    managed only by the ``websockets`` library; Argus doesn't send a second ping.
    """

    model_config = ConfigDict(extra="forbid")

    compression: WebSocketCompressionConfig = Field(default_factory=WebSocketCompressionConfig)
    max_message_size: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    max_queue_messages: int = Field(default=16, ge=1, le=1024)
    ping_interval_s: float = Field(default=20.0, gt=0, le=300)
    ping_timeout_s: float = Field(default=20.0, gt=0, le=300)
    open_timeout_s: float = Field(default=10.0, gt=0, le=120)
    close_timeout_s: float = Field(default=10.0, gt=0, le=120)
    read_timeout_s: float = Field(default=30.0, gt=0, le=600)
    stable_connection_reset_s: float = Field(default=900.0, gt=0, le=86_400)


class IngestionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backfill_limit: int = Field(default=500, gt=0, le=1500)
    reconcile_interval_s: int = Field(default=300, gt=0)
    reconnect_base_s: float = Field(default=1.0, gt=0)
    reconnect_max_s: float = Field(default=60.0, gt=0)
    reconnect_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    # Backwards-compatible surface. New stream code uses ``websocket.read_timeout_s``.
    disconnect_after_s: float = Field(default=30.0, gt=0)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    rate_limit_per_s: float = Field(default=10.0, gt=0)
    order_book_depth: int = Field(default=100, gt=0, le=5000)
    # The stream processes every trade/depth event in memory; default persistence is a
    # compact 1m rollup. Raw JSON is written only with an explicit research/debug opt-in.
    persist_raw_trades: bool = False
    persist_raw_depth_deltas: bool = False
    order_book_materialize_interval_s: float = Field(default=10.0, ge=0.1, le=60.0)
    stream_commit_batch_size: int = Field(default=250, gt=0, le=10_000)

    @field_validator("order_book_depth")
    @classmethod
    def _supported_binance_depth(cls, value: int) -> int:
        if value not in {5, 10, 20, 50, 100, 500, 1000}:
            raise ValueError("order_book_depth must be one of the Binance snapshot depths")
        return value

    @model_validator(mode="after")
    def _reconnect_bounds(self) -> IngestionConfig:
        if self.reconnect_max_s < self.reconnect_base_s:
            raise ValueError("reconnect_max_s must not be less than reconnect_base_s")
        return self


class FeatureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ema_fast: int = Field(default=12, gt=0)
    ema_slow: int = Field(default=26, gt=0)
    rsi_period: int = Field(default=14, gt=1)
    atr_period: int = Field(default=14, gt=1)
    realized_vol_window: int = Field(default=24, gt=1)
    volume_zscore_window: int = Field(default=30, gt=2)
    order_book_depths: list[int] = Field(default_factory=lambda: [5, 10, 25], min_length=1)

    @model_validator(mode="after")
    def _ema_order(self) -> FeatureConfig:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be less than ema_slow")
        return self


class RegimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    high_volatility_natr_pct: float = Field(default=4.0, gt=0)
    low_liquidity_spread_bps: float = Field(default=20.0, gt=0)
    squeeze_funding_abs: float = Field(default=0.001, gt=0)
    min_confidence_pct: int = Field(default=55, ge=0, le=100)


class LLMRoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routine_web_search: Literal[False] = False
    deep_web_search: bool = False
    onchain_enabled: bool = False


class RiskConfig(BaseModel):
    """Non-bypassable baseline safety limits; nonsensical combinations are rejected at startup."""

    model_config = ConfigDict(extra="forbid")

    spot_only: Literal[True] = True
    leverage: Literal[0] = 0
    live_orders_enabled: Literal[False] = False
    human_approval_required: Literal[True] = True
    default_decision: Literal["no_trade"] = "no_trade"
    max_portfolio_risk_pct: float = Field(default=0.5, gt=0, le=5)
    daily_loss_limit_pct: float = Field(default=2.0, gt=0, le=20)
    weekly_loss_limit_pct: float = Field(default=5.0, gt=0, le=30)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=50)
    max_concurrent_positions: int = Field(default=1, ge=0, le=10)
    max_btc_eth_correlation: float = Field(default=0.9, ge=0, le=1)
    max_correlated_exposure_pct: float = Field(default=25.0, gt=0, le=100)
    min_quote_volume_24h: float = Field(default=50_000_000.0, ge=0)
    max_spread_bps: float = Field(default=15.0, gt=0)
    max_slippage_bps: float = Field(default=20.0, gt=0)
    max_data_age_s: float = Field(default=30.0, gt=0)
    max_normalized_atr_pct: float = Field(default=8.0, gt=0)
    cooldown_s: float = Field(default=900.0, ge=0)
    kill_switch: bool = False


class ShadowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    commission_bps: float = Field(default=10.0, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)
    partial_fill_ratio: float = Field(default=1.0, gt=0, le=1)
    timeout_s: int = Field(default=3600, gt=0)


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    train_ratio: float = Field(default=0.6, gt=0, lt=1)
    validation_ratio: float = Field(default=0.2, gt=0, lt=1)
    test_ratio: float = Field(default=0.2, gt=0, lt=1)
    walk_forward: bool = True
    include_commission: bool = True
    include_spread: bool = True
    include_slippage: bool = True
    include_funding: bool = True

    @model_validator(mode="after")
    def _split_total(self) -> BacktestConfig:
        if abs(self.train_ratio + self.validation_ratio + self.test_ratio - 1.0) > 1e-9:
            raise ValueError("train/validation/test ratios must sum to 1")
        return self


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_llm_limit_usd: float = Field(default=100.0, ge=0)


class TradingConfig(BaseModel):
    """Type-level fail-closed trading boundary (there is no execution surface)."""

    model_config = ConfigDict(extra="forbid")

    mode: TradingMode = TradingMode.PUBLIC_MONITOR
    live_execution_enabled: Literal[False] = False
    submission_authorized: Literal[False] = False
    leverage: Literal[0] = 0
    require_explicit_arming: Literal[True] = True
    margin_enabled: Literal[False] = False
    futures_execution_enabled: Literal[False] = False
    withdrawals_enabled: Literal[False] = False
    transfers_enabled: Literal[False] = False
    execution_backend: Literal["disabled"] = "disabled"
    quote_asset: Literal["USDT"] = "USDT"
    maximum_mission_days: int = Field(default=7, gt=0, le=7)
    default_end_policy: MissionEndPolicy = MissionEndPolicy.MANAGE_TO_EXIT


class MissionPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_currency: Literal["TRY"] = "TRY"
    timezone: str = Field(default="Europe/Istanbul", min_length=1)
    daily_profit_stop_pct: float = Field(default=10.0, gt=0, le=100)
    daily_loss_stop_pct: float = Field(default=2.0, gt=0, le=100)
    maximum_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    maximum_conversion_age_s: int = Field(default=600, gt=0, le=86_400)


class NewsFeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    feed_url: str | None = Field(default=None, min_length=1)
    # Backwards-compatible fields for older config/test inputs.
    url: str | None = Field(default=None, min_length=1)
    source_name: str | None = Field(default=None, min_length=1)
    source_type: NewsSourceType = NewsSourceType.RSS
    source_group: SourceGroup = SourceGroup.GENERAL_NEWS
    official_domain: str | None = Field(default=None, min_length=1, max_length=253)
    availability: SourceAvailability = SourceAvailability.AVAILABLE
    priority: int = Field(default=100, ge=0, le=1000)
    reliability_tier: NewsReliabilityTier = NewsReliabilityTier.GENERAL
    reliability: float = Field(default=0.70, ge=0, le=1)
    language: str = Field(default="und", min_length=2, max_length=16)
    official: bool = False
    official_symbols: list[str] = Field(default_factory=list)
    enabled: bool = True
    timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    maximum_items: int | None = Field(default=None, gt=0, le=500)
    maximum_payload_bytes: int | None = Field(default=None, ge=1024, le=10_000_000)
    freshness_window_seconds: int = Field(default=86_400, gt=0, le=2_592_000)
    allowed_event_types: list[NewsEventType] = Field(default_factory=list)
    minimum_request_interval_seconds: float = Field(default=30.0, ge=0, le=86_400)
    maximum_redirects: int = Field(default=3, ge=0, le=10)
    backoff_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    backoff_max_seconds: float = Field(default=300.0, gt=0, le=86_400)
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    parser_version: str = Field(default="rss-atom-v1", min_length=1, max_length=64)
    terms_or_license_note: str = Field(
        default="Public publisher feed; retain source attribution and canonical URL.",
        min_length=1,
        max_length=1000,
    )

    @field_validator("url", "feed_url")
    @classmethod
    def _feed_url(cls, value: str | None) -> str | None:
        from urllib.parse import urlsplit

        if value is None:
            return None
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("news feed URL must be http/https")
        return value.strip()

    @field_validator("official_symbols")
    @classmethod
    def _official_symbols(cls, values: list[str]) -> list[str]:
        return sorted({value.strip().upper() for value in values if value.strip()})

    @model_validator(mode="after")
    def _complete_registry_record(self) -> NewsFeedConfig:
        selected_url = self.feed_url or self.url
        selected_name = self.name or self.source_name
        if selected_name is None:
            raise ValueError("news feed requires a name")
        if self.enabled and selected_url is None:
            raise ValueError("an enabled news feed requires feed_url")
        if self.enabled and self.availability is not SourceAvailability.AVAILABLE:
            raise ValueError("an enabled news feed must be AVAILABLE")
        if self.feed_url is not None and self.url is not None and self.feed_url != self.url:
            raise ValueError("feed_url and legacy url must not differ")
        if self.name is not None and self.source_name is not None and self.name != self.source_name:
            raise ValueError("name and legacy source_name must not differ")
        normalized = "".join(char if char.isalnum() else "_" for char in selected_name.casefold())
        self.feed_url = selected_url
        self.url = selected_url
        self.name = selected_name
        self.source_name = selected_name
        self.provider_id = self.provider_id or normalized.strip("_") or "rss_atom"
        if self.official_domain is None:
            if selected_url is None:
                raise ValueError("a registry record without a URL must carry official_domain")
            from urllib.parse import urlsplit

            self.official_domain = urlsplit(selected_url).hostname
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("news backoff_max_seconds must not be less than the base value")
        return self


class NewsIntelligenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    feeds: list[NewsFeedConfig] = Field(default_factory=list)
    timeout_s: float = Field(default=10.0, gt=0, le=60)
    maximum_response_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    maximum_items_per_feed: int = Field(default=100, gt=0, le=500)
    poll_interval_s: int = Field(default=300, ge=60, le=86_400)
    maximum_age_minutes: int = Field(default=1440, gt=0, le=10_080)
    future_tolerance_seconds: int = Field(default=120, ge=0, le=600)
    default_ttl_minutes: int = Field(default=360, gt=0, le=2880)
    minimum_reliability: float = Field(default=0.60, ge=0, le=1)
    verified_reliability: float = Field(default=0.80, ge=0, le=1)
    parser_quarantine_error_rate: float = Field(default=0.50, gt=0, le=1)
    minimum_critical_sources_healthy: int = Field(default=1, ge=1, le=20)
    raw_document_retention_hours: int = Field(default=168, ge=24, le=2160)
    tradingview_enabled: bool = False
    tradingview_ttl_seconds: int = Field(default=120, ge=10, le=600)
    tradingview_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)

    @model_validator(mode="after")
    def _reliability_order(self) -> NewsIntelligenceConfig:
        if self.verified_reliability < self.minimum_reliability:
            raise ValueError("verified_reliability must not be below minimum_reliability")
        enabled_urls = [feed.feed_url for feed in self.feeds if feed.enabled]
        if len(enabled_urls) != len(set(enabled_urls)):
            raise ValueError("news feed URLs must be unique")
        enabled_ids = [feed.provider_id for feed in self.feeds if feed.enabled]
        if len(enabled_ids) != len(set(enabled_ids)):
            raise ValueError("news provider_id values must be unique")
        return self


class IntradayPlannerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_confidence: float = Field(default=65.0, ge=0, le=100)
    minimum_news_reliability: float = Field(default=0.70, ge=0, le=1)
    maximum_manipulation_risk: float = Field(default=35.0, ge=0, le=100)
    maximum_holding_minutes: int = Field(default=120, ge=60, le=120)
    plan_validity_seconds: int = Field(default=300, gt=0, le=3600)
    reevaluation_cooldown_seconds: int = Field(default=300, gt=0, le=3600)
    meaningful_edge_change_bps: float = Field(default=20.0, ge=0)


class UniverseDiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    quote_asset: Literal["USDT"] = "USDT"
    broad_scan_interval_s: int = Field(default=900, ge=60)
    deep_watchlist_size: int = Field(default=10, gt=0, le=100)
    deep_candidate_pool_multiplier: int = Field(default=3, ge=1, le=10)
    liquidity_probe_notional: float = Field(default=1000.0, gt=0)
    watchlist_rotation_cooldown_s: int = Field(default=1800, ge=60)
    minimum_listing_age_days: int = Field(default=30, ge=0)
    reject_unknown_listing_age: bool = True
    minimum_history_bars: int = Field(default=100, ge=30)
    minimum_quote_volume_24h: float = Field(default=50_000_000.0, ge=0)
    maximum_spread_bps: float = Field(default=15.0, gt=0)
    minimum_depth_quote: float = Field(default=100_000.0, ge=0)
    maximum_slippage_bps: float = Field(default=20.0, gt=0)
    maximum_realized_volatility_pct: float = Field(default=250.0, gt=0)
    blacklist: list[str] = Field(default_factory=list)

    @field_validator("blacklist")
    @classmethod
    def _normalized_blacklist(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values]
        if any(not value for value in normalized):
            raise ValueError("universe blacklist must not contain empty values")
        return sorted(set(normalized))


class FeePolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conservative_maker_bps: float = Field(default=12.0, ge=0)
    conservative_taker_bps: float = Field(default=15.0, ge=0)
    minimum_net_edge_bps: float = Field(default=35.0, ge=0)
    model_uncertainty_buffer_bps: float = Field(default=20.0, ge=0)
    additional_execution_buffer_bps: float = Field(default=5.0, ge=0)
    schedule_max_age_s: int = Field(default=3600, gt=0)
    allow_conservative_config_fallback: bool = False
    assume_bnb_discount: Literal[False] = False


class PortfolioPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cash_reserve_pct: float = Field(default=25.0, ge=0, lt=100)
    risk_per_trade_pct: float = Field(default=0.25, gt=0, le=2)
    maximum_symbol_weight_pct: float = Field(default=20.0, gt=0, le=100)
    maximum_positions: int = Field(default=3, gt=0, le=20)
    liquidity_participation_pct: float = Field(default=1.0, gt=0, le=10)
    maximum_btc_correlation: float = Field(default=0.85, ge=0, le=1)
    maximum_daily_trades: int = Field(default=6, gt=0, le=100)
    same_symbol_cooldown_s: int = Field(default=900, ge=0, le=86_400)


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    maintenance_interval_s: int = Field(default=30, ge=30, le=3600)
    market_data_days: int = Field(default=365, gt=0)
    event_days: int = Field(default=90, gt=0)
    decision_days: int = Field(default=730, gt=0)
    raw_trade_hours: int = Field(default=24, gt=0, le=168)
    raw_depth_hours: int = Field(default=6, gt=0, le=72)
    raw_book_snapshot_hours: int = Field(default=24, gt=0, le=168)
    raw_ticker_hours: int = Field(default=24, gt=0, le=168)
    raw_news_document_hours: int = Field(default=168, ge=24, le=2160)
    rollup_bucket_s: Literal[60] = 60
    maintenance_batch_size: int = Field(default=1000, gt=0, le=10_000)
    maximum_database_mb: int = Field(default=2048, gt=0)
    minimum_free_disk_mb: int = Field(default=5120, ge=256)
    maximum_backlog_events: int = Field(default=1_000_000, gt=0)


class DiagnosticsConfig(BaseModel):
    """Opt-in long-run diagnostics and operational shadow-readiness thresholds.

    ``enabled`` is intentionally false in normal service operation.  The soak CLI may
    explicitly opt in without turning on execution, shadow positions or private data.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    metrics_mode: Literal["production", "diagnostic", "deep_tracing"] = "production"
    tracemalloc_frames: int = Field(default=10, ge=1, le=25)
    maximum_metric_samples: int = Field(default=4096, ge=128, le=100_000)
    maximum_stream_labels: int = Field(default=2048, ge=64, le=20_000)
    memory_warmup_seconds: int = Field(default=3600, ge=0, le=14_400)
    memory_minimum_samples: int = Field(default=12, ge=3, le=1000)
    memory_minimum_span_seconds: int = Field(default=7200, ge=60, le=14_400)
    python_heap_slope_mib_per_hour: float = Field(default=2.0, ge=0)
    private_bytes_slope_mib_per_hour: float = Field(default=5.0, ge=0)
    private_bytes_budget_mib: float = Field(default=512.0, gt=0)
    memory_acceptance_window_seconds: int = Field(default=7200, ge=1800, le=14_400)
    task_growth_per_hour: float = Field(default=1.0, ge=0)
    thread_growth_per_hour: float = Field(default=1.0, ge=0)
    handle_growth_per_hour: float = Field(default=2.0, ge=0)
    socket_growth_per_hour: float = Field(default=2.0, ge=0)
    queue_final_depth: Literal[0] = 0
    reconnect_burst_window_seconds: int = Field(default=60, ge=10, le=600)
    reconnect_burst_max: int = Field(default=3, ge=0, le=100)
    reconnect_per_worker_per_hour: float = Field(default=2.0, ge=0)
    minimum_median_connection_lifetime_s: float = Field(default=900.0, ge=0)
    api_minimum_success_rate: float = Field(default=0.999, ge=0, le=1)
    event_loop_lag_p95_ms: float = Field(default=1000.0, gt=0)
    event_latency_p95_ms: float = Field(default=5000.0, gt=0)


class RuntimeConfig(BaseModel):
    """Runtime "data" config (hot-reloadable)."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    exchanges: list[ExchangeConfig] = Field(default_factory=lambda: [ExchangeConfig()])
    market_types: list[MarketTypeName] = Field(default_factory=_default_market_types)
    timeframes: list[TimeframeName] = Field(default_factory=_default_timeframes)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    watchlist: list[WatchlistItem] = Field(default_factory=list)
    holdings: list[HoldingItem] = Field(default_factory=list)
    triggers: TriggerThresholds = Field(default_factory=TriggerThresholds)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    llm_roles: LLMRoleConfig = Field(default_factory=LLMRoleConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    shadow: ShadowConfig = Field(default_factory=ShadowConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    mission_policy: MissionPolicyConfig = Field(default_factory=MissionPolicyConfig)
    news_intelligence: NewsIntelligenceConfig = Field(default_factory=NewsIntelligenceConfig)
    intraday_planner: IntradayPlannerConfig = Field(default_factory=IntradayPlannerConfig)
    universe_discovery: UniverseDiscoveryConfig = Field(default_factory=UniverseDiscoveryConfig)
    fees: FeePolicyConfig = Field(default_factory=FeePolicyConfig)
    portfolio_policy: PortfolioPolicyConfig = Field(default_factory=PortfolioPolicyConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    outcome: OutcomeConfig = Field(default_factory=OutcomeConfig)
    macro: MacroConfig = Field(default_factory=MacroConfig)

    @model_validator(mode="after")
    def _safe_combinations(self) -> RuntimeConfig:
        if not any(exchange.enabled for exchange in self.exchanges):
            raise ValueError("at least one exchange must be enabled")
        if any(exchange.enabled and not exchange.public_data_only for exchange in self.exchanges):
            raise ValueError("an enabled exchange must be public_data_only in the current scope")
        if any(
            exchange.enabled and exchange.websocket_enabled and not exchange.rest_enabled
            for exchange in self.exchanges
        ):
            raise ValueError(
                "REST snapshots must be enabled for WebSocket order-book reconciliation"
            )
        if not self.market_types:
            raise ValueError("at least one market_type is required")
        if len(self.market_types) != len(set(self.market_types)):
            raise ValueError("market_types must be unique")
        if not self.timeframes:
            raise ValueError("at least one timeframe is required")
        if len(self.timeframes) != len(set(self.timeframes)):
            raise ValueError("timeframes must be unique")
        if self.trading.mode is TradingMode.SHADOW and not self.shadow.enabled:
            raise ValueError("SHADOW mode requires shadow.enabled")
        if self.trading.mode in {TradingMode.TESTNET, TradingMode.LIVE_SPOT}:
            raise ValueError(
                f"{self.trading.mode.value} is unavailable in this build because "
                "execution_backend=disabled"
            )
        if self.universe_discovery.enabled and self.universe.strict:
            raise ValueError(
                "dynamic universe discovery and a strict fixed universe cannot both be enabled"
            )
        if self.universe.strict:
            allowed = set(self.universe.symbols)
            outside = [
                item.symbol.upper()
                for item in self.watchlist
                if item.asset_class is AssetClass.CRYPTO and item.symbol.upper() not in allowed
            ]
            if outside:
                raise ValueError(f"crypto symbols outside the strict universe: {outside}")
        return self

    def by_tier(self, tier: WatchTier) -> list[WatchlistItem]:
        """Instruments in the given tier (for the watch loop)."""
        return [item for item in self.watchlist if item.tier == tier]
