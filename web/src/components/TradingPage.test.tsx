import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { TradingPage } from "./TradingPage";

vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    tradingStatus: vi.fn(),
    tradingAccount: vi.fn(),
    tradingUniverse: vi.fn(),
    tradingNews: vi.fn(),
    tradingResearch: vi.fn(),
    tradingWatchlist: vi.fn(),
    tradingPlans: vi.fn(),
    tradingMissions: vi.fn(),
    tradingPositions: vi.fn(),
    tradingOrders: vi.fn(),
    tradingPerformance: vi.fn(),
    tradingCosts: vi.fn(),
    tradingRisk: vi.fn(),
    tradingStorage: vi.fn(),
    tradingHealth: vi.fn(),
    createTradingMission: vi.fn(),
    armTradingMission: vi.fn(),
    haltTradingMission: vi.fn(),
    enableTradingKillSwitch: vi.fn(),
  },
}));

function resolveEmpty() {
  vi.mocked(api.tradingStatus).mockResolvedValue({
    mode: "public_monitor",
    armed: false,
    kill_switch: false,
    active_mission_id: null,
    active_mission_status: null,
    public_market_data_available: true,
    private_account_available: false,
    user_data_stream_available: false,
    spot_execution_available: false,
    live_execution_enabled: false,
    execution_backend: "disabled",
    source: "stored_point_in_time",
    data_status: "pending",
    reasons: ["spot execution adapter is not in this build"],
  });
  vi.mocked(api.tradingAccount).mockResolvedValue({
    status: "no_data",
    connected: false,
    snapshot: null,
    reasons: ["private account adapter is not connected in this build"],
    secret_exposed: false,
  });
  vi.mocked(api.tradingUniverse).mockResolvedValue({
    as_of: null,
    scanned_count: 0,
    eligible_count: 0,
    returned_count: 0,
    page_limit: 100,
    next_cursor: null,
    snapshots: [],
    data_status: "pending",
    source: "stored_point_in_time",
  });
  vi.mocked(api.tradingResearch).mockResolvedValue({
    scores: [],
    coin_results: [],
    data_status: "pending",
    source: "stored_point_in_time",
  });
  vi.mocked(api.tradingNews).mockResolvedValue({
    records: [],
    reactions: [],
    provider_health: null,
    data_status: "pending",
    source: "stored_point_in_time",
  });
  vi.mocked(api.tradingWatchlist).mockResolvedValue({
    symbols: ["BTCUSDT", "ETHUSDT"],
    changed_at: null,
    dynamic_discovery_enabled: false,
    data_status: "pending",
    source: "configured_fallback",
  });
  vi.mocked(api.tradingMissions).mockResolvedValue({ missions: [] });
  vi.mocked(api.tradingPlans).mockResolvedValue({
    plans: [],
    data_status: "pending",
    source: "stored_point_in_time",
  });
  vi.mocked(api.tradingPositions).mockResolvedValue({
    mission_id: null,
    mission_inventory: [],
    shadow_positions: [],
    data_status: "pending",
    reason: "no position/fill data",
  });
  vi.mocked(api.tradingOrders).mockResolvedValue({
    mission_id: null,
    intents: [],
    exchange_orders: [],
    data_status: "pending",
    reason: "no order intent or exchange order data",
  });
  vi.mocked(api.tradingPerformance).mockResolvedValue({
    mission_id: null,
    gross_pnl: null,
    net_pnl: null,
    realized_pnl: null,
    unrealized_pnl: null,
    total_commission: null,
    estimated_slippage: null,
    turnover: null,
    fee_drag_pct: null,
    data_status: "pending",
    reason: "no active mission or realized fill data",
  });
  vi.mocked(api.tradingCosts).mockResolvedValue({
    schedules: [],
    conservative_estimate_enabled: false,
    conservative_maker_bps: null,
    conservative_taker_bps: null,
    uncertainty_buffer_bps: null,
    live_eligible: false,
    data_status: "pending",
    reason: "no verified current commission rate; execution is blocked",
  });
  vi.mocked(api.tradingRisk).mockResolvedValue({
    kill_switch: false,
    guard_results: [],
    data_status: "pending",
    reason: "no GUARD evaluation",
  });
  vi.mocked(api.tradingStorage).mockResolvedValue({
    health: {
      checked_at: "2026-09-02T12:00:00Z",
      database_bytes: null,
      wal_bytes: null,
      free_disk_bytes: null,
      raw_trade_events: 0,
      raw_depth_events: 0,
      retention_backlog_events: 0,
      retention_enabled: true,
      healthy: true,
      reasons: [],
    },
    last_retention: null,
  });
  vi.mocked(api.tradingHealth).mockResolvedValue({
    checked_at: "2026-09-02T12:00:00Z",
    overall: "degraded",
    last_market_data_at: null,
    public_market_data: "pending",
    news_providers: "pending",
    private_account: "no_data",
    user_data_stream: "no_data",
    reconciliation: "no_data",
    execution: "disabled",
    storage: {
      checked_at: "2026-09-02T12:00:00Z",
      database_bytes: null,
      wal_bytes: null,
      free_disk_bytes: null,
      raw_trade_events: 0,
      raw_depth_events: 0,
      retention_backlog_events: 0,
      retention_enabled: true,
      healthy: true,
      reasons: [],
    },
    reasons: ["private account/user-stream/reconciliation NO_DATA"],
  });
}

