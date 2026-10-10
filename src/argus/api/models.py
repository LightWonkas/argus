"""Dashboard API response contracts (typed, secret-free).

Converts the existing internal types (``SystemStatus``, ``CheckResult``) into web-friendly
pydantic models. Money in TRY+USD, time in UTC ISO-8601; no response ever returns a secret.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus.api.portfolio import PortfolioSummary, PositionValue
from argus.api.quotes import QuoteRow
from argus.core.contracts import AnalysisOutput, OHLCVSeries, Proposal
from argus.core.crypto import (
    BasisSnapshot,
    FundingRate,
    FuturesTicker,
    LiquidationEvent,
    LongShortRatio,
    OpenInterest,
    OrderBookSnapshot,
    RiskAssessment,
    ShadowPosition,
    SpotTicker,
    TradeCandidate,
)
from argus.core.schemas import DataStatus
from argus.observability.health import CheckResult, HealthState
from argus.observability.status import SystemStatus
from argus.storage.base import FeedbackSummary, HitRate, NewsRecord, OutcomeBucket, StoredAnalysis


class LoginRequest(BaseModel):
    """Login body (the password is never echoed back in responses)."""

    model_config = ConfigDict(frozen=True)

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class CheckResultModel(BaseModel):
    """Web view of a single health check."""

    model_config = ConfigDict(frozen=True)

    name: str
    state: HealthState
    detail: str = ""

    @classmethod
    def from_result(cls, result: CheckResult) -> CheckResultModel:
        return cls(name=result.name, state=result.state, detail=result.detail)


class HealthResponse(BaseModel):
    """``GET /api/health`` — aggregate state + component checks."""

    model_config = ConfigDict(frozen=True)

    state: HealthState = Field(..., description="Worst component state (FAIL>DEGRADED>OK)")
    checks: tuple[CheckResultModel, ...] = Field(default_factory=tuple)


class CryptoArtifactView(BaseModel):
    """Derived feature/regime/agent/backtest artifact recorded by the watch pipeline."""

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    data_status: DataStatus
    payload: object


class CryptoDashboardResponse(BaseModel):
    """Crypto cockpit view for one symbol, built only from persisted records.

    The API does not recompute features, proposals or risk decisions in this endpoint.
    Missing sources are carried as ``None`` and ``missing_data``; no zero or demo values are
    produced.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    data_status: DataStatus
    as_of: datetime | None = None
    source: Literal["stored_point_in_time"] = "stored_point_in_time"
    spot: SpotTicker | None = None
    perpetual: FuturesTicker | None = None
    funding: FundingRate | None = None
    open_interest: OpenInterest | None = None
    basis: BasisSnapshot | None = None
    long_short_ratio: LongShortRatio | None = None
    order_book: OrderBookSnapshot | None = None
    liquidations: tuple[LiquidationEvent, ...] = Field(default_factory=tuple)
    quality: CryptoArtifactView | None = None
    features: CryptoArtifactView | None = None
    regime: CryptoArtifactView | None = None
    experts: CryptoArtifactView | None = None
    trade_candidate: TradeCandidate | None = None
    guard: RiskAssessment | None = None
    shadow_positions: tuple[ShadowPosition, ...] = Field(default_factory=tuple)
    backtest: CryptoArtifactView | None = None
    missing_data: tuple[str, ...] = Field(default_factory=tuple)


class StatusResponse(BaseModel):
    """``GET /api/status`` — status bar data (from SystemStatus, secret-free)."""

    model_config = ConfigDict(frozen=True)

    version: str
    env: str
    authorized_count: int
    budget_remaining_usd: float | None = None
    budget_total_usd: float | None = None
    hit_correct: int | None = None
    hit_total: int | None = None
    hit_rate_pct: float | None = Field(default=None, description="Hit rate % (when n>0)")
    degraded_sources: tuple[str, ...] = Field(default_factory=tuple)

    @classmethod
    def from_status(cls, status: SystemStatus) -> StatusResponse:
        rate: float | None = None
        if status.hit_total:  # n>0
            rate = round((status.hit_correct or 0) / status.hit_total * 100, 1)
        return cls(
            version=status.version,
            env=status.env,
            authorized_count=status.authorized_count,
            budget_remaining_usd=status.budget_remaining_usd,
            budget_total_usd=status.budget_total_usd,
            hit_correct=status.hit_correct,
            hit_total=status.hit_total,
            hit_rate_pct=rate,
            degraded_sources=status.degraded_sources,
        )


