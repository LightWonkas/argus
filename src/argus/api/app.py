"""Dashboard FastAPI application (cockpit API + auth).

``create_app(...)`` returns a FastAPI app; providers (status/health/users) are **injected**
→ tests run without network (``TestClient`` + fake providers). FastAPI is imported lazily,
only in this module (the existing DI/lazy convention). Read-only, auth required, no secrets
leak.

Auth: ``POST /api/login`` verifies user+password → writes a signed session cookie
(``argus_session``); protected endpoints require that cookie. An empty user table = auth
disabled (dev only).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, NoReturn

from argus.api.chat_models import ChatRequest, ChatResponse, ChatTurnView
from argus.api.intelligence_models import TradingViewWebhookAck, TradingViewWebhookRequest
from argus.api.models import (
    AlertItem,
    AnalysisDetail,
    AnalysisListItem,
    CheckResultModel,
    CostResponse,
    CryptoArtifactView,
    CryptoDashboardResponse,
    FeedbackAck,
    FeedbackRequest,
    FeedbackSummaryResponse,
    HealthResponse,
    HitRateResponse,
    LoginRequest,
    MeasurementView,
    NewsResponseItem,
    OHLCVResponse,
    PortfolioResponse,
    ProposalResponse,
    QuoteItem,
    SparkBatchResponse,
    SparkItem,
    StatusResponse,
)
from argus.api.news_router_models import NewsRouteRequest, NewsRouteResponse
from argus.api.portfolio import PortfolioSummary
from argus.api.quotes import QuoteRow
from argus.api.security import (
    SessionConfig,
    authenticate,
    issue_session,
    parse_users,
    verify_session,
)
from argus.api.settings_models import (
    DataSettingsUpdate,
    LLMSettingsUpdate,
    SettingsView,
    SettingsWriteResult,
    TelegramSettingsUpdate,
    TriggerUpdate,
    WatchlistUpdate,
)
from argus.api.trading_models import (
    KillSwitchRequest,
    KillSwitchResponse,
    MissionArmRequest,
    MissionCreateRequest,
    MissionDetailResponse,
    MissionHaltRequest,
    MissionListResponse,
    MissionMutationResponse,
    TradingAccountResponse,
    TradingCostsResponse,
    TradingFillsResponse,
    TradingHealthResponse,
    TradingNewsResponse,
    TradingOrdersResponse,
    TradingPerformanceResponse,
    TradingPlansResponse,
    TradingPositionsResponse,
    TradingResearchResponse,
    TradingRiskResponse,
    TradingStatusResponse,
    TradingStorageResponse,
    TradingUniverseResponse,
    TradingWatchlistResponse,
)
from argus.config.models import RuntimeConfig
from argus.core.contracts import Proposal
from argus.core.errors import ConcurrentStateError, RiskRejectedError
from argus.core.intelligence import ExternalTechnicalSignal
from argus.core.schemas import DataStatus, utcnow
from argus.observability.health import HealthRegistry, default_registry
from argus.observability.status import SystemStatus
from argus.storage.base import ChatStore, FeedbackReader, FeedbackWriter, JournalReader

if TYPE_CHECKING:
    from fastapi import FastAPI

    from argus.brain.chat import ChatEngine
    from argus.brain.news_router import NewsRouter
    from argus.config.settings import Settings
    from argus.core.contracts import OHLCVSeries
    from argus.interface.trading_control import TradingControlService
    from argus.storage.sqlite import SqliteStore

SESSION_COOKIE = "argus_session"
CSRF_COOKIE = "argus_csrf"

StatusProvider = Callable[[], SystemStatus]
HealthProvider = Callable[[], HealthRegistry]
JournalProvider = Callable[[], JournalReader | None]
QuotesProvider = Callable[[], list[QuoteRow]]
PortfolioProvider = Callable[[], PortfolioSummary]
SettingsReader = Callable[[], SettingsView]
EnvWriter = Callable[[dict[str, str]], None]
ConfigWriter = Callable[[RuntimeConfig], None]
ConfigReader = Callable[[], RuntimeConfig]
ChatProvider = Callable[[], "ChatEngine"]
ChatStoreProvider = Callable[[], ChatStore | None]
NewsRouterProvider = Callable[[], "NewsRouter"]
FeedbackProvider = Callable[[], FeedbackReader | None]
FeedbackWriterProvider = Callable[[], FeedbackWriter | None]
CostProvider = Callable[[], CostResponse | None]
ProposalProvider = Callable[[str], Proposal | None]
CryptoDashboardProvider = Callable[[str], CryptoDashboardResponse]
TradingControlProvider = Callable[[], "TradingControlService"]
IntelligenceStoreProvider = Callable[[], "SqliteStore | None"]
# Returns the candle array of a known (config-defined) symbol; None otherwise.
# The second argument is the timeframe (None → config default); undefined symbol+TF → None (404).
OHLCVReader = Callable[[str, "str | None"], "OHLCVSeries | None"]


def create_app(
    *,
    status_provider: StatusProvider | None = None,
    health_provider: HealthProvider | None = None,
    journal_provider: JournalProvider | None = None,
    quotes_provider: QuotesProvider | None = None,
    portfolio_provider: PortfolioProvider | None = None,
    settings_reader: SettingsReader | None = None,
    env_writer: EnvWriter | None = None,
    config_writer: ConfigWriter | None = None,
    config_reader: ConfigReader | None = None,
    chat_provider: ChatProvider | None = None,
    chat_store_provider: ChatStoreProvider | None = None,
    news_router_provider: NewsRouterProvider | None = None,
    feedback_provider: FeedbackProvider | None = None,
    feedback_writer_provider: FeedbackWriterProvider | None = None,
    cost_provider: CostProvider | None = None,
    proposal_provider: ProposalProvider | None = None,
    crypto_dashboard_provider: CryptoDashboardProvider | None = None,
    trading_control_provider: TradingControlProvider | None = None,
    intelligence_store_provider: IntelligenceStoreProvider | None = None,
    tradingview_webhook_secret: str | None = None,
    ohlcv_reader: OHLCVReader | None = None,
    users: dict[str, str] | None = None,
    session: SessionConfig | None = None,
    secure_cookie: bool = True,
    cors_origins: tuple[str, ...] = (),
    trading_local_control: bool = False,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Builds the Dashboard API app. Providers can be injected (for tests).

    Args:
        status_provider: Callable producing ``SystemStatus`` (default
            ``factory.build_status``).
        health_provider: Callable producing ``HealthRegistry`` (default ``default_registry``).
        users: ``{name: scrypt_hash}`` table. Empty/None → auth disabled (dev only).
        session: Session signing configuration (default: random per process).
        secure_cookie: Cookie ``Secure`` flag (True for prod/TLS; False in tests).
        cors_origins: Allowed origins (e.g. the dev frontend ``http://localhost:3000``).
            Empty means CORS is off (same-origin serving). **No wildcard** is used because of
            the cookie — an exact origin list + ``allow_credentials=True``.
    """
    from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, status
    from fastapi.responses import JSONResponse

    status_fn = status_provider or _default_status_provider
    health_fn = health_provider or default_registry
    journal_fn = journal_provider or _default_journal_provider
    quotes_fn = quotes_provider or _default_quotes_provider
    portfolio_fn = portfolio_provider or _default_portfolio_provider
    settings_reader_fn = settings_reader or _default_settings_reader
    env_writer_fn = env_writer or _default_env_writer
    config_writer_fn = config_writer or _default_config_writer
    config_reader_fn = config_reader or _default_config_reader
    chat_fn = chat_provider or _default_chat_provider
    chat_store_fn = chat_store_provider or _default_chat_store_provider
    news_router_fn = news_router_provider or _default_news_router_provider
    feedback_fn = feedback_provider or _default_feedback_provider
    feedback_writer_fn = feedback_writer_provider or _default_feedback_writer_provider
    cost_fn = cost_provider or _default_cost_provider
    proposal_fn = proposal_provider
    crypto_dashboard_fn = crypto_dashboard_provider or (
        lambda symbol: CryptoDashboardResponse(
            symbol=symbol.upper(),
            data_status="pending",
            missing_data=(
                "spot",
                "perpetual",
                "funding",
                "open_interest",
                "basis",
                "order_book",
                "quality",
                "features",
                "regime",
                "experts",
                "trade_candidate",
                "guard",
                "backtest",
            ),
        )
    )
    ohlcv_reader_fn = ohlcv_reader or _default_ohlcv_reader
    trading_control_fn = trading_control_provider
    intelligence_store_fn = intelligence_store_provider
    user_table = users or {}
    auth_required = bool(user_table)
    session_cfg = session or SessionConfig.from_secret("")
    webhook_attempts: deque[float] = deque()
    webhook_rate_lock = threading.Lock()

    app = FastAPI(
        title="Argus Dashboard API",
        version="0.1.0",
        description="Argus web cockpit backend. No live order execution. Not financial advice.",
        lifespan=lifespan,
    )

    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=True,  # so the cookie (argus_session) is sent
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-Argus-CSRF"],
        )

    def require_auth(argus_session: str | None = Cookie(default=None)) -> str:
        """401 without a valid session. Returns 'dev' when auth is disabled (no users)."""
        if not auth_required:
            return "dev"
        if argus_session and (user := verify_session(session_cfg, argus_session)) is not None:
            return user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session required — /api/login",
        )

    def require_trading_mutation(
        user: str = Depends(require_auth),
        argus_csrf: str | None = Cookie(default=None, alias=CSRF_COOKIE),
        x_argus_csrf: str | None = None,
    ) -> str:
        """Trading mutations require auth + double-submit CSRF + a local bind."""
        # Using a Header() annotation here would complicate resolution under future
        # annotations/lazy imports; the route wrapper below binds it with an explicit Header.
        if not auth_required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Dashboard auth must be configured for trading control",
            )
        if not trading_local_control:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Trading mutations are only available on a local-control bind",
            )
        if not argus_csrf or not x_argus_csrf or not hmac.compare_digest(argus_csrf, x_argus_csrf):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Valid CSRF verification required",
            )
        return user

    def trading_mutation_user(
        user: str = Depends(require_auth),
        argus_csrf: str | None = Cookie(default=None, alias=CSRF_COOKIE),
        x_argus_csrf: str | None = Header(default=None, alias="X-Argus-CSRF"),
    ) -> str:
        return require_trading_mutation(user, argus_csrf, x_argus_csrf)

    @app.post("/api/login", response_model=None)
    def login(body: LoginRequest) -> JSONResponse:
        # NOTE: with `from __future__ import annotations` + the lazy FastAPI import, injecting
        # a parameter type (Response) breaks annotation resolution, so the cookie is set on the
        # returned response instead.
        if not auth_required:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Auth is not configured (ARGUS_DASHBOARD_USERS is empty)",
            )
        if not authenticate(user_table, body.username, body.password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
            )
        token = issue_session(session_cfg, body.username)
        csrf_token = secrets.token_urlsafe(32)
        response = JSONResponse({"user": body.username})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            max_age=session_cfg.ttl_seconds,
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            httponly=False,
            secure=secure_cookie,
            samesite="strict",
            max_age=session_cfg.ttl_seconds,
        )
        return response

    @app.post("/api/logout", response_model=None)
    def logout() -> JSONResponse:
        response = JSONResponse({"status": "logged out"})
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return response

    @app.get("/api/health")
    def health(_: str = Depends(require_auth)) -> HealthResponse:
        registry = health_fn()
        results = registry.run()
        return HealthResponse(
            state=registry.aggregate(),
            checks=tuple(CheckResultModel.from_result(r) for r in results),
        )

    @app.get("/api/status")
    def status_endpoint(_: str = Depends(require_auth)) -> StatusResponse:
        return StatusResponse.from_status(status_fn())

    def _journal() -> JournalReader:
        reader = journal_fn()
        if reader is None:
            # Journal not connected (e.g. a non-SQLite backend) → an honest 503.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Analysis journal not connected — DATA PENDING",
            )
        return reader

    @app.get("/api/analyses")
    def analyses(
        symbol: str | None = None,
        limit: int = 20,
        _: str = Depends(require_auth),
    ) -> list[AnalysisListItem]:
        limit = max(1, min(limit, 200))
        rows = _journal().recent_analyses(symbol=symbol, limit=limit)
        return [AnalysisListItem.from_stored(r) for r in rows]

    @app.get("/api/analyses/{analysis_id}")
    def analysis_detail(
        analysis_id: int,
        _: str = Depends(require_auth),
    ) -> AnalysisDetail:
        # No dedicated single-record query; search the last N (enough for cockpit use).
        for r in _journal().recent_analyses(limit=200):
            if r.id == analysis_id:
                return AnalysisDetail.from_stored(r)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Analysis not found: {analysis_id}"
        )

    @app.get("/api/hitrate")
    def hitrate(
        symbol: str | None = None,
        _: str = Depends(require_auth),
    ) -> HitRateResponse:
        # v2: the v1 fields are kept (close×system, symbol-filtered); series + baselines +
        # the low-sample flag are added (v2 is an aggregate, with no per-symbol breakdown).
        journal = _journal()
        v1 = journal.hit_rate(symbol=symbol)
        try:
            min_n = config_reader_fn().outcome.min_n_warn
        except Exception:  # schema default if config is unreadable
            min_n = 30
        return HitRateResponse.from_buckets(v1, journal.hit_rate_v2(), min_n=min_n)

    @app.get("/api/alerts")
    def alerts(
        limit: int = 50,
        severity: str | None = None,
        _: str = Depends(require_auth),
    ) -> list[AlertItem]:
        limit = max(1, min(limit, 200))
        records = _journal().recent_alerts(limit=limit)
        if severity:
            records = [r for r in records if r.severity == severity]
        return [
            AlertItem(
                analysis_id=r.analysis_id,
                created_at=r.created_at,
                symbol=r.symbol,
                severity=r.severity,
                kind=r.kind,
                message=r.message,
            )
            for r in records
        ]

    @app.get("/api/quotes")
    def quotes(_: str = Depends(require_auth)) -> list[QuoteItem]:
        return [QuoteItem.from_row(r) for r in quotes_fn()]

    @app.get("/api/ohlcv/{symbol}")
    def ohlcv(symbol: str, tf: str | None = None, _: str = Depends(require_auth)) -> OHLCVResponse:
        # ohlcv_reader only reads symbol+TF pairs DEFINED in config (others → None → 404).
        series = ohlcv_reader_fn(symbol, tf)
        if series is None or not series.bars:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No OHLCV: {symbol}"
                    + (f" tf={tf}" if tf else "")
                    + " (not defined in config or DATA PENDING)"
                ),
            )
        return OHLCVResponse.from_series(series)

    @app.get("/api/ohlcv-batch")
    def ohlcv_batch(
        symbols: str,
        n: int = 40,
        tf: str | None = None,
        _: str = Depends(require_auth),
    ) -> SparkBatchResponse:
        """Last ``n`` closes of several symbols (sparkline) — in one request (not N calls).

        ``symbols``: comma-separated (e.g. ``BTCUSDT,XU100``). Only symbols defined in config
        that have data are returned; undefined/empty symbols are skipped silently (a partial
        response, not a 404 — the watchlist table can still draw the remaining sparklines).
        Limit: at most 50 symbols.
        """
        n = max(1, min(n, 500))
        wanted = [s.strip() for s in symbols.split(",") if s.strip()][:50]
        items: list[SparkItem] = []
        for sym in wanted:
            series = ohlcv_reader_fn(sym, tf)
            if series is None or not series.bars:
                continue  # undefined/no data → skip (partial response; no made-up zero)
            closes = tuple(float(b.close) for b in series.bars[-n:])
            items.append(SparkItem(symbol=sym, closes=closes))
        return SparkBatchResponse(items=tuple(items))

    @app.get("/api/proposal/{symbol}")
    def proposal(symbol: str, _: str = Depends(require_auth)) -> ProposalResponse:
        # In production the single source of truth is the proposal recorded by the watch
        # pipeline. In legacy/test usage without an injected provider, a backward-compatible
        # deterministic view is computed on the fly.
        if proposal_fn is not None:
            stored = proposal_fn(symbol)
            if stored is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No stored proposal: {symbol} (DATA PENDING)",
                )
            return ProposalResponse.from_proposal(stored)

        from argus.brain.agents import AnalysisContext
        from argus.decision.aggregator import default_aggregator

        series = ohlcv_reader_fn(symbol, None)
        if series is None or not series.bars:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No proposal: {symbol} (not on the watchlist or DATA PENDING)",
            )
        ctx = AnalysisContext(symbol=series.symbol, timeframe=series.timeframe, series=series)
        resp = ProposalResponse.from_proposal(default_aggregator().aggregate(ctx))
        return resp.model_copy(update={"measurement": _measurement_for(series, config_reader_fn)})

    @app.get("/api/crypto/{symbol}")
    def crypto_dashboard(symbol: str, _: str = Depends(require_auth)) -> CryptoDashboardResponse:
        """Returns the stored PIT crypto state; the endpoint produces no analysis."""
        canonical = symbol.strip().upper().replace("/", "")
        if not canonical:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol is empty")
        response = crypto_dashboard_fn(canonical)
        if response.symbol != canonical:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Crypto dashboard provider returned the wrong symbol",
            )
        return response

    # ── Trading control center (PIT read model + fail-closed local mutations) ──
    def _trading_control() -> TradingControlService:
        if trading_control_fn is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Trading control service not connected",
            )
        return trading_control_fn()

    def _control_conflict(exc: Exception) -> NoReturn:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/api/trading/status")
    def trading_status(_: str = Depends(require_auth)) -> TradingStatusResponse:
        return _trading_control().status()

    @app.get("/api/trading/account")
    def trading_account(_: str = Depends(require_auth)) -> TradingAccountResponse:
        return _trading_control().account()

    @app.get("/api/trading/universe")
    def trading_universe(
        limit: int = Query(default=100, ge=1, le=500),
        cursor: str | None = Query(default=None, min_length=1, max_length=64),
        _: str = Depends(require_auth),
    ) -> TradingUniverseResponse:
        return _trading_control().universe(limit=limit, cursor=cursor)

    @app.get("/api/trading/news")
    def trading_news(_: str = Depends(require_auth)) -> TradingNewsResponse:
        return _trading_control().news()

    @app.get("/api/trading/research")
    def trading_research(_: str = Depends(require_auth)) -> TradingResearchResponse:
        return _trading_control().research()

    @app.get("/api/trading/watchlist")
    def trading_watchlist(_: str = Depends(require_auth)) -> TradingWatchlistResponse:
        return _trading_control().watchlist()

    @app.get("/api/trading/plans")
    def trading_plans(_: str = Depends(require_auth)) -> TradingPlansResponse:
        return _trading_control().plans()

    @app.get("/api/trading/missions")
    def trading_missions(_: str = Depends(require_auth)) -> MissionListResponse:
        return _trading_control().missions()

    @app.get("/api/trading/missions/{mission_id}")
    def trading_mission(mission_id: str, _: str = Depends(require_auth)) -> MissionDetailResponse:
        result = _trading_control().mission(mission_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found")
        return result

    @app.get("/api/trading/positions")
    def trading_positions(_: str = Depends(require_auth)) -> TradingPositionsResponse:
        return _trading_control().positions()

    @app.get("/api/trading/orders")
    def trading_orders(_: str = Depends(require_auth)) -> TradingOrdersResponse:
        return _trading_control().orders()

    @app.get("/api/trading/fills")
    def trading_fills(_: str = Depends(require_auth)) -> TradingFillsResponse:
        return _trading_control().fills()

    @app.get("/api/trading/performance")
    def trading_performance(_: str = Depends(require_auth)) -> TradingPerformanceResponse:
        return _trading_control().performance()

    @app.get("/api/trading/costs")
    def trading_costs(_: str = Depends(require_auth)) -> TradingCostsResponse:
        return _trading_control().costs()

    @app.get("/api/trading/risk")
    def trading_risk(_: str = Depends(require_auth)) -> TradingRiskResponse:
        return _trading_control().risk()

    @app.get("/api/trading/storage")
    def trading_storage(_: str = Depends(require_auth)) -> TradingStorageResponse:
        return _trading_control().storage()

    @app.get("/api/trading/health")
    def trading_health(_: str = Depends(require_auth)) -> TradingHealthResponse:
        return _trading_control().health()

    # ── News / event hub: stored PIT results only ───────────────────────────
    def _intelligence_store() -> SqliteStore:
        from argus.storage.sqlite import SqliteStore

        candidate = intelligence_store_fn() if intelligence_store_fn is not None else journal_fn()
        if not isinstance(candidate, SqliteStore):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Intelligence SQLite store not connected — NO_DATA",
            )
        return candidate

    @app.get("/api/intelligence/sources")
    def intelligence_sources(
        source_group: str | None = Query(default=None, max_length=64),
        enabled: bool | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=100_000),
        _: str = Depends(require_auth),
    ) -> dict[str, object]:
        rows = _intelligence_store().source_registry_entries(limit=500)
        if source_group is not None:
            rows = [row for row in rows if row.source_group.value == source_group.casefold()]
        if enabled is not None:
            rows = [row for row in rows if row.enabled is enabled]
        page = rows[offset : offset + limit]
        return {
            "items": page,
            "returned_count": len(page),
            "total_count": len(rows),
            "limit": limit,
            "offset": offset,
            "data_status": "ok" if rows else "no_data",
        }

    @app.get("/api/intelligence/source-health")
    def intelligence_source_health(
        source_id: str | None = Query(default=None, max_length=100),
        limit: int = Query(default=100, ge=1, le=500),
        _: str = Depends(require_auth),
    ) -> dict[str, object]:
        rows = _intelligence_store().source_health_history(source_id=source_id, limit=limit)
        return {"items": rows, "data_status": "ok" if rows else "no_data"}

    @app.get("/api/intelligence/events")
    def intelligence_events(
        symbol: str | None = Query(default=None, max_length=30),
        event_type: str | None = Query(default=None, max_length=64),
        as_of: datetime | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=100_000),
        _: str = Depends(require_auth),
    ) -> dict[str, object]:
        if as_of is not None and as_of.tzinfo is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="as_of must be timezone-aware",
            )
        rows = _intelligence_store().latest_normalized_events(
            symbol=symbol,
            event_type=event_type,
            as_of=as_of.astimezone(UTC) if as_of else None,
            limit=limit,
            offset=offset,
        )
        return {
            "items": rows,
            "returned_count": len(rows),
            "limit": limit,
            "offset": offset,
            "data_status": "ok" if rows else "no_data",
        }

    @app.get("/api/intelligence/events/{event_id}")
    def intelligence_event_detail(
        event_id: str,
        _: str = Depends(require_auth),
    ) -> object:
        event = _intelligence_store().normalized_event(event_id)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        return event

    @app.get("/api/intelligence/symbol/{symbol}")
    def intelligence_symbol(
        symbol: str,
        limit: int = Query(default=100, ge=1, le=500),
        _: str = Depends(require_auth),
    ) -> dict[str, object]:
        canonical = symbol.strip().upper()
        store = _intelligence_store()
        events = store.latest_normalized_events(symbol=canonical, limit=limit)
        reactions = store.latest_news_market_reactions(symbol=canonical, limit=limit)
        research = [
            row for row in store.latest_coin_research_results(limit=500) if row.symbol == canonical
        ]
        plans = [row for row in store.latest_trade_plans(limit=500) if row.symbol == canonical]
        has_data = bool(events or reactions or research or plans)
        return {
            "symbol": canonical,
            "events": events,
            "market_reactions": reactions,
            "research": research,
            "trade_plans": plans,
            "data_status": "ok" if has_data else "no_data",
        }

    @app.get("/api/intelligence/market-reactions")
    def intelligence_market_reactions(
        symbol: str | None = Query(default=None, max_length=30),
        limit: int = Query(default=100, ge=1, le=500),
        _: str = Depends(require_auth),
    ) -> dict[str, object]:
        rows = _intelligence_store().latest_news_market_reactions(
            symbol=symbol.strip().upper() if symbol else None, limit=limit
        )
        return {"items": rows, "data_status": "ok" if rows else "no_data"}

    @app.get("/api/intelligence/trade-plans")
    def intelligence_trade_plans(
        symbol: str | None = Query(default=None, max_length=30),
        limit: int = Query(default=100, ge=1, le=500),
        _: str = Depends(require_auth),
    ) -> dict[str, object]:
        rows = _intelligence_store().latest_trade_plans(limit=limit)
        if symbol:
            canonical = symbol.strip().upper()
            rows = [row for row in rows if row.symbol == canonical]
        return {
            "items": rows,
            "execution_enabled": False,
            "data_status": "ok" if rows else "no_data",
        }

    @app.get("/api/intelligence/health")
    def intelligence_health(_: str = Depends(require_auth)) -> dict[str, object]:
        snapshot = _intelligence_store().intelligence_health()
        snapshot.update(
            {
                "system_mode": config_reader_fn().trading.mode.value,
                "live_execution_enabled": False,
                "private_endpoints_enabled": False,
                "data_status": (
                    "ok"
                    if snapshot["event_generation_count"] or snapshot["source_status_counts"]
                    else "no_data"
                ),
            }
        )
        return snapshot

    def _webhook_secret() -> str | None:
        if tradingview_webhook_secret is not None:
            return tradingview_webhook_secret
        from argus.config.settings import get_settings

        configured = get_settings().tradingview_webhook_secret
        return configured.get_secret_value() if configured is not None else None

    def _webhook_rejected(reason: str, *, subject_id: str | None = None) -> None:
        try:
            _intelligence_store().append_audit_event(
                "tradingview_webhook_rejected", {"reason": reason}, subject_id=subject_id
            )
        except Exception:
            # The rejection path logs neither credentials nor the request body; a storage
            # failure must not change the auth outcome.
            return

    @app.post("/api/intelligence/webhooks/tradingview")
    def tradingview_webhook(
        body: TradingViewWebhookRequest,
        x_argus_webhook_token: str | None = Header(default=None, alias="X-Argus-Webhook-Token"),
    ) -> TradingViewWebhookAck:
        config = config_reader_fn().news_intelligence
        if not config.tradingview_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook disabled")
        expected = _webhook_secret()
        if (
            expected is None
            or x_argus_webhook_token is None
            or not hmac.compare_digest(expected, x_argus_webhook_token)
        ):
            _webhook_rejected("authentication_failed")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        at = utcnow()
        age_seconds = (at - body.timestamp).total_seconds()
        if age_seconds < -30 or age_seconds > config.tradingview_ttl_seconds:
            _webhook_rejected("timestamp_outside_ttl", subject_id=body.idempotency_key)
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT, detail="Signal expired"
            )
        monotonic_now = time.monotonic()
        with webhook_rate_lock:
            while webhook_attempts and webhook_attempts[0] <= monotonic_now - 60:
                webhook_attempts.popleft()
            if len(webhook_attempts) >= config.tradingview_rate_limit_per_minute:
                _webhook_rejected("rate_limited", subject_id=body.idempotency_key)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded"
                )
            webhook_attempts.append(monotonic_now)
        signal = ExternalTechnicalSignal(
            symbol=body.symbol,
            signal_type=body.signal_type,
            timeframe=body.timeframe,
            direction=body.direction,
            observed_at=body.timestamp,
            received_at=at,
            nonce=body.nonce,
            idempotency_key=body.idempotency_key,
            attributes=body.attributes,
            data_status=DataStatus.QUARANTINED,
            submission_authorized=False,
        )
        store = _intelligence_store()
        if not store.save_external_technical_signal(signal):
            _webhook_rejected("replay_or_duplicate", subject_id=body.idempotency_key)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Duplicate signal")
        store.append_audit_event(
            "external_technical_signal_received",
            {"source_id": signal.source_id, "symbol": signal.symbol, "accepted_for_orders": False},
            subject_id=signal.signal_id,
            occurred_at=at,
        )
        return TradingViewWebhookAck(signal_id=signal.signal_id)

    @app.post("/api/trading/missions")
    def create_trading_mission(
        body: MissionCreateRequest,
        actor: str = Depends(trading_mutation_user),
    ) -> MissionMutationResponse:
        try:
            return _trading_control().create_mission(body, actor=actor)
        except (ConcurrentStateError, RiskRejectedError, ValueError) as exc:
            _control_conflict(exc)

    @app.post("/api/trading/missions/{mission_id}/arm")
    def arm_trading_mission(
        mission_id: str,
        body: MissionArmRequest,
        actor: str = Depends(trading_mutation_user),
    ) -> MissionMutationResponse:
        try:
            return _trading_control().arm_mission(
                mission_id, expected_revision=body.expected_revision, actor=actor
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found"
            ) from exc
        except (ConcurrentStateError, RiskRejectedError, ValueError) as exc:
            _control_conflict(exc)

    @app.post("/api/trading/missions/{mission_id}/halt")
    def halt_trading_mission(
        mission_id: str,
        body: MissionHaltRequest,
        actor: str = Depends(trading_mutation_user),
    ) -> MissionMutationResponse:
        try:
            return _trading_control().halt_mission(
                mission_id,
                expected_revision=body.expected_revision,
                reason=body.reason,
                actor=actor,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mission not found"
            ) from exc
        except (ConcurrentStateError, RiskRejectedError, ValueError) as exc:
            _control_conflict(exc)

    @app.post("/api/trading/kill-switch")
    def enable_trading_kill_switch(
        body: KillSwitchRequest,
        actor: str = Depends(trading_mutation_user),
    ) -> KillSwitchResponse:
        try:
            return _trading_control().enable_kill_switch(reason=body.reason, actor=actor)
        except (ConcurrentStateError, RiskRejectedError, ValueError) as exc:
            _control_conflict(exc)

    @app.get("/api/news")
    def news(
        limit: int = 50,
        source: str | None = None,
        _: str = Depends(require_auth),
    ) -> list[NewsResponseItem]:
        limit = max(1, min(limit, 200))
        records = _journal().recent_news(source=source, limit=limit)
        return [NewsResponseItem.from_record(r) for r in records]

    @app.get("/api/portfolio")
    def portfolio(_: str = Depends(require_auth)) -> PortfolioResponse:
        return PortfolioResponse.from_summary(portfolio_fn())

    # ── Settings (write layer; secrets are write-only, masked in responses) ───
    @app.get("/api/settings")
    def get_settings_endpoint(_: str = Depends(require_auth)) -> SettingsView:
        return settings_reader_fn()

    @app.post("/api/settings/llm")
    def save_llm(body: LLMSettingsUpdate, _: str = Depends(require_auth)) -> SettingsWriteResult:
        updates: dict[str, str] = {}
        if body.anthropic_api_key is not None:
            updates["ANTHROPIC_API_KEY"] = body.anthropic_api_key
        if body.openai_api_key is not None:
            updates["ARGUS_OPENAI_API_KEY"] = body.openai_api_key
        if body.openrouter_api_key is not None:
            updates["ARGUS_OPENROUTER_API_KEY"] = body.openrouter_api_key
        if body.llm_provider is not None:
            updates["ARGUS_LLM_PROVIDER"] = body.llm_provider
        if body.ollama_base_url is not None:
            updates["ARGUS_OLLAMA_BASE_URL"] = body.ollama_base_url
        if body.ollama_model is not None:
            updates["ARGUS_OLLAMA_MODEL"] = body.ollama_model
        if body.provider_routine is not None:
            updates["ARGUS_PROVIDER_ROUTINE"] = body.provider_routine
        if body.provider_deep is not None:
            updates["ARGUS_PROVIDER_DEEP"] = body.provider_deep
        if body.provider_copilot is not None:
            updates["ARGUS_PROVIDER_COPILOT"] = body.provider_copilot
        if body.provider_news_router is not None:
            updates["ARGUS_PROVIDER_NEWS_ROUTER"] = body.provider_news_router
        if body.provider_exchange_expert is not None:
            updates["ARGUS_PROVIDER_EXCHANGE_EXPERT"] = body.provider_exchange_expert
        if body.model_routine is not None:
            updates["ARGUS_MODEL_ROUTINE"] = body.model_routine
        if body.model_deep is not None:
            updates["ARGUS_MODEL_DEEP"] = body.model_deep
        if body.model_copilot is not None:
            updates["ARGUS_MODEL_COPILOT"] = body.model_copilot
        if body.model_news_router is not None:
            updates["ARGUS_MODEL_NEWS_ROUTER"] = body.model_news_router
        if body.model_exchange_expert is not None:
            updates["ARGUS_MODEL_EXCHANGE_EXPERT"] = body.model_exchange_expert
        if body.llm_daily_budget_usd is not None:
            updates["ARGUS_LLM_DAILY_BUDGET_USD"] = str(body.llm_daily_budget_usd)
        env_writer_fn(updates)

        # Honest note if a not-yet-working provider (openai/openrouter) is picked for a role.
        from argus.brain.llm import LIVE_PROVIDERS

        live = {p.value for p in LIVE_PROVIDERS}
        chosen = {
            body.provider_routine,
            body.provider_deep,
            body.provider_copilot,
            body.provider_news_router,
            body.provider_exchange_expert,
        } - {None}
        pending = sorted(p for p in chosen if p and p not in live)
        msg = "LLM settings saved. Restart Argus to apply them live."
        if pending:
            msg += (
                f" Note: {', '.join(pending)} not active yet (coming soon) — "
                "those role(s) will not work."
            )
        return SettingsWriteResult(requires_restart=True, message=msg)

    @app.post("/api/settings/telegram")
    def save_telegram(
        body: TelegramSettingsUpdate, _: str = Depends(require_auth)
    ) -> SettingsWriteResult:
        updates: dict[str, str] = {}
        if body.bot_token is not None:
            updates["TELEGRAM_BOT_TOKEN"] = body.bot_token
        if body.authorized_chat_ids is not None:
            updates["ARGUS_AUTHORIZED_CHAT_IDS"] = ",".join(
                str(i) for i in body.authorized_chat_ids
            )
        env_writer_fn(updates)
        return SettingsWriteResult(
            requires_restart=True,
            message="Telegram settings saved. Restart Argus to apply them live.",
        )

    @app.post("/api/settings/data")
    def save_data_keys(
        body: DataSettingsUpdate, _: str = Depends(require_auth)
    ) -> SettingsWriteResult:
        # Data source keys (EVDS macro + news APIs) — write-only, into .env.
        updates: dict[str, str] = {}
        if body.evds_key is not None:
            updates["ARGUS_EVDS_KEY"] = body.evds_key
        if body.cryptopanic_token is not None:
            updates["ARGUS_CRYPTOPANIC_TOKEN"] = body.cryptopanic_token
        if body.finnhub_key is not None:
            updates["ARGUS_FINNHUB_KEY"] = body.finnhub_key
        env_writer_fn(updates)
        return SettingsWriteResult(
            requires_restart=True,
            message=("Data source keys saved. Restart the poller/watch processes."),
        )

    @app.post("/api/settings/watchlist")
    def save_watchlist(
        body: WatchlistUpdate, _: str = Depends(require_auth)
    ) -> SettingsWriteResult:
        new_config = config_reader_fn().model_copy(
            update={"watchlist": body.watchlist, "holdings": body.holdings}
        )
        config_writer_fn(new_config)
        return SettingsWriteResult(
            requires_restart=True,
            message=(
                "Watchlist saved. The running scheduler uses a static job plan, so "
                "restart the watch process."
            ),
        )

    @app.post("/api/settings/triggers")
    def save_triggers(body: TriggerUpdate, _: str = Depends(require_auth)) -> SettingsWriteResult:
        new_config = config_reader_fn().model_copy(
            update={"triggers": body.triggers, "alerts": body.alerts}
        )
        config_writer_fn(new_config)
        return SettingsWriteResult(
            requires_restart=True,
            message=(
                "Trigger/alert settings saved. A restart is required for the running "
                "watch process to apply them."
            ),
        )

    # ── Argus Copilot (chat — talking to the brain) ──────────────────────────
    @app.post("/api/chat")
    def chat(body: ChatRequest, _: str = Depends(require_auth)) -> ChatResponse:
        from argus.brain.llm import LLMMessage
        from argus.core.errors import BudgetExceededError

        engine = chat_fn()

        # Context: the selected asset's live indicators (quotes) + latest analysis (if any).
        # All of it is added as DATA (injection-safe); missing values are skipped, never made up.
        context_text: str | None = None
        if body.context_symbol:
            parts: list[str] = []
            # 1) Live quote (price/indicators/freshness from the store's OHLCV) — so Copilot
            #    sees the selected symbol's current state even if the analysis journal is empty.
            quote = next((q for q in quotes_fn() if q.symbol == body.context_symbol), None)
            if quote is not None:
                metrics = []
                if quote.price is not None:
                    metrics.append(f"price={quote.price}")
                if quote.change_pct_24h is not None:
                    metrics.append(f"24h={quote.change_pct_24h:.2f}%")
                if quote.rsi is not None:
                    metrics.append(f"RSI={quote.rsi:.1f}")
                if quote.macd is not None:
                    metrics.append(f"MACD={quote.macd:.2f}")
                if quote.atr is not None:
                    metrics.append(f"ATR={quote.atr:.2f}")
                if metrics:
                    parts.append(
                        f"{quote.symbol} live indicators [{', '.join(metrics)}, "
                        f"freshness={quote.freshness}] (evidence tier [A], computed)"
                    )
            # 2) Latest analysis (if any) — when a deep /analyze has run.
            reader = journal_fn()
            if reader is not None:
                rows = reader.recent_analyses(symbol=body.context_symbol, limit=1)
                if rows:
                    a = rows[0].analysis
                    parts.append(
                        f"{a.symbol} ({a.timeframe.value}) latest analysis: {a.summary} "
                        f"[bias={a.bias}, confidence={a.confidence_pct}%, "
                        f"evidence={a.evidence_tier.value}]"
                    )
            if parts:
                context_text = "\n".join(parts)

        msgs = [LLMMessage(role=m.role, content=m.content) for m in body.messages]
        chat_status: Literal["ok", "budget_exceeded", "error"] = "ok"
        try:
            result = engine.respond(msgs, context_text=context_text)
            reply, model, cost = result.text, result.model, result.cost_usd
        except BudgetExceededError as exc:
            # Budget → explicit status; the reply text is for the user, status for machines.
            chat_status = "budget_exceeded"
            reply = (
                f"⚠️ Daily LLM budget exhausted ({exc}). It resets at the end of the day, "
                "or raise the daily cap under Settings → LLM."
            )
            model, cost = "budget-exceeded", 0.0

        store = chat_store_fn()
        if store is not None and msgs:
            store.save_chat("user", msgs[-1].content)
            store.save_chat("assistant", reply)

        return ChatResponse(
            reply=reply, model=model, cost_usd=cost, demo=engine.demo, status=chat_status
        )

    @app.get("/api/chat/history")
    def chat_history(limit: int = 50, _: str = Depends(require_auth)) -> list[ChatTurnView]:
        store = chat_store_fn()
        if store is None:
            return []
        limit = max(1, min(limit, 200))
        return [ChatTurnView.from_turn(t) for t in store.recent_chat(limit=limit)]

    @app.get("/api/cost")
    def cost(_: str = Depends(require_auth)) -> CostResponse:
        """Today's LLM spend vs cap + line-item breakdown + monthly projection."""
        summary = cost_fn()
        if summary is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cost ledger not connected — DATA PENDING",
            )
        return summary

    @app.get("/api/feedback/summary")
    def feedback_summary(days: int = 30, _: str = Depends(require_auth)) -> FeedbackSummaryResponse:
        """User-value summary: 👍 ratio + symbol/severity breakdown (last N days)."""
        reader = feedback_fn()
        if reader is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Feedback store not connected — DATA PENDING",
            )
        days = max(1, min(days, 365))
        return FeedbackSummaryResponse.from_summary(reader.feedback_summary(days=days))

    @app.post("/api/feedback")
    def post_feedback(body: FeedbackRequest, user: str = Depends(require_auth)) -> FeedbackAck:
        """A 👍/👎 vote from the web cockpit — writes to the same store as Telegram (idempotent).

        The session user becomes ``user_id``; the same user may change their mind (the last
        vote wins). 503 if the store is not connected (no fake acknowledgement).
        """
        writer = feedback_writer_fn()
        if writer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Feedback store not connected — vote could not be recorded",
            )
        writer.save_feedback(
            body.ref_type,
            body.ref_id,
            f"web:{user}",
            useful=body.useful,
            symbol=body.symbol,
            severity=body.severity,
        )
        return FeedbackAck(useful=body.useful)

    # ── News router (classify one manually supplied item — first multi-AI agent) ──
    @app.post("/api/news/route")
    def news_route(body: NewsRouteRequest, _: str = Depends(require_auth)) -> NewsRouteResponse:
        from argus.core.errors import BudgetExceededError

        nr = news_router_fn()
        try:
            r = nr.route(
                body.title,
                summary=body.summary,
                source=body.source,
                symbols=body.symbols,
            )
        except BudgetExceededError:
            # Budget exhausted → explicit "could not be evaluated" (NO fake low-importance label).
            return NewsRouteResponse(
                status="budget_exceeded",
                rationale="Daily LLM budget exhausted — the news item could not be evaluated.",
                demo=nr.demo,
            )
        except Exception as exc:  # LLM/parse error → an honest status, not a crash
            return NewsRouteResponse(
                status="error",
                rationale=f"Classification failed ({type(exc).__name__}) — could not be evaluated.",
                demo=nr.demo,
            )
        return NewsRouteResponse(
            status="ok",
            importance=r.importance.value,
            relevant=r.relevant,
            target_agent=r.target_agent,
            rationale=r.rationale,
            confidence_pct=r.confidence_pct,
            model=r.model,
            demo=nr.demo,
        )

    return app