afterEach(() => vi.clearAllMocks());

describe("TradingPage", () => {
  it("shows loading while requests are in flight", () => {
    vi.mocked(api.tradingStatus).mockReturnValue(new Promise(() => undefined));
    render(<TradingPage onBack={vi.fn()} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows missing data without fabricating a zero PnL", async () => {
    resolveEmpty();
    render(<TradingPage onBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("LIVE EXECUTION DISABLED")).toBeInTheDocument());
    expect(screen.getAllByText(/DATA PENDING|NO_DATA/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("NO DATA").length).toBeGreaterThan(0);
    expect(screen.queryByText("0.00 USDT")).not.toBeInTheDocument();
    expect(screen.getByText("Private account").nextSibling).toHaveTextContent("NO_DATA / DISABLED");
  });

  it("shows stored news and a plan without execution authorization in the same PIT view", async () => {
    resolveEmpty();
    vi.mocked(api.tradingNews).mockResolvedValue({
      records: [
        {
          news_id: "news-1",
          canonical_url: "https://example.com/bitcoin-upgrade",
          provider: "rss",
          source_name: "Official Project",
          source_reliability: "0.95",
          title: "Bitcoin network upgrade released",
          short_summary: "Short public summary.",
          published_at: "2026-09-02T11:55:00Z",
          first_seen_at: "2026-09-02T12:00:00Z",
          related_symbols: ["BTCUSDT"],
          entity_matches: [],
          event_type: "network_upgrade",
          sentiment: "positive",
          impact_score: "40",
          confidence: "100",
          verification_status: "verified",
          risk_flags: [],
          data_status: "ok",
        },
      ],
      reactions: [],
      provider_health: { data_status: "ok" },
      data_status: "ok",
      source: "stored_point_in_time",
    });
    vi.mocked(api.tradingPlans).mockResolvedValue({
      plans: [
        {
          plan_id: "plan-1",
          mission_id: "mission-1",
          symbol: "BTCUSDT",
          strategy_basis: "news_catalyst",
          created_at: "2026-09-02T12:00:00Z",
          valid_until: "2026-09-02T12:05:00Z",
          horizon: "no_trade",
          expected_holding_minutes: 0,
          entry_condition: "NO_TRADE: commission NO_DATA",
          expected_entry_price: null,
          stop_price: null,
          expected_gross_return_bps: "100",
          expected_entry_cost_bps: "17.5",
          expected_exit_cost_bps: "17.5",
          estimated_round_trip_cost_bps: "35",
          expected_net_return_bps: "45",
          confidence: "80",
          news_evidence: ["news:news-1"],
          technical_evidence: ["market:1m:up"],
          risk_flags: ["commission NO_DATA"],
          rejection_reasons: ["commission NO_DATA"],
          cost_source: "conservative_estimate",
          live_eligible: false,
          data_status: "rejected",
          submission_authorized: false,
        },
      ],
      data_status: "rejected",
      source: "stored_point_in_time",
    });
    render(<TradingPage onBack={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText("Bitcoin network upgrade released")).toBeInTheDocument(),
    );
    expect(screen.getByText("no_trade")).toBeInTheDocument();
    expect(screen.getByText("news_catalyst")).toBeInTheDocument();
    expect(screen.getByText("35 bps")).toBeInTheDocument();
    expect(screen.getByText("ESTIMATED COST — NOT ELIGIBLE FOR LIVE EXECUTION")).toBeInTheDocument();
  });

  it("does not turn no-relevant-news on a healthy provider into NO_DATA", async () => {
    resolveEmpty();
    vi.mocked(api.tradingNews).mockResolvedValue({
      records: [],
      reactions: [],
      provider_health: {
        data_status: "ok",
        coverage_status: "no_relevant_news",
        provider_health: [{ provider_id: "feed-a", name: "Feed A", health_status: "healthy" }],
      },
      data_status: "ok",
      source: "stored_point_in_time",
    });
    render(<TradingPage onBack={vi.fn()} />);
    await waitFor(() =>
      expect(
        screen.getByText("NO_RELEVANT_NEWS · sources healthy, no relevant recent news"),
      ).toBeInTheDocument(),
    );
  });

  it("switches to flying blind instead of presenting an API error as current state", async () => {
    resolveEmpty();
    vi.mocked(api.tradingStatus).mockRejectedValue(new Error("offline"));
    render(<TradingPage onBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("FLYING BLIND")).toBeInTheDocument());
    expect(screen.getByText("Could not reach the trading API")).toBeInTheDocument();
  });
});
