// Argus dashboard API client — calls the backend /api endpoints (Vite proxy → same origin).
// Cookie-based session: every request uses credentials="include".

export interface StatusResponse {
  version: string;
  env: string;
  authorized_count: number;
  budget_remaining_usd: number | null;
  budget_total_usd: number | null;
  hit_correct: number | null;
  hit_total: number | null;
  hit_rate_pct: number | null;
  degraded_sources: string[];
}

export type HealthState = "ok" | "degraded" | "fail";

export interface CheckResult {
  name: string;
  state: HealthState;
  detail: string;
}

export interface HealthResponse {
  state: HealthState;
  checks: CheckResult[];
}

export type TradingMode = "public_monitor" | "shadow" | "testnet" | "live_spot";
export type DataStatus = "ok" | "pending" | "quarantined" | "rejected";

export interface TradingMission {
  mission_id: string;
  status: string;
  mode: TradingMode;
  quote_asset: "USDT";
  display_currency: "TRY";
  input_budget_try: string | null;
  try_per_usdt: string | null;
  conversion_rate_source: string | null;
  conversion_rate_at: string | null;
  capital_limit: string;
  starting_equity: string;
  available_mission_cash: string;
  capital_limit_usdt: string | null;
  starting_equity_usdt: string | null;
  current_equity_usdt: string | null;
  available_cash_usdt: string | null;
  daily_starting_equity_usdt: string | null;
  daily_realized_net_pnl: string;
  daily_profit_stop_pct: string;
  daily_loss_stop_pct: string;
  maximum_drawdown_pct: string;
  timezone: string;
  created_at: string;
  started_at: string | null;
  scheduled_end_at: string;
  actual_end_at: string | null;
  end_policy: string;
  armed_at: string | null;
  halted_at: string | null;
  halt_reason: string | null;
  revision: number;
}

export interface TradingStatusResponse {
  mode: TradingMode;
  armed: boolean;
  kill_switch: boolean;
  active_mission_id: string | null;
  active_mission_status: string | null;
  public_market_data_available: boolean;
  private_account_available: boolean;
  user_data_stream_available: boolean;
  spot_execution_available: boolean;
  live_execution_enabled: boolean;
  execution_backend: string;
  source: "stored_point_in_time";
  data_status: DataStatus;
  reasons: string[];
}

export interface TradingAccountResponse {
  status: string;
  connected: boolean;
  snapshot: Record<string, unknown> | null;
  reasons: string[];
  secret_exposed: false;
}

export interface UniverseSnapshot {
  symbol: string;
  discovered_at: string;
  eligible: boolean;
  rejection_reasons: string[];
  quote_volume_24h: string | null;
  spread_bps: string | null;
  estimated_depth_quote: string | null;
  estimated_slippage_bps: string | null;
  realized_volatility_pct: string | null;
  quality_status: DataStatus;
}

export interface TradingUniverseResponse {
  as_of: string | null;
  scanned_count: number;
  eligible_count: number;
  returned_count: number;
  page_limit: number;
  next_cursor: string | null;
  snapshots: UniverseSnapshot[];
  data_status: DataStatus;
  source: "stored_point_in_time";
}

export interface ResearchScore {
  symbol: string;
  as_of: string;
  status: string;
  final_research_score: string | null;
  evidence: string[];
  risk_flags: string[];
  data_freshness_s: string | null;
}

export interface TradingResearchResponse {
  scores: ResearchScore[];
  coin_results: CoinResearchResult[];
  data_status: DataStatus;
  source: "stored_point_in_time";
}

export interface CoinResearchResult {
  research_id: string;
  symbol: string;
  as_of: string;
  eligibility: boolean;
  strategy_basis: "news_catalyst" | "technical_intraday" | "no_trade";
  news_coverage_status: string;
  market_regime: string;
  technical_score: string | null;
  news_score: string | null;
  news_reliability: string | null;
  market_reaction: string;
  liquidity_score: string | null;
  manipulation_risk: string | null;
  execution_quality: string | null;
  confidence: string;
  evidence: string[];
  rejection_reasons: string[];
  valid_until: string;
  data_status: DataStatus;
}