def _default_status_provider() -> SystemStatus:
    """Production provider — gathers status from real settings (lazy: factory does IO)."""
    from argus.interface.factory import build_status

    return build_status()


def _default_journal_provider() -> JournalReader | None:
    """Production provider — the configured SQLite journal (None if absent → 503)."""
    from argus.config.settings import get_settings
    from argus.interface.factory import _build_journal
    from argus.storage.sqlite import SqliteStore

    journal = _build_journal(get_settings())
    return journal if isinstance(journal, SqliteStore) else None


def _cost_from_store(store: SqliteStore | None, settings: Settings) -> CostResponse | None:
    """Builds the cost summary from an open store; the caller owns the connection."""
    import calendar

    from argus.core.schemas import utcnow

    if store is None:
        return None
    today = utcnow().date()
    day = today.isoformat()
    rows = store.llm_cost_for_day(day)
    tokens = sum(t for t, _ in rows.values())
    search = sum(s for _, s in rows.values())
    spent = tokens + search
    mtd = store.llm_cost_for_month(day[:7])
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projection = mtd / today.day * days_in_month  # rough linear projection (labelled as such)
    return CostResponse(
        day=day,
        budget_usd=settings.llm_daily_budget_usd,
        spent_today_usd=round(spent, 4),
        remaining_today_usd=round(max(settings.llm_daily_budget_usd - spent, 0.0), 4),
        tokens_usd=round(tokens, 4),
        search_usd=round(search, 4),
        by_role={role: round(t + s, 4) for role, (t, s) in sorted(rows.items())},
        month_to_date_usd=round(mtd, 4),
        month_projection_usd=round(projection, 2),
        last_days=tuple((d, round(v, 4)) for d, v in store.llm_cost_recent_days(days=7)),
    )


