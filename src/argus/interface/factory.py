"""Watch pipeline assembly factories — build the live WatchLoop + Scheduler from settings.

Wires the live adapters (ccxt/borsapy), the analyst (LLM), the Telegram notifier, the
dispatch policy (prioritization + quiet hours) and the event bus according to ``Settings``
+ ``config/config.yaml``. Switching to a local LLM only changes
:func:`~argus.brain.factory.build_analyst`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argus.interface.crypto_runtime import CryptoSurveillanceCycle, CryptoWebSocketService
    from argus.interface.news_runtime import NewsIntelligenceCycle

from argus.brain.factory import build_analyst
from argus.config.loader import ConfigLoader
from argus.config.models import AlertConfig, QualityConfig, RuntimeConfig, TriggerThresholds
from argus.config.settings import Settings, get_settings
from argus.core.contracts import Disclosure, NewsItem, OHLCVSeries
from argus.core.enums import AssetClass, Severity, Timeframe
from argus.decision.aggregator import build_aggregator
from argus.ingestion.base import MarketDataAdapter
from argus.interface.cooldown import CooldownTracker
from argus.interface.dispatcher import AlertDispatcher
from argus.interface.notifier import TelegramNotifier
from argus.interface.prioritization import AlertPolicy
from argus.interface.quiet_hours import QuietHours
from argus.interface.scheduler import WatchScheduler
from argus.interface.telegram_sender import TelegramHttpSender
from argus.interface.watch_loop import WatchLoop
from argus.observability.logging import get_logger
from argus.observability.status import SystemStatus
from argus.platform.adjust import apply_actions
from argus.platform.event_bus import EventBus, InMemoryEventBus, RedisEventBus
from argus.platform.quality import QualityDecision, QualityGate
from argus.platform.triggers import TriggerConfig, TriggerEngine
from argus.storage.base import AnalysisJournal
from argus.storage.sqlite import SqliteStore

_log = get_logger("argus.interface.factory")
_SQLITE_PREFIX = "sqlite:///"


def build_store(settings: Settings) -> SqliteStore | None:
    """Build the durable store when ``database_url`` is SQLite (journal/cooldown/OHLCV)."""
    url = settings.database_url
    if not url.startswith(_SQLITE_PREFIX):
        return None
    target = url[len(_SQLITE_PREFIX) :]
    if target not in (":memory:", ""):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(target or ":memory:")


def build_adjuster(store: SqliteStore | None) -> Callable[[OHLCVSeries], OHLCVSeries] | None:
    """Build the read-time adjustment function from the store's corporate actions.

    Without a store returns None (no adjustment — raw view). No-op for symbols with no
    actions.
    """
    if store is None:
        return None

    def _adjust(series: OHLCVSeries) -> OHLCVSeries:
        actions = store.load_corporate_actions(series.symbol)
        return apply_actions(series, actions) if actions else series

    return _adjust


def _build_journal(settings: Settings) -> AnalysisJournal | None:
    """Build the decision journal when ``database_url`` is SQLite (only backend so far)."""
    return build_store(settings)


def _make_series_loader(
    store: SqliteStore | None, limit: int
) -> Callable[[str, Timeframe], OHLCVSeries | None] | None:
    """Timeframe-aware series reader from the store (HTF context); None without a store."""
    if store is None:
        return None

    def _load(symbol: str, tf: Timeframe) -> OHLCVSeries | None:
        return store.load_series(symbol, tf, limit=limit)

    return _load


def _make_macro_loader(
    store: SqliteStore | None, stale_after_h: int
) -> Callable[[], dict[str, float] | None] | None:
    """Fresh macro trend reader from the store; None without a store (agent → NO_DATA)."""
    if store is None:
        return None

    def _load() -> dict[str, float] | None:
        return store.load_macro_trends(max_age_h=float(stale_after_h))

    return _load


def _make_positioning_loader(
    store: SqliteStore | None, stale_after_h: int
) -> Callable[[], dict[str, float] | None] | None:
    """Fresh positioning LEVELS from the store (Fear & Greed); None without a store."""
    if store is None:
        return None

    def _load() -> dict[str, float] | None:
        return store.load_macro_values(max_age_h=float(stale_after_h))

    return _load


# News context freshness window: older news does not enter the analysis (no sentiment is
# derived from stale news — in the spirit of the Data Quality Gate).
_NEWS_FRESH_H = 48.0


def _make_news_loader(
    store: SqliteStore | None,
) -> Callable[[str], tuple[tuple[NewsItem, ...], tuple[Disclosure, ...]]] | None:
    """Convert fresh archived news + KAP disclosures for a symbol into agent contracts."""
    if store is None:
        return None

    def _load(symbol: str) -> tuple[tuple[NewsItem, ...], tuple[Disclosure, ...]]:
        from argus.core.schemas import Provenance, utcnow

        now = utcnow()
        news: list[NewsItem] = []
        disclosures: list[Disclosure] = []
        for r in store.recent_news(limit=100):
            if (now - r.published_at).total_seconds() > _NEWS_FRESH_H * 3600:
                continue
            if r.symbol is not None and r.symbol != symbol:
                continue  # another symbol's news is excluded (no symbol = general, included)
            prov = Provenance(source=r.source, timestamp=r.published_at, confidence=0.6)
            if r.kind == "disclosure" and r.symbol:
                disclosures.append(
                    Disclosure(
                        disclosure_id=r.ext_id,
                        symbol=r.symbol,
                        title=r.title,
                        published_at=r.published_at,
                        url=r.url,
                        provenance=prov,
                    )
                )
            else:
                news.append(
                    NewsItem(
                        id=r.ext_id,
                        title=r.title,
                        published_at=r.published_at,
                        url=r.url,
                        symbols=(r.symbol,) if r.symbol else (),
                        sentiment=r.sentiment,
                        provenance=prov,
                    )
                )
        return tuple(news), tuple(disclosures)

    return _load


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def to_trigger_config(thresholds: TriggerThresholds) -> TriggerConfig:
    """``TriggerThresholds`` (data) → :class:`TriggerConfig` (behaviour)."""
    return TriggerConfig(
        rsi_period=thresholds.rsi_period,
        rsi_overbought=thresholds.rsi_overbought,
        rsi_oversold=thresholds.rsi_oversold,
        price_move_pct=thresholds.price_move_pct,
        atr_period=thresholds.atr_period,
        atr_spike_mult=thresholds.atr_spike_mult,
        ema_fast=thresholds.ema_fast,
        ema_slow=thresholds.ema_slow,
    )


def to_quality_gate(quality: QualityConfig) -> QualityGate:
    """``QualityConfig`` (data) → :class:`QualityGate`."""
    return QualityGate(
        stale_factor=quality.stale_factor,
        max_jump_pct=quality.max_jump_pct,
        gap_factor=quality.gap_factor,
        min_bars=quality.min_bars,
        max_clock_skew_s=quality.max_clock_skew_s,
        empty_action=QualityDecision(quality.empty_action),
        stale_action=QualityDecision(quality.stale_action),
        duplicate_action=QualityDecision(quality.duplicate_action),
        gap_action=QualityDecision(quality.gap_action),
        disorder_action=QualityDecision(quality.disorder_action),
        outlier_action=QualityDecision(quality.outlier_action),
        future_action=QualityDecision(quality.future_action),
        insufficient_history_action=QualityDecision(quality.insufficient_history_action),
    )


def to_alert_policy(alerts: AlertConfig) -> AlertPolicy:
    """``AlertConfig`` (data) → :class:`AlertPolicy` (behaviour)."""
    quiet = None
    if alerts.quiet_start and alerts.quiet_end:
        quiet = QuietHours(
            _parse_hhmm(alerts.quiet_start), _parse_hhmm(alerts.quiet_end), tz=alerts.quiet_tz
        )
    return AlertPolicy(
        min_instant_severity=Severity(alerts.min_instant_severity),
        quiet_hours=quiet,
        critical_overrides_quiet=alerts.critical_overrides_quiet,
    )


def build_watch_loop(
    settings: Settings | None = None,
    config: RuntimeConfig | None = None,
    *,
    bus: EventBus | None = None,
) -> WatchLoop:
    """Build the WatchLoop with live adapters, analyst, Telegram notifier and dispatch policy."""
    cfg = settings or get_settings()
    runtime_config = config or ConfigLoader().get()
    if cfg.telegram_bot_token is None:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set (required for notifications)")
    if not cfg.authorized_chat_id_set:
        raise ValueError("ARGUS_AUTHORIZED_CHAT_IDS is empty (an authorized chat is required)")

    # Adapters are built by lazy factories (ccxt/borsapy are imported here).
    from argus.ingestion.borsapy_provider import bist_adapter
    from argus.ingestion.crypto import binance_adapter

    bist = bist_adapter()
    adapters: dict[AssetClass, MarketDataAdapter] = {
        AssetClass.CRYPTO: binance_adapter(),
        AssetClass.BIST: bist,
        # Metals: gold/silver ETFs traded on BIST (GLDTR/GMSTR) → the same borsapy adapter.
        AssetClass.METAL: bist,
    }
    # Global (secondary/context) → OpenBB. Skip gracefully when openbb is not installed.
    try:
        from argus.ingestion.openbb import openbb_adapter

        global_adapter = openbb_adapter()
        adapters[AssetClass.EQUITY] = global_adapter
        adapters[AssetClass.FX] = global_adapter
    except Exception as exc:  # openbb missing / import error → no global adapter
        _log.info("openbb-adapter-skipped", reason=str(exc))
    sender = TelegramHttpSender(cfg.telegram_bot_token.get_secret_value())
    notifier = TelegramNotifier(sender, cfg.authorized_chat_id_set)
    dispatcher = AlertDispatcher(notifier, policy=to_alert_policy(runtime_config.alerts))

    # One store: decision journal and cooldown persistence (idempotent recovery).
    store = _build_journal(cfg)
    cooldown_store = store if isinstance(store, SqliteStore) else None

    loop_cfg = runtime_config.loop
    return WatchLoop(
        adapters=adapters,
        # Cost is written to the ledger (daily cap shared across processes + /api/cost).
        analyst=build_analyst(cfg, cost_store=cooldown_store),
        notifier=notifier,
        bus=bus or InMemoryEventBus(),
        quality=to_quality_gate(runtime_config.quality),
        triggers=TriggerEngine(to_trigger_config(runtime_config.triggers)),
        cooldown=CooldownTracker(loop_cfg.cooldown_s, store=cooldown_store),
        timeframe=Timeframe(loop_cfg.timeframe),
        limit=loop_cfg.limit,
        min_severity=Severity(loop_cfg.min_severity),
        fetch_retries=loop_cfg.fetch_retries,
        circuit_threshold=loop_cfg.circuit_threshold,
        circuit_reset_s=loop_cfg.circuit_reset_s,
        dispatcher=dispatcher,
        # Live confluence: deterministic agents + the LLM-assisted technical agent (when a
        # real provider is connected). In demo mode build_aggregator skips the LLM agent.
        aggregator=build_aggregator(cfg, cost_store=cooldown_store),
        journal=store,
        # With a SqliteStore, write OHLCV to the PIT store → real dashboard data.
        ohlcv_store=cooldown_store,
        # Proposal journal: confluence proposals are measured too (self-monitoring).
        proposal_store=cooldown_store,
        # The store stays raw; triggers/analysis see the corporate-action-adjusted view.
        adjuster=build_adjuster(cooldown_store),
        # HTF context — the symbol's other timeframe series are loaded from the store.
        series_loader=_make_series_loader(cooldown_store, loop_cfg.limit),
        # Fresh EVDS macro trends fill the context (stale/empty → agent NO_DATA).
        macro_loader=_make_macro_loader(cooldown_store, runtime_config.macro.stale_after_h),
        # Fresh archived news + KAP → NewsSentimentAgent; F&G level → PositioningAgent.
        news_loader=_make_news_loader(cooldown_store),
        positioning_loader=_make_positioning_loader(
            cooldown_store, runtime_config.macro.stale_after_h
        ),
        health_store=cooldown_store,
    )


def build_scheduler(
    settings: Settings | None = None, config: RuntimeConfig | None = None
) -> WatchScheduler:
    """Build a live watch scheduler from settings + ``config/config.yaml``.

    The dispatcher and the daily briefing (digest flush) job are attached as well.
    """
    runtime_config = config or ConfigLoader().get()
    loop = build_watch_loop(settings, runtime_config)
    crypto_cycle = build_crypto_cycle(settings, runtime_config)
    news_cycle = build_news_cycle(settings, runtime_config)
    return WatchScheduler(
        loop,
        runtime_config,
        dispatcher=loop.dispatcher,
        digest_interval_s=runtime_config.alerts.briefing_interval_s,
        crypto_cycle=crypto_cycle,
        crypto_interval_s=runtime_config.ingestion.reconcile_interval_s,
        news_cycle=news_cycle,
        news_interval_s=runtime_config.news_intelligence.poll_interval_s,
    )


def build_news_cycle(
    settings: Settings | None = None, config: RuntimeConfig | None = None
) -> NewsIntelligenceCycle:
    """News side job that only uses the configured public RSS/Atom sources."""
    from argus.interface.news_runtime import NewsIntelligenceCycle

    cfg = settings or get_settings()
    runtime_config = config or ConfigLoader().get()
    store = build_store(cfg)
    if store is None:
        raise ValueError("the news intelligence cycle requires a SQLite store")
    return NewsIntelligenceCycle(store, lambda: runtime_config, owns_store=True)


def build_crypto_cycle(
    settings: Settings | None = None, config: RuntimeConfig | None = None
) -> CryptoSurveillanceCycle | None:
    """Build the crypto side job with the public Binance REST provider + its own SQLite link.

    The client accepts no API key. WebSocket consumption can be started separately through
    the same coordinator; the default test suite never calls this factory or the network.
    """
    from argus.ingestion.binance import BinanceRESTProvider, ReconnectPolicy
    from argus.interface.crypto_runtime import CryptoSurveillanceCycle
    from argus.interface.universe_runtime import DynamicUniverseCycle

    cfg = settings or get_settings()
    runtime_config = config or ConfigLoader().get()
    exchange = next(
        (
            item
            for item in runtime_config.exchanges
            if item.enabled and item.name.lower() == "binance" and item.rest_enabled
        ),
        None,
    )
    if exchange is None:
        return None
    if not exchange.public_data_only:
        raise ValueError("crypto ingestion currently accepts public_data_only exchanges only")
    store = build_store(cfg)
    if store is None:
        raise ValueError("the crypto cycle requires a SQLite store")
    ingestion = runtime_config.ingestion
    provider = BinanceRESTProvider(
        max_clock_skew_ms=round(runtime_config.quality.max_clock_skew_s * 1000),
        requests_per_second=ingestion.rate_limit_per_s,
        request_attempts=runtime_config.loop.fetch_retries,
        retry_policy=ReconnectPolicy(
            base_s=ingestion.reconnect_base_s,
            max_s=ingestion.reconnect_max_s,
            jitter_ratio=ingestion.reconnect_jitter_ratio,
        ),
    )
    cycle = CryptoSurveillanceCycle(provider, store, runtime_config, owns_store=True)
    if runtime_config.universe_discovery.enabled:
        cycle.universe_cycle = DynamicUniverseCycle(provider, store, runtime_config)
    return cycle


def build_crypto_stream(
    settings: Settings | None = None, config: RuntimeConfig | None = None
) -> CryptoWebSocketService | None:
    """Build the long-lived public spot + perpetual WebSocket service."""
    from argus.ingestion.binance import (
        BinanceWebSocketProvider,
        ReconnectPolicy,
        ensure_websocket_runtime,
    )
    from argus.interface.crypto_runtime import CryptoWebSocketService

    runtime_config = config or ConfigLoader().get()
    exchange = next(
        (
            item
            for item in runtime_config.exchanges
            if item.enabled and item.name.lower() == "binance" and item.websocket_enabled
        ),
        None,
    )
    if exchange is None:
        return None
    ensure_websocket_runtime()
    cycle = build_crypto_cycle(settings, runtime_config)
    if cycle is None:
        return None
    ingestion = runtime_config.ingestion
    websocket = BinanceWebSocketProvider(
        policy=ReconnectPolicy(
            base_s=ingestion.reconnect_base_s,
            max_s=ingestion.reconnect_max_s,
            jitter_ratio=ingestion.reconnect_jitter_ratio,
        ),
        disconnect_after_s=ingestion.websocket.read_timeout_s,
        compression_enabled=ingestion.websocket.compression.enabled,
        max_message_size=ingestion.websocket.max_message_size,
        max_queue_messages=ingestion.websocket.max_queue_messages,
        ping_interval_s=ingestion.websocket.ping_interval_s,
        ping_timeout_s=ingestion.websocket.ping_timeout_s,
        open_timeout_s=ingestion.websocket.open_timeout_s,
        close_timeout_s=ingestion.websocket.close_timeout_s,
        stable_connection_reset_s=ingestion.websocket.stable_connection_reset_s,
        metrics=cycle.stream_metrics,
    )
    return CryptoWebSocketService(cycle, websocket)


def build_consumer(settings: Settings | None = None) -> RedisEventBus:
    """Build the Redis push consumer: forwards instant events (news/KAP/trigger) to Telegram.

    The **push** half of the watch loop: irregular events (KAP disclosure, news, whale) are
    published to Redis from separate processes; this consumer listens and relays them to
    authorized chats. Run with ``python -m argus --consumer`` (blocking loop).
    """
    from argus.core.events import Event, EventType

    cfg = settings or get_settings()
    if cfg.telegram_bot_token is None or not cfg.authorized_chat_id_set:
        raise ValueError(
            "Telegram token + an authorized chat are required (for push notifications)"
        )

    sender = TelegramHttpSender(cfg.telegram_bot_token.get_secret_value())
    notifier = TelegramNotifier(sender, cfg.authorized_chat_id_set)
    bus = RedisEventBus(cfg.redis_url)

    def notify(event: Event) -> None:
        symbol = str(event.payload.get("symbol", "—"))
        title = str(event.payload.get("title") or event.payload.get("message") or "")
        notifier.broadcast(f"📣 [{event.type.value}] {symbol} — {title}\n(source: {event.source})")

    # Push events needing prompt attention (the ORDER_REQUESTED execution event is excluded).
    for et in (EventType.DISCLOSURE_KAP, EventType.NEWS_RECEIVED, EventType.TRIGGER_FIRED):
        bus.subscribe(et, notify)
    return bus


def build_status(
    settings: Settings | None = None, *, store: SqliteStore | None = None
) -> SystemStatus:
    """Collect a status summary from settings + the durable journal (/status, --status)."""
    from argus import __version__
    from argus.core.schemas import utcnow

    cfg = settings or get_settings()
    hit_correct: int | None = None
    hit_total: int | None = None
    budget_remaining: float | None = None
    journal = store or _build_journal(cfg)
    owns_journal = store is None
    if isinstance(journal, SqliteStore):
        hr = journal.hit_rate()
        hit_correct, hit_total = hr.correct, hr.total
        # Cost ledger: today's real spend (all processes) → an honest remaining budget.
        rows = journal.llm_cost_for_day(utcnow().date().isoformat())
        spent = sum(t + s for t, s in rows.values())
        budget_remaining = max(cfg.llm_daily_budget_usd - spent, 0.0)
    try:
        return SystemStatus(
            version=__version__,
            env=cfg.env,
            authorized_count=len(cfg.authorized_chat_id_set),
            budget_remaining_usd=budget_remaining,
            budget_total_usd=cfg.llm_daily_budget_usd,
            hit_correct=hit_correct,
            hit_total=hit_total,
        )
    finally:
        if owns_journal and isinstance(journal, SqliteStore):
            journal.close()