export interface CryptoNewsRecord {
  news_id: string;
  canonical_url: string;
  provider: string;
  source_name: string;
  source_reliability: string;
  title: string;
  short_summary: string | null;
  published_at: string;
  first_seen_at: string;
  related_symbols: string[];
  entity_matches: Array<{
    symbol: string;
    entity_confidence: string;
    match_evidence: string[];
    ambiguous: boolean;
    rejection_reason: string | null;
  }>;
  event_type: string;
  sentiment: string;
  impact_score: string;
  confidence: string;
  verification_status: string;
  risk_flags: string[];
  data_status: DataStatus;
}

export interface NewsMarketReaction {
  reaction_id: string;
  news_id: string;
  symbol: string;
  as_of: string;
  status: string;
  blocks_entry: boolean;
  reasons: string[];
  data_status: DataStatus;
}

export interface TradingNewsResponse {
  records: CryptoNewsRecord[];
  reactions: NewsMarketReaction[];
  provider_health: Record<string, unknown> | null;
  data_status: DataStatus;
  source: "stored_point_in_time";
}

export interface IntelligenceSource {
  schema_version: "source-registry-v1";
  source_id: string;
  display_name: string;
  source_group: string;
  official_domain: string;
  feed_type: string;
  feed_url: string | null;
  enabled: boolean;
  availability: "available" | "unavailable" | "manual_configuration_required";
  priority: number;
  trust_level: string;
  supported_event_types: string[];
  symbol_scope: string[];
  last_success_at: string | null;
  last_failure_at: string | null;
  consecutive_failures: number;
  health_status: string;
  fetch_latency_ms: number | null;
}

export interface NormalizedCryptoEvent {
  schema_version: "normalized-crypto-event-v1";
  event_id: string;
  document_ids: string[];
  event_type: string;
  event_status: string;
  affected_symbols: string[];
  affected_projects: string[];
  affected_networks: string[];
  affected_exchanges: string[];
  announced_at: string | null;
  first_seen_at: string;
  expires_at: string;
  direction: string;
  severity: string;
  urgency: string;
  confidence: string;
  source_trust: string;
  corroboration_count: number;
  facts: string[];
  uncertainties: string[];
  evidence: Array<{
    document_id: string;
    source_id: string;
    canonical_url: string;
    evidence_role: string;
    independent_origin: string;
  }>;
  data_status: DataStatus;
}

export interface IntelligencePage<T> {
  items: T[];
  returned_count?: number;
  total_count?: number;
  limit?: number;
  offset?: number;
  data_status: DataStatus | "no_data";
}

export interface IntelligenceHealth {
  checked_at: string;
  source_status_counts: Record<string, number>;
  stale_source_count: number;
  unavailable_source_count: number;
  event_generation_count: number;
  market_reaction_pending_count: number;
  queue_depth: number;
  database_bytes: number | null;
  system_mode: string;
  live_execution_enabled: false;
  private_endpoints_enabled: false;
  data_status: DataStatus | "no_data";
}

export interface TradePlan {
  plan_id: string;
  mission_id: string;
  symbol: string;
  strategy_basis: "news_catalyst" | "technical_intraday" | "no_trade";
  created_at: string;
  valid_until: string;
  horizon: string;
  expected_holding_minutes: number;
  entry_condition: string;
  expected_entry_price: string | null;
  stop_price: string | null;
  expected_gross_return_bps: string | null;
  expected_entry_cost_bps: string | null;
  expected_exit_cost_bps: string | null;
  estimated_round_trip_cost_bps: string | null;
  expected_net_return_bps: string | null;
  confidence: string;
  news_evidence: string[];
  technical_evidence: string[];
  risk_flags: string[];
  rejection_reasons: string[];
  cost_source: "verified_account" | "conservative_estimate" | "no_data";
  live_eligible: false;
  data_status: DataStatus;
  submission_authorized: false;
}