def _default_cost_provider() -> CostResponse | None:
    """Production provider — today's spend + monthly projection from the cost ledger."""
    from argus.config.settings import get_settings
    from argus.interface.factory import build_store

    settings = get_settings()
    store = build_store(settings)
    try:
        return _cost_from_store(store, settings)
    finally:
        if store is not None:
            store.close()


def _default_feedback_provider() -> FeedbackReader | None:
    """Production provider — the configured SQLite store (None if absent → 503)."""
    from argus.config.settings import get_settings
    from argus.interface.factory import build_store

    return build_store(get_settings())


def _default_feedback_writer_provider() -> FeedbackWriter | None:
    """Production provider — the same SQLite store for votes (shared with Telegram)."""
    from argus.config.settings import get_settings
    from argus.interface.factory import build_store

    return build_store(get_settings())


def _quotes_from_store(store: SqliteStore | None, config: RuntimeConfig) -> list[QuoteRow]:
    """Builds quote rows from an open store + typed config."""
    from argus.api.quotes import build_quote_row
    from argus.core.enums import Timeframe
    from argus.interface.factory import build_adjuster

    if store is None:
        return []
    timeframe = Timeframe(config.loop.timeframe)
    adjuster = build_adjuster(store)
    rows: list[QuoteRow] = []
    for item in config.watchlist:
        primary = Timeframe(item.timeframes[0]) if item.timeframes else timeframe
        series = store.load_series(item.symbol, primary, limit=config.loop.limit)
        if adjuster is not None:
            series = adjuster(series)
        rows.append(build_quote_row(item, series, default_timeframe=config.loop.timeframe))
    return rows