class AnalysisListItem(BaseModel):
    """``GET /api/analyses`` list item — summary (for the card table)."""

    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    symbol: str
    timeframe: str
    bias: str | None
    evidence_tier: str
    confidence_pct: int
    summary: str

    @classmethod
    def from_stored(cls, stored: StoredAnalysis) -> AnalysisListItem:
        a = stored.analysis
        return cls(
            id=stored.id,
            created_at=stored.created_at,
            symbol=a.symbol,
            timeframe=a.timeframe.value,
            bias=a.bias,
            evidence_tier=a.evidence_tier.value,
            confidence_pct=a.confidence_pct,
            summary=a.summary,
        )


class AnalysisDetail(BaseModel):
    """``GET /api/analyses/{id}`` — full analysis output + metadata."""

    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    analysis: AnalysisOutput

    @classmethod
    def from_stored(cls, stored: StoredAnalysis) -> AnalysisDetail:
        return cls(id=stored.id, created_at=stored.created_at, analysis=stored.analysis)


class MetricStat(BaseModel):
    """Hit counter for a single metric (touch|close)."""

    model_config = ConfigDict(frozen=True)

    hit: int = 0
    n: int = 0
    pct: float | None = Field(default=None, description="Hit % (when n>0)")

    @classmethod
    def from_bucket(cls, bucket: OutcomeBucket | None) -> MetricStat:
        if bucket is None or bucket.total == 0:
            return cls()
        return cls(hit=bucket.correct, n=bucket.total, pct=round(bucket.rate * 100, 1))


class VariantStats(BaseModel):
    """Two-metric counter of one baseline (random|bullish|momentum)."""

    model_config = ConfigDict(frozen=True)

    touch: MetricStat = Field(default_factory=MetricStat)
    close: MetricStat = Field(default_factory=MetricStat)


class SeriesStats(BaseModel):
    """Baseline-relative statistics of one series (LLM analysis | confluence proposal).

    ``delta_pts``: per metric, system% − random-baseline% (when both sides have n>0; else
    null). A bare percentage is MEANINGLESS ON ITS OWN — the cockpit speaks in "+X points vs
    baseline".
    """

    model_config = ConfigDict(frozen=True)

    touch: MetricStat = Field(default_factory=MetricStat)
    close: MetricStat = Field(default_factory=MetricStat)
    baselines: dict[str, VariantStats] = Field(default_factory=dict)
    delta_pts: dict[str, float | None] = Field(default_factory=dict)
    low_n: bool = True  # n < min_n → statistically meaningless