export interface TradingPlansResponse {
  plans: TradePlan[];
  data_status: DataStatus;
  source: "stored_point_in_time";
}

export interface TradingHealthResponse {
  checked_at: string;
  overall: "ok" | "degraded" | "fail";
  last_market_data_at: string | null;
  public_market_data: DataStatus;
  news_providers: DataStatus;
  private_account: "no_data";
  user_data_stream: "no_data";
  reconciliation: "no_data";
  execution: "disabled";
  storage: TradingStorageResponse["health"];
  reasons: string[];
}

export interface TradingWatchlistResponse {
  symbols: string[];
  changed_at: string | null;
  dynamic_discovery_enabled: boolean;
  data_status: DataStatus;
  source: "stored_checkpoint" | "configured_fallback";
}

export interface MissionListResponse {
  missions: TradingMission[];
}

export interface TradingPositionsResponse {
  mission_id: string | null;
  mission_inventory: Record<string, unknown>[];
  shadow_positions: Record<string, unknown>[];
  data_status: DataStatus;
  reason: string;
}

export interface TradingOrdersResponse {
  mission_id: string | null;
  intents: Record<string, unknown>[];
  exchange_orders: Record<string, unknown>[];
  data_status: DataStatus;
  reason: string;
}

export interface TradingPerformanceResponse {
  mission_id: string | null;
  gross_pnl: string | null;
  net_pnl: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  total_commission: string | null;
  estimated_slippage: string | null;
  turnover: string | null;
  fee_drag_pct: string | null;
  data_status: DataStatus;
  reason: string;
}

export interface TradingCostsResponse {
  schedules: {
    symbol: string;
    maker_rate: string | null;
    taker_rate: string | null;
    source: string;
    fetched_at: string;
    valid_until: string;
    discount_verified: boolean;
    commission_asset: string | null;
  }[];
  conservative_estimate_enabled: boolean;
  conservative_maker_bps: string | null;
  conservative_taker_bps: string | null;
  uncertainty_buffer_bps: string | null;
  live_eligible: false;
  data_status: DataStatus;
  reason: string;
}

export interface TradingRiskResponse {
  kill_switch: boolean;
  guard_results: {
    result_id: string;
    mission_id: string;
    symbol: string;
    assessed_at: string;
    decision: string;
    reasons: string[];
  }[];
  data_status: DataStatus;
  reason: string;
}

export interface TradingStorageResponse {
  health: {
    checked_at: string;
    database_bytes: number | null;
    wal_bytes: number | null;
    free_disk_bytes: number | null;
    raw_trade_events: number;
    raw_depth_events: number;
    retention_backlog_events: number;
    retention_enabled: boolean;
    healthy: boolean;
    reasons: string[];
  };
  last_retention: {
    started_at: string;
    finished_at: string;
    enabled: boolean;
    success: boolean;
    trade_events_deleted: number;
    book_snapshots_deleted: number;
    book_deltas_deleted: number;
    reason: string | null;
  } | null;
}

export interface MissionMutationResponse {
  mission: TradingMission;
  message: string;
}

export interface AnalysisListItem {
  id: number;
  created_at: string;
  symbol: string;
  timeframe: string;
  bias: string | null;
  evidence_tier: string;
  confidence_pct: number;
  summary: string;
}

export interface Scenario {
  name: string;
  probability_pct: number;
  detail: string;
}

export interface AnalysisOutput {
  symbol: string;
  timeframe: string;
  summary: string;
  bias: string | null;
  evidence_tier: string;
  confidence_pct: number;
  invalidation: string;
  scenarios: Scenario[];
  model: string;
  disclaimer: string;
}

export interface AnalysisDetail {
  id: number;
  created_at: string;
  analysis: AnalysisOutput;
}