def _default_quotes_provider() -> list[QuoteRow]:
    """Production provider — quote rows from the config watchlist + the store's OHLCV.

    **Collects no new data**: only stored OHLCV is read; without it the row is DATA PENDING
    (price/indicators None). Empty list if the store is not SQLite.
    """
    from argus.config.loader import ConfigLoader
    from argus.config.settings import get_settings
    from argus.interface.factory import _build_journal
    from argus.storage.sqlite import SqliteStore

    store = _build_journal(get_settings())
    if not isinstance(store, SqliteStore):
        return []
    try:
        return _quotes_from_store(store, ConfigLoader().load())
    finally:
        store.close()


def _ohlcv_from_store(
    store: SqliteStore | None, config: RuntimeConfig, symbol: str, tf: str | None = None
) -> OHLCVSeries | None:
    """Reads only config-allowed symbol/TF series from an open store."""
    from argus.core.enums import Timeframe
    from argus.interface.factory import build_adjuster

    if store is None:
        return None
    items = {item.symbol: item for item in config.watchlist}
    known = set(items) | {h.symbol for h in config.holdings}
    if symbol not in known:
        return None
    item = items.get(symbol)
    allowed = set(item.timeframes if item is not None else []) | {config.loop.timeframe}
    requested = tf or config.loop.timeframe
    if requested not in allowed:
        return None
    try:
        timeframe = Timeframe(requested)
    except ValueError:
        return None
    series = store.load_series(symbol, timeframe, limit=config.loop.limit)
    adjuster = build_adjuster(store)
    return adjuster(series) if adjuster is not None else series