class HitRateResponse(BaseModel):
    """``GET /api/hitrate`` v2 — baseline-relative, dual-metric hit rate (backward-compatible).

    The v1 fields (``correct/total/rate_pct``) are the close×system counter of the
    LLM-analysis series (older cockpit/StatusBar clients keep working). v2: ``series.llm`` +
    ``series.proposal`` + ``low_n``.
    """

    model_config = ConfigDict(frozen=True)

    correct: int
    total: int
    rate_pct: float | None = Field(default=None, description="Hit rate % (when n>0)")
    series: dict[str, SeriesStats] = Field(default_factory=dict)
    low_n: bool = True
    min_n: int = 30

    @classmethod
    def from_hit_rate(cls, hr: HitRate) -> HitRateResponse:
        return cls(
            correct=hr.correct,
            total=hr.total,
            rate_pct=round(hr.rate * 100, 1) if hr.total else None,
        )

    @classmethod
    def from_buckets(
        cls, v1: HitRate, buckets: list[OutcomeBucket], *, min_n: int = 30
    ) -> HitRateResponse:
        """Builds the baseline-relative response from store buckets (ref_type → series name)."""
        by_key = {(b.ref_type, b.metric, b.variant): b for b in buckets}
        series: dict[str, SeriesStats] = {}
        for ref_type, name in (("analysis", "llm"), ("proposal", "proposal")):
            sys_touch = MetricStat.from_bucket(by_key.get((ref_type, "touch", "system")))
            sys_close = MetricStat.from_bucket(by_key.get((ref_type, "close", "system")))
            baselines = {
                variant: VariantStats(
                    touch=MetricStat.from_bucket(by_key.get((ref_type, "touch", variant))),
                    close=MetricStat.from_bucket(by_key.get((ref_type, "close", variant))),
                )
                for variant in ("random", "bullish", "momentum")
            }
            delta: dict[str, float | None] = {}
            for metric, sys_stat in (("touch", sys_touch), ("close", sys_close)):
                rnd = baselines["random"].touch if metric == "touch" else baselines["random"].close
                delta[metric] = (
                    round(sys_stat.pct - rnd.pct, 1)
                    if sys_stat.pct is not None and rnd.pct is not None
                    else None
                )
            series[name] = SeriesStats(
                touch=sys_touch,
                close=sys_close,
                baselines=baselines,
                delta_pts=delta,
                low_n=max(sys_touch.n, sys_close.n) < min_n,
            )
        return cls(
            correct=v1.correct,
            total=v1.total,
            rate_pct=round(v1.rate * 100, 1) if v1.total else None,
            series=series,
            low_n=all(s.low_n for s in series.values()),
            min_n=min_n,
        )


class FeedbackCountModel(BaseModel):
    """Vote counter of one breakdown key."""

    model_config = ConfigDict(frozen=True)

    useful: int
    total: int


class FeedbackSummaryResponse(BaseModel):
    """``GET /api/feedback/summary`` — the user-value metric.

    Whether alerts/analyses are useful: 👍 ratio + symbol/severity breakdown. With no votes
    ``useful_pct`` is null (no made-up zero).
    """

    model_config = ConfigDict(frozen=True)

    useful: int
    total: int
    useful_pct: float | None
    days: int
    by_symbol: dict[str, FeedbackCountModel]
    by_severity: dict[str, FeedbackCountModel]

    @classmethod
    def from_summary(cls, s: FeedbackSummary) -> FeedbackSummaryResponse:
        return cls(
            useful=s.useful,
            total=s.total,
            useful_pct=s.useful_pct,
            days=s.days,
            by_symbol={
                k: FeedbackCountModel(useful=v.useful, total=v.total)
                for k, v in s.by_symbol.items()
            },
            by_severity={
                k: FeedbackCountModel(useful=v.useful, total=v.total)
                for k, v in s.by_severity.items()
            },
        )


class AlertItem(BaseModel):
    """``GET /api/alerts`` item — latest alert/trigger derived from the analysis journal."""

    model_config = ConfigDict(frozen=True)

    analysis_id: int
    created_at: datetime
    symbol: str
    severity: str
    kind: str = Field(..., description="Trigger type (rsi/price_move/atr/ema_cross…)")
    message: str


class FeedbackRequest(BaseModel):
    """``POST /api/feedback`` body — a 👍/👎 vote from the web cockpit.

    Writes to the same store as Telegram. ``ref_type``/``ref_id`` point at the voted record
    (e.g. ``analysis`` + analysis id). ``useful=True`` → useful, ``False`` → not useful.
    """

    model_config = ConfigDict(frozen=True)

    ref_type: Literal["analysis", "proposal"] = "analysis"
    ref_id: str = Field(..., min_length=1)
    useful: bool
    symbol: str | None = None
    severity: str | None = None


class FeedbackAck(BaseModel):
    """``POST /api/feedback`` response — vote recorded (short acknowledgement)."""

    model_config = ConfigDict(frozen=True)

    ok: bool = True
    useful: bool