export interface MetricStat {
  hit: number;
  n: number;
  pct: number | null;
}

export interface VariantStats {
  touch: MetricStat;
  close: MetricStat;
}

/** Hit-rate series: points versus baseline; a bare percentage is never reported alone. */
export interface SeriesStats {
  touch: MetricStat;
  close: MetricStat;
  baselines: Record<string, VariantStats>; // random | bullish | momentum
  delta_pts: Record<string, number | null>; // metric → system% − random%
  low_n: boolean;
}

export interface HitRateResponse {
  correct: number;
  total: number;
  rate_pct: number | null;
  series: Record<string, SeriesStats>; // llm | proposal
  low_n: boolean;
  min_n: number;
}

export interface FeedbackCount {
  useful: number;
  total: number;
}

/** Daily LLM spend versus the cap — token + web search breakdown. */
export interface CostResponse {
  day: string;
  budget_usd: number;
  spent_today_usd: number;
  remaining_today_usd: number;
  tokens_usd: number;
  search_usd: number;
  by_role: Record<string, number>;
  month_to_date_usd: number;
  month_projection_usd: number;
  monthly_limit_usd: number;
  last_days: [string, number][]; // last 7 days (oldest→newest): [day, USD]
}

/** User value — are the alerts/analyses useful (👍/👎). */
export interface FeedbackSummary {
  useful: number;
  total: number;
  useful_pct: number | null;
  days: number;
  by_symbol: Record<string, FeedbackCount>;
  by_severity: Record<string, FeedbackCount>;
}

export interface AlertItem {
  analysis_id: number;
  created_at: string;
  symbol: string;
  severity: string;
  kind: string;
  message: string;
}

/** POST /api/feedback body — 👍/👎 from the web panel; same store as Telegram. */
export interface FeedbackVote {
  ref_type: "analysis" | "proposal";
  ref_id: string;
  useful: boolean;
  symbol?: string | null;
  severity?: string | null;
}

export interface FeedbackAck {
  ok: boolean;
  useful: boolean;
}

export interface QuoteItem {
  symbol: string;
  asset_class: string;
  tier: string;
  price: number | null;
  change_pct_24h: number | null;
  rsi: number | null;
  macd: number | null;
  macd_signal: number | null;
  atr: number | null;
  freshness: "fresh" | "stale" | "none";
  bar_time: string | null;
  bar_count: number;
  timeframes: string[]; // timeframes defined in config (the chart selector offers only these)
}