def _crypto_dashboard_from_store(
    store: SqliteStore | None, config: RuntimeConfig, symbol: str
) -> CryptoDashboardResponse:
    """Builds the cockpit view from persisted PIT records only; no computation/ingestion."""
    from argus.core.crypto import (
        BasisSnapshot,
        FundingRate,
        FuturesTicker,
        LiquidationEvent,
        LongShortRatio,
        OpenInterest,
        OrderBookSnapshot,
        SpotTicker,
    )
    from argus.core.schemas import DataStatus, FreshnessLabel

    canonical = symbol.strip().upper().replace("/", "")
    if canonical not in config.universe.symbols or store is None:
        pending_missing = (
            ("symbol_not_in_universe",)
            if canonical not in config.universe.symbols
            else ("storage",)
        )
        return CryptoDashboardResponse(
            symbol=canonical, data_status=DataStatus.PENDING, missing_data=pending_missing
        )

    def latest(record_type: str) -> object | None:
        return store.latest_crypto_record(canonical, record_type)

    spot_raw = latest("SpotTicker")
    futures_raw = latest("FuturesTicker")
    funding_raw = latest("FundingRate")
    oi_raw = latest("OpenInterest")
    basis_raw = latest("BasisSnapshot")
    ratio_raw = latest("LongShortRatio")
    book_raw = latest("OrderBookSnapshot")
    liquidations_raw = store.recent_crypto_records(canonical, "LiquidationEvent", limit=20)

    spot = spot_raw if isinstance(spot_raw, SpotTicker) else None
    futures = futures_raw if isinstance(futures_raw, FuturesTicker) else None
    funding = funding_raw if isinstance(funding_raw, FundingRate) else None
    open_interest = oi_raw if isinstance(oi_raw, OpenInterest) else None
    basis = basis_raw if isinstance(basis_raw, BasisSnapshot) else None
    long_short_ratio = ratio_raw if isinstance(ratio_raw, LongShortRatio) else None
    order_book = book_raw if isinstance(book_raw, OrderBookSnapshot) else None
    liquidations = tuple(item for item in liquidations_raw if isinstance(item, LiquidationEvent))

    def artifact(name: str) -> CryptoArtifactView | None:
        row = store.latest_crypto_artifact(canonical, name)
        return (
            CryptoArtifactView(as_of=row[0], data_status=row[1], payload=row[2])
            if row is not None
            else None
        )

    quality = artifact("quality")
    features = artifact("features")
    regime = artifact("regime")
    experts = artifact("experts")
    backtest = artifact("backtest")
    candidate = store.latest_trade_candidate(canonical)
    guard = store.latest_risk_assessment(canonical)
    shadow_positions = tuple(store.shadow_positions(symbol=canonical))

    named_required = {
        "spot": spot,
        "perpetual": futures,
        "funding": funding,
        "open_interest": open_interest,
        "basis": basis,
        "order_book": order_book,
        "quality": quality,
        "features": features,
        "regime": regime,
        "experts": experts,
    }
    named_optional_outputs = {
        "trade_candidate": candidate,
        "guard": guard,
        "backtest": backtest,
    }
    blocking_missing = [name for name, value in named_required.items() if value is None]
    missing = [
        *blocking_missing,
        *(name for name, value in named_optional_outputs.items() if value is None),
    ]
    records = tuple(
        item
        for item in (spot, futures, funding, open_interest, basis, long_short_ratio, order_book)
        if item is not None
    )
    missing.extend(
        f"{type(record).__name__}:stale"
        for record in records
        if record.freshness is not FreshnessLabel.FRESH
    )
    statuses = [record.data_status for record in records]
    statuses.extend(
        item.data_status
        for item in (quality, features, regime, experts, backtest)
        if item is not None
    )
    if DataStatus.REJECTED in statuses:
        data_status = DataStatus.REJECTED
    elif DataStatus.QUARANTINED in statuses:
        data_status = DataStatus.QUARANTINED
    elif blocking_missing:
        data_status = DataStatus.PENDING
    else:
        data_status = DataStatus.OK

    timestamps = [record.event_timestamp for record in records]
    timestamps.extend(
        item.as_of for item in (quality, features, regime, experts, backtest) if item is not None
    )
    if candidate is not None:
        timestamps.append(candidate.created_at)
    if guard is not None:
        timestamps.append(guard.assessed_at)
    return CryptoDashboardResponse(
        symbol=canonical,
        data_status=data_status,
        as_of=max(timestamps) if timestamps else None,
        spot=spot,
        perpetual=futures,
        funding=funding,
        open_interest=open_interest,
        basis=basis,
        long_short_ratio=long_short_ratio,
        order_book=order_book,
        liquidations=liquidations,
        quality=quality,
        features=features,
        regime=regime,
        experts=experts,
        trade_candidate=candidate,
        guard=guard,
        shadow_positions=shadow_positions,
        backtest=backtest,
        missing_data=tuple(missing),
    )