class QuoteItem(BaseModel):
    """``GET /api/quotes`` item — watchlist table (price + indicators + freshness)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    asset_class: str
    tier: str
    price: float | None = None
    change_pct_24h: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    atr: float | None = None
    freshness: str = Field(..., description="fresh | stale | none (DATA PENDING)")
    bar_time: datetime | None = None
    bar_count: int = 0
    timeframes: tuple[str, ...] = Field(
        default=(), description="Timeframes defined in config (the chart selector offers these)"
    )

    @classmethod
    def from_row(cls, row: QuoteRow) -> QuoteItem:
        return cls(
            symbol=row.symbol,
            asset_class=row.asset_class,
            tier=row.tier,
            price=row.price,
            change_pct_24h=row.change_pct_24h,
            rsi=row.rsi,
            macd=row.macd,
            macd_signal=row.macd_signal,
            atr=row.atr,
            freshness=row.freshness,
            bar_time=row.bar_time,
            bar_count=row.bar_count,
            timeframes=row.timeframes,
        )


class CostResponse(BaseModel):
    """``GET /api/cost`` — daily LLM spend vs cap + line-item breakdown.

    Source: the persistent cost ledger (``llm_cost``) → the TOTAL across the watch daemon,
    bot, cockpit and poller processes. Projection: month-to-date total / days elapsed × days
    in month (rough, and labelled as such).
    """

    model_config = ConfigDict(frozen=True)

    day: str
    budget_usd: float
    spent_today_usd: float
    remaining_today_usd: float
    tokens_usd: float
    search_usd: float = Field(..., description="Web search line item ($0.01/search)")
    by_role: dict[str, float] = Field(default_factory=dict)
    month_to_date_usd: float
    month_projection_usd: float
    monthly_limit_usd: float = 100.0  # target: ≤ $100/month
    # Daily totals of the last 7 days (oldest→newest) — the cockpit mini trend.
    last_days: tuple[tuple[str, float], ...] = ()


class NewsResponseItem(BaseModel):
    """``GET /api/news`` item — archived news/KAP disclosure + router label.

    ``route_status``: null = not labelled · "ok" = labelled · "budget_exceeded"/"error" =
    could not be evaluated (label fields stay null — no fake label).
    """

    model_config = ConfigDict(frozen=True)

    source: str
    kind: str = Field(..., description="news | disclosure")
    title: str
    url: str | None
    symbol: str | None
    published_at: datetime
    importance: str | None = None
    relevant: bool | None = None
    target_agent: str | None = None
    confidence_pct: int | None = None
    route_status: str | None = None

    @classmethod
    def from_record(cls, r: NewsRecord) -> NewsResponseItem:
        return cls(
            source=r.source,
            kind=r.kind,
            title=r.title,
            url=r.url,
            symbol=r.symbol,
            published_at=r.published_at,
            importance=r.importance,
            relevant=r.relevant,
            target_agent=r.target_agent,
            confidence_pct=r.confidence_pct,
            route_status=r.route_status,
        )


class PositionItem(BaseModel):
    """``GET /api/portfolio`` holding item — value/P&L (TRY+USD)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    asset_class: str
    owner: str
    quantity: float
    avg_cost: float
    cost_currency: str
    price: float | None
    value_try: float | None
    value_usd: float | None
    pnl_try: float | None
    pnl_usd: float | None
    pnl_pct: float | None

    @classmethod
    def from_value(cls, v: PositionValue) -> PositionItem:
        return cls(
            symbol=v.symbol,
            asset_class=v.asset_class,
            owner=v.owner,
            quantity=v.quantity,
            avg_cost=v.avg_cost,
            cost_currency=v.cost_currency,
            price=v.price,
            value_try=v.value_try,
            value_usd=v.value_usd,
            pnl_try=v.pnl_try,
            pnl_usd=v.pnl_usd,
            pnl_pct=v.pnl_pct,
        )