export interface OHLCVCandle {
  time: number; // epoch seconds (lightweight-charts)
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OHLCVResponse {
  symbol: string;
  timeframe: string;
  candles: OHLCVCandle[];
}

/** GET /api/ohlcv-batch — last N closes for the watchlist sparklines (single request). */
export interface SparkItem {
  symbol: string;
  closes: number[];
}

export interface SparkBatchResponse {
  items: SparkItem[];
}

export interface ContributorView {
  agent: string;
  stance: "bullish" | "bearish" | "neutral" | "no_data";
  confidence_pct: number;
}

/** Proposal measurement threshold — NOT a target/stop; the ref ± k×ATR levels the hit rate tests. */
export interface MeasurementView {
  ref_price: number;
  ref_time: number;
  atr: number;
  atr_period: number;
  k_atr: number;
  threshold_abs: number;
  horizon_bars: number;
}

export interface ProposalResponse {
  symbol: string;
  timeframe: string;
  direction: "bullish" | "bearish" | "neutral" | "no_data";
  strength: number;
  confluence_score: number;
  rationale: string;
  contributors: ContributorView[];
  data_status: "ok" | "pending";
  disclaimer: string;
  measurement: MeasurementView | null;
}

export type CryptoDataStatus = "ok" | "pending" | "quarantined" | "rejected";
export type NumericValue = number | string;

export interface CryptoRecordMeta {
  exchange: string;
  market_type: "spot" | "perpetual";
  symbol: string;
  event_timestamp: string;
  ingestion_timestamp: string;
  source: string;
  confidence: number;
  freshness: "fresh" | "stale" | "expired";
  sequence: number | null;
  data_status: CryptoDataStatus;
  representation: "raw" | "transformed";
}

export interface CryptoSpotTicker extends CryptoRecordMeta {
  last_price: NumericValue;
  bid: NumericValue | null;
  ask: NumericValue | null;
  base_volume_24h: NumericValue | null;
  quote_volume_24h: NumericValue | null;
}

export interface CryptoFuturesTicker extends CryptoRecordMeta {
  last_price: NumericValue;
  mark_price: NumericValue | null;
  index_price: NumericValue | null;
  bid: NumericValue | null;
  ask: NumericValue | null;
  open_interest: NumericValue | null;
  funding_rate: NumericValue | null;
}

export interface CryptoDerivativeRecord extends CryptoRecordMeta {
  rate?: NumericValue;
  contracts?: NumericValue | null;
  base_value?: NumericValue | null;
  quote_value?: NumericValue | null;
  spot_price?: NumericValue;
  perpetual_price?: NumericValue;
  long_ratio?: NumericValue;
  short_ratio?: NumericValue;
  account_ratio?: NumericValue | null;
}

export interface CryptoOrderBook extends CryptoRecordMeta {
  bids: { price: NumericValue; quantity: NumericValue }[];
  asks: { price: NumericValue; quantity: NumericValue }[];
}

export interface CryptoLiquidation extends CryptoRecordMeta {
  liquidation_id: string;
  side: "buy" | "sell";
  price: NumericValue;
  quantity: NumericValue;
}

export interface CryptoArtifact {
  as_of: string;
  data_status: CryptoDataStatus;
  payload: unknown;
}

export interface CryptoTradeCandidate {
  candidate_id: string;
  symbol: string;
  side: "buy" | "sell";
  created_at: string;
  horizon: string;
  entry_reference: NumericValue;
  stop_price: NumericValue | null;
  invalidation: string;
  confidence_pct: number;
  execution_enabled: false;
  human_approval_required: true;
}

export interface CryptoGuardAssessment {
  candidate_id: string;
  assessed_at: string;
  decision: "allow_shadow" | "require_human_review" | "reject" | "halt_system";
  reasons: string[];
  human_review_required: boolean;
}

export interface CryptoShadowPosition {
  position_id: string;
  symbol: string;
  status: "open" | "closed";
  quantity: NumericValue;
  average_entry_price: NumericValue;
  mark_price: NumericValue;
  realized_pnl: NumericValue;
  unrealized_pnl: NumericValue;
  fees: NumericValue;
  funding_cost: NumericValue;
}

/** Built from persisted point-in-time records; the API never invents values for null fields. */
export interface CryptoDashboardResponse {
  symbol: string;
  data_status: CryptoDataStatus;
  as_of: string | null;
  source: "stored_point_in_time";
  spot: CryptoSpotTicker | null;
  perpetual: CryptoFuturesTicker | null;
  funding: CryptoDerivativeRecord | null;
  open_interest: CryptoDerivativeRecord | null;
  basis: CryptoDerivativeRecord | null;
  long_short_ratio: CryptoDerivativeRecord | null;
  order_book: CryptoOrderBook | null;
  liquidations: CryptoLiquidation[];
  quality: CryptoArtifact | null;
  features: CryptoArtifact | null;
  regime: CryptoArtifact | null;
  experts: CryptoArtifact | null;
  trade_candidate: CryptoTradeCandidate | null;
  guard: CryptoGuardAssessment | null;
  shadow_positions: CryptoShadowPosition[];
  backtest: CryptoArtifact | null;
  missing_data: string[];
}

export interface NewsResponseItem {
  source: string;
  kind: "news" | "disclosure";
  title: string;
  url: string | null;
  symbol: string | null;
  published_at: string;
  // News-router label: route_status null = not labelled; "ok" = labelled;
  // "budget_exceeded"|"error" = could not be evaluated (labels stay null).
  importance: string | null;
  relevant: boolean | null;
  target_agent: string | null;
  confidence_pct: number | null;
  route_status: string | null;
}

export interface PositionItem {
  symbol: string;
  asset_class: string;
  owner: string;
  quantity: number;
  avg_cost: number;
  cost_currency: string;
  price: number | null;
  value_try: number | null;
  value_usd: number | null;
  pnl_try: number | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
}

export interface PortfolioResponse {
  connected: boolean;
  positions: PositionItem[];
  total_value_try: number | null;
  total_value_usd: number | null;
  total_pnl_try: number | null;
  total_pnl_usd: number | null;
}

// ── Settings ────────────────────────────────────────────────────────────────

export interface SecretStatus {
  set: boolean;
  last4: string | null;
}

export interface WatchlistItemT {
  symbol: string;
  asset_class: string;
  tier: "holdings" | "watchlist" | "broad";
  note: string;
}

export interface HoldingItemT {
  symbol: string;
  asset_class: string;
  quantity: number;
  avg_cost: number;
  cost_currency: "TRY" | "USD";
  owner: string;
}

export interface TriggerThresholds {
  rsi_period: number;
  rsi_overbought: number;
  rsi_oversold: number;
  price_move_pct: number;
  atr_period: number;
  atr_spike_mult: number;
  ema_fast: number;
  ema_slow: number;
}

export interface AlertConfig {
  min_instant_severity: string;
  quiet_start: string | null;
  quiet_end: string | null;
  quiet_tz: string;
  critical_overrides_quiet: boolean;
  briefing_interval_s: number;
}

export interface RoleModel {
  role: string;
  label: string;
  provider: string; // claude | ollama | openai | openrouter | "—" (planned)
  model: string;
  active: boolean;
}

export interface LLMSettingsView {
  anthropic_api_key: SecretStatus;
  openai_api_key: SecretStatus;
  openrouter_api_key: SecretStatus;
  llm_provider: "claude" | "ollama";
  providers: string[]; // UI dropdown — supported providers
  live_providers: string[]; // providers live at runtime (claude/ollama)
  ollama_base_url: string;
  ollama_model: string;
  model_routine: string;
  model_deep: string;
  model_copilot: string;
  llm_daily_budget_usd: number;
  roles: RoleModel[];
}

export interface SettingsView {
  llm: LLMSettingsView;
  telegram: { bot_token: SecretStatus; authorized_chat_ids: number[] };
  watch: { watchlist: WatchlistItemT[]; holdings: HoldingItemT[] };
  triggers: { triggers: TriggerThresholds; alerts: AlertConfig };
  data: { evds_key: SecretStatus; cryptopanic_token: SecretStatus; finnhub_key: SecretStatus };
}

/** Data source key update — write-only (null = leave untouched). */
export interface DataKeysUpdate {
  evds_key?: string | null;
  cryptopanic_token?: string | null;
  finnhub_key?: string | null;
}

export interface SettingsWriteResult {
  ok: boolean;
  requires_restart: boolean;
  message: string;
}

export interface LLMUpdate {
  anthropic_api_key?: string | null;
  openai_api_key?: string | null;
  openrouter_api_key?: string | null;
  llm_provider?: "claude" | "ollama";
  ollama_base_url?: string;
  ollama_model?: string;
  provider_routine?: string;
  provider_deep?: string;
  provider_copilot?: string;
  provider_news_router?: string;
  provider_exchange_expert?: string; // BIST exchange expert
  model_routine?: string;
  model_deep?: string;
  model_copilot?: string;
  model_news_router?: string;
  model_exchange_expert?: string;
  llm_daily_budget_usd?: number;
}

export interface TelegramUpdate {
  bot_token?: string | null;
  authorized_chat_ids?: number[];
}

// ── Argus Copilot (chat) ────────────────────────────────────────────────────

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatResponse {
  reply: string;
  model: string;
  cost_usd: number;
  demo: boolean;
  status: "ok" | "budget_exceeded" | "error"; // explicit status when no reply could be produced
  disclaimer: string;
}

export interface ChatTurnView {
  role: string;
  content: string;
  created_at: string;
}

// ── News router (classify a single item) ─────────────────────────────────────

export interface NewsRouteRequest {
  title: string;
  summary?: string | null;
  source?: string | null;
  symbols?: string[];
}

/** status != "ok" → label fields are null (could not be evaluated; no invented labels). */
export interface NewsRouteResult {
  status: "ok" | "budget_exceeded" | "error";
  importance: string | null; // info | low | medium | high | critical (only when status=ok)
  relevant: boolean | null;
  target_agent: string | null;
  rationale: string;
  confidence_pct: number | null;
  model: string;
  demo: boolean;
  disclaimer: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith("argus_csrf="))
      ?.slice("argus_csrf=".length);
    if (csrf) headers.set("X-Argus-CSRF", decodeURIComponent(csrf));
  }
  const res = await fetch(path, {
    credentials: "include",
    ...init,
    headers,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // body is not JSON — keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  login: (username: string, password: string): Promise<{ user: string }> =>
    request("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: (): Promise<{ status: string }> =>
    request("/api/logout", { method: "POST" }),
  status: (): Promise<StatusResponse> => request("/api/status"),
  health: (): Promise<HealthResponse> => request("/api/health"),
  analyses: (limit = 20): Promise<AnalysisListItem[]> =>
    request(`/api/analyses?limit=${limit}`),
  analysis: (id: number): Promise<AnalysisDetail> =>
    request(`/api/analyses/${id}`),
  hitrate: (): Promise<HitRateResponse> => request("/api/hitrate"),
  feedbackSummary: (days = 30): Promise<FeedbackSummary> =>
    request(`/api/feedback/summary?days=${days}`),
  sendFeedback: (vote: FeedbackVote): Promise<FeedbackAck> =>
    request("/api/feedback", { method: "POST", body: JSON.stringify(vote) }),
  cost: (): Promise<CostResponse> => request("/api/cost"),
  alerts: (limit = 50): Promise<AlertItem[]> =>
    request(`/api/alerts?limit=${limit}`),
  quotes: (): Promise<QuoteItem[]> => request("/api/quotes"),
  proposal: (symbol: string): Promise<ProposalResponse> =>
    request(`/api/proposal/${encodeURIComponent(symbol)}`),
  cryptoDashboard: (symbol: string): Promise<CryptoDashboardResponse> =>
    request(`/api/crypto/${encodeURIComponent(symbol)}`),
  tradingStatus: (): Promise<TradingStatusResponse> => request("/api/trading/status"),
  tradingAccount: (): Promise<TradingAccountResponse> => request("/api/trading/account"),
  tradingUniverse: (): Promise<TradingUniverseResponse> => request("/api/trading/universe"),
  tradingNews: (): Promise<TradingNewsResponse> => request("/api/trading/news"),
  tradingResearch: (): Promise<TradingResearchResponse> => request("/api/trading/research"),
  tradingWatchlist: (): Promise<TradingWatchlistResponse> => request("/api/trading/watchlist"),
  tradingPlans: (): Promise<TradingPlansResponse> => request("/api/trading/plans"),
  tradingMissions: (): Promise<MissionListResponse> => request("/api/trading/missions"),
  tradingPositions: (): Promise<TradingPositionsResponse> => request("/api/trading/positions"),
  tradingOrders: (): Promise<TradingOrdersResponse> => request("/api/trading/orders"),
  tradingPerformance: (): Promise<TradingPerformanceResponse> =>
    request("/api/trading/performance"),
  tradingCosts: (): Promise<TradingCostsResponse> => request("/api/trading/costs"),
  tradingRisk: (): Promise<TradingRiskResponse> => request("/api/trading/risk"),
  tradingStorage: (): Promise<TradingStorageResponse> => request("/api/trading/storage"),
  tradingHealth: (): Promise<TradingHealthResponse> => request("/api/trading/health"),
  intelligenceSources: (): Promise<IntelligencePage<IntelligenceSource>> =>
    request("/api/intelligence/sources?limit=500"),
  intelligenceEvents: (): Promise<IntelligencePage<NormalizedCryptoEvent>> =>
    request("/api/intelligence/events?limit=100"),
  intelligenceHealth: (): Promise<IntelligenceHealth> => request("/api/intelligence/health"),
  createTradingMission: (body: {
    capital_limit: string;
    duration_days: number;
    mode?: TradingMode;
  }): Promise<MissionMutationResponse> =>
    request("/api/trading/missions", { method: "POST", body: JSON.stringify(body) }),
  armTradingMission: (missionId: string, expectedRevision: number): Promise<MissionMutationResponse> =>
    request(`/api/trading/missions/${encodeURIComponent(missionId)}/arm`, {
      method: "POST",
      body: JSON.stringify({ confirm: true, expected_revision: expectedRevision }),
    }),
  haltTradingMission: (
    missionId: string,
    expectedRevision: number,
    reason: string,
  ): Promise<MissionMutationResponse> =>
    request(`/api/trading/missions/${encodeURIComponent(missionId)}/halt`, {
      method: "POST",
      body: JSON.stringify({ reason, expected_revision: expectedRevision }),
    }),
  enableTradingKillSwitch: (reason: string): Promise<{ enabled: true; message: string }> =>
    request("/api/trading/kill-switch", {
      method: "POST",
      body: JSON.stringify({ enabled: true, reason }),
    }),
  ohlcv: (symbol: string, tf?: string): Promise<OHLCVResponse> =>
    request(
      `/api/ohlcv/${encodeURIComponent(symbol)}${tf ? `?tf=${encodeURIComponent(tf)}` : ""}`,
    ),
  ohlcvBatch: (symbols: string[], n = 40): Promise<SparkBatchResponse> =>
    request(
      `/api/ohlcv-batch?symbols=${encodeURIComponent(symbols.join(","))}&n=${n}`,
    ),
  news: (limit = 50): Promise<NewsResponseItem[]> =>
    request(`/api/news?limit=${limit}`),
  portfolio: (): Promise<PortfolioResponse> => request("/api/portfolio"),
  getSettings: (): Promise<SettingsView> => request("/api/settings"),
  saveLLM: (b: LLMUpdate): Promise<SettingsWriteResult> =>
    request("/api/settings/llm", { method: "POST", body: JSON.stringify(b) }),
  saveTelegram: (b: TelegramUpdate): Promise<SettingsWriteResult> =>
    request("/api/settings/telegram", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  saveWatchlist: (b: {
    watchlist: WatchlistItemT[];
    holdings: HoldingItemT[];
  }): Promise<SettingsWriteResult> =>
    request("/api/settings/watchlist", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  saveTriggers: (b: {
    triggers: TriggerThresholds;
    alerts: AlertConfig;
  }): Promise<SettingsWriteResult> =>
    request("/api/settings/triggers", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  saveDataKeys: (b: DataKeysUpdate): Promise<SettingsWriteResult> =>
    request("/api/settings/data", { method: "POST", body: JSON.stringify(b) }),
  chat: (
    messages: ChatMessage[],
    contextSymbol?: string | null,
  ): Promise<ChatResponse> =>
    request("/api/chat", {
      method: "POST",
      body: JSON.stringify({ messages, context_symbol: contextSymbol ?? null }),
    }),
  chatHistory: (): Promise<ChatTurnView[]> => request("/api/chat/history"),
  routeNews: (b: NewsRouteRequest): Promise<NewsRouteResult> =>
    request("/api/news/route", { method: "POST", body: JSON.stringify(b) }),
};