def _default_ohlcv_reader(symbol: str, tf: str | None = None) -> OHLCVSeries | None:
    """Production reader — reads the candle array of **config-defined** symbol+TF pairs only.

    Security: a symbol not on the watchlist, or a TF not defined in config for that symbol →
    None (the endpoint returns 404; no silent fallback). Collects no new data.
    """
    from argus.config.loader import ConfigLoader
    from argus.config.settings import get_settings
    from argus.interface.factory import _build_journal
    from argus.storage.sqlite import SqliteStore

    config = ConfigLoader().load()
    store = _build_journal(get_settings())
    if not isinstance(store, SqliteStore):
        return None
    try:
        return _ohlcv_from_store(store, config, symbol, tf)
    finally:
        store.close()


def _default_portfolio_provider() -> PortfolioSummary:
    """Production provider — portfolio value/P&L from config holdings + last quote prices.

    Prices come from `_default_quotes_provider` (store OHLCV) → no new data is collected.
    With no holdings the summary is empty ("portfolio not connected" in the UI).
    """
    from argus.api.portfolio import build_portfolio
    from argus.config.loader import ConfigLoader

    config = ConfigLoader().load()
    if not config.holdings:
        return build_portfolio([], {})

    prices: dict[str, float | None] = {row.symbol: row.price for row in _default_quotes_provider()}
    return build_portfolio(config.holdings, prices)