class PortfolioResponse(BaseModel):
    """``GET /api/portfolio`` — holdings + totals (TRY+USD). Empty list without holdings.

    There is no execution; holdings are entered manually in config, so value/P&L is
    config-based until a live portfolio state is connected.
    """

    model_config = ConfigDict(frozen=True)

    connected: bool = Field(..., description="Whether portfolio data exists (holdings are defined)")
    positions: tuple[PositionItem, ...] = Field(default_factory=tuple)
    total_value_try: float | None = None
    total_value_usd: float | None = None
    total_pnl_try: float | None = None
    total_pnl_usd: float | None = None

    @classmethod
    def from_summary(cls, summary: PortfolioSummary) -> PortfolioResponse:
        return cls(
            connected=bool(summary.positions),
            positions=tuple(PositionItem.from_value(p) for p in summary.positions),
            total_value_try=summary.total_value_try,
            total_value_usd=summary.total_value_usd,
            total_pnl_try=summary.total_pnl_try,
            total_pnl_usd=summary.total_pnl_usd,
        )


class OHLCVCandle(BaseModel):
    """A single candle — lightweight-charts format (``time`` = unix seconds, UTC)."""

    model_config = ConfigDict(frozen=True)

    time: int  # epoch seconds (UTC) — expected by TradingView/lightweight-charts
    open: float
    high: float
    low: float
    close: float
    volume: float


class OHLCVResponse(BaseModel):
    """``GET /api/ohlcv/{symbol}`` — candle array for the chart (secret-free)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    candles: tuple[OHLCVCandle, ...] = Field(default_factory=tuple)

    @classmethod
    def from_series(cls, series: OHLCVSeries) -> OHLCVResponse:
        candles = tuple(
            OHLCVCandle(
                time=int(b.timestamp.timestamp()),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in series.bars
        )
        return cls(symbol=series.symbol, timeframe=series.timeframe.value, candles=candles)


class SparkItem(BaseModel):
    """``GET /api/ohlcv-batch`` item — last N closes for the watchlist table sparkline.

    Not full OHLCV (a sparkline only draws closes) → N symbols arrive in one request instead
    of one call per symbol. With no data ``closes`` is empty (no line is drawn; — is shown).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    closes: tuple[float, ...] = Field(default_factory=tuple)


class SparkBatchResponse(BaseModel):
    """``GET /api/ohlcv-batch`` — sparkline closes of several symbols (one request)."""

    model_config = ConfigDict(frozen=True)

    items: tuple[SparkItem, ...] = Field(default_factory=tuple)


class ContributorView(BaseModel):
    """One agent's contribution to the confluence (transparency — who said what)."""

    model_config = ConfigDict(frozen=True)

    agent: str
    stance: str  # bullish | bearish | neutral | no_data
    confidence_pct: int


class MeasurementView(BaseModel):
    """Proposal measurement threshold — **NOT a target/stop** (no price targets are produced).

    The deterministic levels hit rate v2 will test this proposal against: reference (close
    at proposal time) ± ``k_atr × ATR``. The chart draws them as "measurement thresholds".
    """

    model_config = ConfigDict(frozen=True)

    ref_price: float  # last close at proposal time (PIT reference)
    ref_time: int  # epoch seconds (last candle)
    atr: float
    atr_period: int
    k_atr: float
    threshold_abs: float  # k_atr × atr (absolute price distance)
    horizon_bars: int  # horizon: the threshold is tested within this many candles


class ProposalResponse(BaseModel):
    """``GET /api/proposal/{symbol}`` — L4 confluence proposal (secret-free, **not advice**)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    direction: str  # bullish | bearish | neutral | no_data
    strength: int
    confluence_score: int
    rationale: str
    contributors: tuple[ContributorView, ...] = Field(default_factory=tuple)
    data_status: str  # ok | pending
    disclaimer: str
    # None when ATR cannot be computed (too few candles) — no threshold is made up.
    measurement: MeasurementView | None = None

    @classmethod
    def from_proposal(cls, p: Proposal) -> ProposalResponse:
        return cls(
            symbol=p.symbol,
            timeframe=p.timeframe.value,
            direction=p.direction.value,
            strength=p.strength,
            confluence_score=p.confluence_score,
            rationale=p.rationale,
            contributors=tuple(
                ContributorView(
                    agent=c.agent, stance=c.stance.value, confidence_pct=c.confidence_pct
                )
                for c in p.contributors
            ),
            data_status=p.data_status.value,
            disclaimer=p.disclaimer,
        )