def _default_settings_reader() -> SettingsView:
    """Production reader — gathers settings + config into a **masked** SettingsView."""
    from argus.api.settings_models import (
        DataSettingsView,
        LLMSettingsView,
        RoleModel,
        SecretStatus,
        TelegramSettingsView,
        TriggerSettingsView,
        WatchlistSettingsView,
    )
    from argus.brain.llm import LIVE_PROVIDERS, Provider
    from argus.config.loader import ConfigLoader
    from argus.config.settings import get_settings

    s = get_settings()
    cfg = ConfigLoader().load()

    def _active(role: str, label: str, model: str) -> RoleModel:
        # Each active role picks its own provider (provider_for falls back to the global one).
        return RoleModel(
            role=role, label=label, provider=s.provider_for(role), model=model, active=True
        )

    # Active roles (actually in use) + planned roles (multi-AI — not yet).
    roles = [
        _active("routine", "Routine (alerts/scans)", s.model_routine),
        _active("deep", "Deep analysis (/analyze)", s.model_deep),
        _active("copilot", "Copilot (cockpit chat)", s.model_copilot),
        _active("news_router", "News router", s.model_news_router),
        # BIST expert is active (empty model → the Ollama default is shown).
        _active(
            "exchange_expert",
            "BIST expert (context filter)",
            s.model_exchange_expert or s.ollama_model,
        ),
        RoleModel(role="manager", label="Manager AI", provider="—", model="—", active=False),
    ]
    return SettingsView(
        llm=LLMSettingsView(
            anthropic_api_key=SecretStatus.from_secret(s.anthropic_api_key),
            openai_api_key=SecretStatus.from_secret(s.openai_api_key),
            openrouter_api_key=SecretStatus.from_secret(s.openrouter_api_key),
            llm_provider=s.llm_provider,
            providers=[p.value for p in Provider],
            live_providers=sorted(p.value for p in LIVE_PROVIDERS),
            ollama_base_url=s.ollama_base_url,
            ollama_model=s.ollama_model,
            model_routine=s.model_routine,
            model_deep=s.model_deep,
            model_copilot=s.model_copilot,
            llm_daily_budget_usd=s.llm_daily_budget_usd,
            roles=roles,
        ),
        telegram=TelegramSettingsView(
            bot_token=SecretStatus.from_secret(s.telegram_bot_token),
            authorized_chat_ids=sorted(s.authorized_chat_id_set),
        ),
        watch=WatchlistSettingsView(watchlist=cfg.watchlist, holdings=cfg.holdings),
        triggers=TriggerSettingsView(triggers=cfg.triggers, alerts=cfg.alerts),
        data=DataSettingsView(
            evds_key=SecretStatus.from_secret(s.evds_key),
            cryptopanic_token=SecretStatus.from_secret(s.cryptopanic_token),
            finnhub_key=SecretStatus.from_secret(s.finnhub_key),
        ),
    )


def _default_env_writer(updates: dict[str, str]) -> None:
    """Production writer — updates `.env` + clears the settings cache (next read is fresh)."""
    from pathlib import Path

    from argus.config.env_writer import update_env_file
    from argus.config.settings import get_settings

    update_env_file(Path(".env"), updates)
    get_settings.cache_clear()


def _default_config_writer(config: RuntimeConfig) -> None:
    """Production writer — atomic write to config.yaml (hot-reloaded)."""
    from argus.config.loader import ConfigLoader

    ConfigLoader().save(config)


def _default_config_reader() -> RuntimeConfig:
    from argus.config.loader import ConfigLoader

    return ConfigLoader().load()


def _measurement_for(series: OHLCVSeries, config_reader: ConfigReader) -> MeasurementView | None:
    """Computes the thresholds hit rate v2 will test the proposal against (PIT: existing
    candles only).

    Not a target/stop — the ``ref ± k_atr × ATR`` levels the evaluator will use. Returns None
    when ATR cannot be computed (too few candles); no threshold is made up.
    """
    from argus.config.models import OutcomeConfig
    from argus.platform.indicators import atr as atr_series

    try:
        oc = config_reader().outcome
    except Exception:  # schema defaults if config is unreadable (as the evaluator does)
        oc = OutcomeConfig()
    bars = series.bars
    atr_vals = atr_series(
        [float(b.high) for b in bars],
        [float(b.low) for b in bars],
        [float(b.close) for b in bars],
        oc.atr_period,
    )
    atr_val = atr_vals[-1] if atr_vals else None
    if atr_val is None:
        return None
    last = bars[-1]
    return MeasurementView(
        ref_price=float(last.close),
        ref_time=int(last.timestamp.timestamp()),
        atr=atr_val,
        atr_period=oc.atr_period,
        k_atr=oc.k_atr,
        threshold_abs=oc.k_atr * atr_val,
        horizon_bars=oc.horizon_bars,
    )


def _default_chat_provider() -> ChatEngine:
    """Production provider — chat engine from settings (demo mode without an API key)."""
    from argus.brain.chat import build_chat_engine
    from argus.config.settings import get_settings
    from argus.interface.factory import build_store

    settings = get_settings()
    # Copilot spend is also written to the shared cost ledger.
    return build_chat_engine(settings, cost_store=build_store(settings))


def _default_chat_store_provider() -> ChatStore | None:
    """Production provider — SQLite store for chat history (None if absent)."""
    from argus.config.settings import get_settings
    from argus.interface.factory import _build_journal
    from argus.storage.sqlite import SqliteStore

    store = _build_journal(get_settings())
    return store if isinstance(store, SqliteStore) else None


def _default_news_router_provider() -> NewsRouter:
    """Production provider — news router from settings (demo mode without a key)."""
    from argus.brain.news_router import build_news_router
    from argus.config.settings import get_settings
    from argus.interface.factory import build_store

    settings = get_settings()
    return build_news_router(settings, cost_store=build_store(settings))


def build_app_from_settings() -> FastAPI:
    """Full app from settings; owns one SQLite connection for the FastAPI lifespan."""
    import ipaddress
    from contextlib import asynccontextmanager

    from argus.brain.chat import build_chat_engine
    from argus.brain.news_router import build_news_router
    from argus.config.loader import ConfigLoader
    from argus.config.settings import get_settings
    from argus.interface.factory import build_status, build_store
    from argus.interface.trading_control import TradingControlService
    from argus.observability.health import operational_registry

    cfg = get_settings()
    loader = ConfigLoader()
    store = build_store(cfg)
    trading_control = TradingControlService(store, loader.load) if store is not None else None
    origins = tuple(o.strip() for o in cfg.dashboard_cors_origins.split(",") if o.strip())
    try:
        trading_local_control = ipaddress.ip_address(cfg.dashboard_bind).is_loopback
    except ValueError:
        trading_local_control = cfg.dashboard_bind.strip().lower() == "localhost"

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if trading_control is not None:
                # After a restart an active mission never stays directly RUNNING.
                trading_control.recover_after_restart()
            yield
        finally:
            if store is not None:
                store.close()

    def _journal_provider() -> JournalReader | None:
        return store

    def _chat_store_provider() -> ChatStore | None:
        return store

    def _feedback_provider() -> FeedbackReader | None:
        return store

    def _feedback_writer_provider() -> FeedbackWriter | None:
        return store

    def _quotes_provider() -> list[QuoteRow]:
        return _quotes_from_store(store, loader.load())

    def _ohlcv_reader(symbol: str, tf: str | None) -> OHLCVSeries | None:
        return _ohlcv_from_store(store, loader.load(), symbol, tf)

    def _portfolio_provider() -> PortfolioSummary:
        from argus.api.portfolio import build_portfolio

        config = loader.load()
        prices = {row.symbol: row.price for row in _quotes_from_store(store, config)}
        return build_portfolio(config.holdings, prices)

    return create_app(
        status_provider=lambda: build_status(cfg, store=store),
        health_provider=lambda: operational_registry(cfg, store, loader.load()),
        journal_provider=_journal_provider,
        quotes_provider=_quotes_provider,
        portfolio_provider=_portfolio_provider,
        chat_provider=lambda: build_chat_engine(cfg, cost_store=store),
        chat_store_provider=_chat_store_provider,
        news_router_provider=lambda: build_news_router(cfg, cost_store=store),
        feedback_provider=_feedback_provider,
        feedback_writer_provider=_feedback_writer_provider,
        cost_provider=lambda: _cost_from_store(store, cfg),
        proposal_provider=lambda symbol: (
            store.latest_proposal(symbol) if store is not None else None
        ),
        crypto_dashboard_provider=lambda symbol: _crypto_dashboard_from_store(
            store, loader.load(), symbol
        ),
        trading_control_provider=(
            (lambda: trading_control) if trading_control is not None else None
        ),
        ohlcv_reader=_ohlcv_reader,
        users=parse_users(cfg.dashboard_users.get_secret_value()),
        session=SessionConfig.from_secret(cfg.dashboard_session_secret.get_secret_value()),
        secure_cookie=cfg.is_prod,
        cors_origins=origins,
        trading_local_control=trading_local_control,
        lifespan=_lifespan,
    )
