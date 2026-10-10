import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  ApiError,
  api,
  type MissionListResponse,
  type IntelligenceHealth,
  type IntelligencePage,
  type IntelligenceSource,
  type NormalizedCryptoEvent,
  type TradingAccountResponse,
  type TradingCostsResponse,
  type TradingHealthResponse,
  type TradingNewsResponse,
  type TradingOrdersResponse,
  type TradingPerformanceResponse,
  type TradingPositionsResponse,
  type TradingPlansResponse,
  type TradingResearchResponse,
  type TradingRiskResponse,
  type TradingStatusResponse,
  type TradingStorageResponse,
  type TradingUniverseResponse,
  type TradingWatchlistResponse,
} from "../api";

interface TradingData {
  status: TradingStatusResponse;
  account: TradingAccountResponse;
  universe: TradingUniverseResponse;
  news: TradingNewsResponse;
  research: TradingResearchResponse;
  watchlist: TradingWatchlistResponse;
  plans: TradingPlansResponse;
  missions: MissionListResponse;
  positions: TradingPositionsResponse;
  orders: TradingOrdersResponse;
  performance: TradingPerformanceResponse;
  costs: TradingCostsResponse;
  risk: TradingRiskResponse;
  storage: TradingStorageResponse;
  health: TradingHealthResponse;
  intelligenceSources: IntelligencePage<IntelligenceSource>;
  intelligenceEvents: IntelligencePage<NormalizedCryptoEvent>;
  intelligenceHealth: IntelligenceHealth | null;
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900">
      <h2 className="border-b border-zinc-800 px-4 py-2.5 text-sm font-semibold text-zinc-200">
        {title}
      </h2>
      <div className="p-4">{children}</div>
    </section>
  );
}

function StateBadge({ value }: { value: string }) {
  const color =
    value === "ok" || value === "running" || value === "eligible"
      ? "border-emerald-700/60 bg-emerald-500/10 text-emerald-300"
      : value === "pending" || value === "no_data" || value === "disarmed"
        ? "border-amber-700/60 bg-amber-500/10 text-amber-300"
        : "border-red-700/60 bg-red-500/10 text-red-300";
  return (
    <span className={`rounded border px-2 py-0.5 text-[10px] font-semibold uppercase ${color}`}>
      {value}
    </span>
  );
}

const money = (value: string | null, suffix = " USDT") =>
  value == null ? "NO DATA" : `${value}${suffix}`;

const bytes = (value: number | null) =>
  value == null ? "NO DATA" : `${(value / 1024 / 1024).toFixed(1)} MiB`;

export function TradingPage({ onBack }: { onBack: () => void }) {
  const [data, setData] = useState<TradingData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [capital, setCapital] = useState("1000");
  const [duration, setDuration] = useState(7);

  const load = useCallback(async () => {
    try {
      const [
        status,
        account,
        universe,
        news,
        research,
        watchlist,
        plans,
        missions,
        positions,
        orders,
        performance,
        costs,
        risk,
        storage,
        health,
        intelligenceSources,
        intelligenceEvents,
        intelligenceHealth,
      ] = await Promise.all([
        api.tradingStatus(),
        api.tradingAccount(),
        api.tradingUniverse(),
        api.tradingNews(),
        api.tradingResearch(),
        api.tradingWatchlist(),
        api.tradingPlans(),
        api.tradingMissions(),
        api.tradingPositions(),
        api.tradingOrders(),
        api.tradingPerformance(),
        api.tradingCosts(),
        api.tradingRisk(),
        api.tradingStorage(),
        api.tradingHealth(),
        api.intelligenceSources?.() ?? Promise.resolve({ items: [], data_status: "no_data" as const }),
        api.intelligenceEvents?.() ?? Promise.resolve({ items: [], data_status: "no_data" as const }),
        api.intelligenceHealth?.() ?? Promise.resolve(null),
      ]);
      setData({
        status,
        account,
        universe,
        news,
        research,
        watchlist,
        plans,
        missions,
        positions,
        orders,
        performance,
        costs,
        risk,
        storage,
        health,
        intelligenceSources,
        intelligenceEvents,
        intelligenceHealth,
      });
      setError(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Could not reach the trading API");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 15_000);
    return () => clearInterval(timer);
  }, [load]);

  const activeMission = useMemo(() => {
    if (!data) return null;
    return (
      data.missions.missions.find(
        (mission) => mission.mission_id === data.status.active_mission_id,
      ) ?? data.missions.missions[0] ?? null
    );
  }, [data]);

  async function mutate(action: () => Promise<unknown>) {
    setBusy(true);
    setMutationError(null);
    try {
      await action();
      await load();
    } catch (cause) {
      setMutationError(cause instanceof ApiError ? cause.message : "Control action failed");
    } finally {
      setBusy(false);
    }
  }

  function createMission(event: FormEvent) {
    event.preventDefault();
    void mutate(() =>
      api.createTradingMission({
        capital_limit: capital,
        duration_days: duration,
        mode: "public_monitor",
      }),
    );
  }

  if (error && !data) {
    return (
      <main className="mx-auto max-w-6xl p-6">
        <h1 className="text-lg font-semibold text-red-300">FLYING BLIND</h1>
        <p className="mt-2 text-sm text-red-400">{error}</p>
        <button className="mt-4 text-sm text-zinc-300 underline" onClick={onBack}>
          Back to cockpit
        </button>
      </main>
    );
  }

  if (!data) return <p className="p-8 text-sm text-zinc-500">Loading…</p>;

  const eligible = data.universe.snapshots.filter((row) => row.eligible);
  const topResearch = data.research.scores.slice(0, 12);
  const rawNewsHealth = data.news.provider_health;
  const newsCoverage =
    typeof rawNewsHealth?.coverage_status === "string"
      ? rawNewsHealth.coverage_status
      : data.news.data_status;
  const providerHealth = Array.isArray(rawNewsHealth?.provider_health)
    ? (rawNewsHealth.provider_health as Array<Record<string, unknown>>)
    : [];

  return (
    <main className="mx-auto max-w-7xl space-y-4 px-4 py-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold">Shadow Trading Simulation</h1>
            <StateBadge value={data.status.mode} />
            <span className="rounded border border-red-800 bg-red-950/50 px-2 py-0.5 text-[10px] font-bold text-red-300">
              LIVE EXECUTION DISABLED
            </span>
          </div>
          <p className="mt-1 text-xs text-zinc-500">
            Simulation / research view · stored point-in-time data · net cost first · no edge or
            profitability is implied · Not financial advice.
          </p>
        </div>
        <button
          onClick={onBack}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          ← Back to cockpit
        </button>
      </div>

      {(error || mutationError) && (
        <div className="rounded-lg border border-red-900 bg-red-950/30 px-4 py-2 text-sm text-red-300">
          {mutationError ?? `Data may be stale: ${error}`}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-3">
          <Card title="News and Event Center">
            <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
              <StateBadge value={data.intelligenceHealth?.data_status ?? "no_data"} />
              <span className="text-zinc-500">Mode</span>
              <span className="text-zinc-200">{data.intelligenceHealth?.system_mode ?? data.status.mode}</span>
              <span className="rounded border border-red-800 bg-red-950/50 px-2 py-0.5 font-bold text-red-300">
                Execution: DISABLED
              </span>
              <span className="ml-auto text-zinc-500">
                Sources {data.intelligenceSources.total_count ?? "NO_DATA"} · events {data.intelligenceEvents.returned_count ?? "NO_DATA"}
              </span>
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">Source health</h3>
                {data.intelligenceSources.items.length === 0 ? (
                  <p className="text-sm text-amber-300">NO_DATA · no stored Source Registry snapshot</p>
                ) : (
                  <div className="max-h-64 overflow-auto text-xs">
                    {data.intelligenceSources.items.map((source) => (
                      <div key={source.source_id} className="grid grid-cols-[1fr_auto] gap-2 border-t border-zinc-800 py-2">
                        <div className="min-w-0">
                          <div className="truncate text-zinc-200">{source.display_name}</div>
                          <div className="truncate text-zinc-500">{source.source_group} · {source.last_success_at ? `last success ${new Date(source.last_success_at).toLocaleString("en-US")}` : "last success NO_DATA"}</div>
                        </div>
                        <div className="text-right"><StateBadge value={source.health_status} /><div className="mt-1 text-zinc-500">{source.availability}</div></div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">Latest critical and important events</h3>
                {data.intelligenceEvents.items.length === 0 ? (
                  <p className="text-sm text-amber-300">NO_DATA · no events; this does not mean “no news”</p>
                ) : (
                  <div className="max-h-64 space-y-2 overflow-auto text-xs">
                    {data.intelligenceEvents.items.slice(0, 12).map((event) => (
                      <div key={event.event_id} className="border-t border-zinc-800 pt-2">
                        <div className="flex flex-wrap items-center gap-2"><StateBadge value={event.severity} /><span className="text-zinc-300">{event.event_type}</span><span className="ml-auto text-zinc-500">{event.affected_symbols.join(", ") || "ENTITY NO_DATA"}</span></div>
                        <div className="mt-1 text-zinc-200">{event.facts[0] ?? "Summary NO_DATA"}</div>
                        <div className="mt-1 text-zinc-500">confidence {event.confidence} · independent corroboration {event.corroboration_count} · {new Date(event.first_seen_at).toLocaleString("en-US")}</div>
                        {event.uncertainties.length > 0 && <div className="mt-1 text-amber-300">Uncertainty: {event.uncertainties.join(" · ")}</div>}
                        <div className="mt-1 flex flex-wrap gap-2">{event.evidence.map((evidence) => <a key={evidence.document_id} href={evidence.canonical_url} target="_blank" rel="noreferrer" className="text-blue-300 hover:underline">{evidence.source_id} ({evidence.evidence_role})</a>)}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </Card>
        </div>
        <Card title="Mission">
          {activeMission ? (
            <div className="space-y-3 text-xs">
              <div className="flex items-center justify-between">
                <span className="text-zinc-500">Status</span>
                <StateBadge value={activeMission.status} />
              </div>
              <dl className="grid grid-cols-2 gap-2 text-zinc-400">
                <dt>Budget</dt>
                <dd className="text-right text-zinc-200">{money(activeMission.capital_limit_usdt ?? activeMission.capital_limit)}</dd>
                <dt>TRY reference</dt>
                <dd className="text-right text-zinc-200">
                  {activeMission.input_budget_try == null ? "NO_DATA" : money(activeMission.input_budget_try, " TRY")}
                </dd>
                <dt>Daily net PnL</dt>
                <dd className="text-right text-zinc-200">{money(activeMission.daily_realized_net_pnl)}</dd>
                <dt>Profit stop</dt>
                <dd className="text-right text-zinc-200">{activeMission.daily_profit_stop_pct}%</dd>
                <dt>Loss stop</dt>
                <dd className="text-right text-zinc-200">-{activeMission.daily_loss_stop_pct}%</dd>
                <dt>Ends</dt>
                <dd className="text-right text-zinc-200">
                  {new Date(activeMission.scheduled_end_at).toLocaleString("en-US")}
                </dd>
                <dt>Policy</dt>
                <dd className="text-right text-zinc-200">{activeMission.end_policy}</dd>
                <dt>Revision</dt>
                <dd className="text-right text-zinc-200">{activeMission.revision}</dd>
              </dl>
              <div className="flex gap-2">
                {activeMission.status === "disarmed" && (
                  <button
                    disabled={busy || data.status.kill_switch}
                    onClick={() =>
                      void mutate(() =>
                        api.armTradingMission(activeMission.mission_id, activeMission.revision),
                      )
                    }
                    className="rounded bg-amber-700 px-3 py-1.5 text-amber-50 disabled:opacity-40"
                  >
                    Public mission ARM
                  </button>
                )}
                {!["halted", "completed", "failed_safe"].includes(activeMission.status) && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      void mutate(() =>
                        api.haltTradingMission(
                          activeMission.mission_id,
                          activeMission.revision,
                          "operator dashboard halt",
                        ),
                      )
                    }
                    className="rounded border border-zinc-700 px-3 py-1.5 disabled:opacity-40"
                  >
                    Halt
                  </button>
                )}
              </div>
            </div>
          ) : (
            <form className="space-y-3 text-xs" onSubmit={createMission}>
              <p className="text-amber-300">No mission · system DISARMED</p>
              <p className="text-zinc-500">
                No TRY/USDT conversion provider is connected; since no fake rate is used, enter the
                canonical USDT budget.
              </p>
              <label className="block text-zinc-400">
                Allocated USDT
                <input
                  value={capital}
                  onChange={(event) => setCapital(event.target.value)}
                  type="number"
                  min="0.01"
                  step="0.01"
                  className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5"
                />
              </label>
              <label className="block text-zinc-400">
                Duration (days, max 7)
                <input
                  value={duration}
                  onChange={(event) => setDuration(Number(event.target.value))}
                  type="number"
                  min="1"
                  max="7"
                  className="mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-2 py-1.5"
                />
              </label>
              <button
                disabled={busy || data.status.kill_switch}
                className="rounded bg-blue-700 px-3 py-1.5 text-white disabled:opacity-40"
              >
                Create DISARMED public mission
              </button>
            </form>
          )}
          <button
            disabled={busy || data.status.kill_switch}
            onClick={() => {
              if (window.confirm("Enable the kill switch permanently?")) {
                void mutate(() => api.enableTradingKillSwitch("operator dashboard kill switch"));
              }
            }}
            className="mt-4 w-full rounded border border-red-800 px-3 py-1.5 text-xs text-red-300 disabled:opacity-40"
          >
            {data.status.kill_switch ? "KILL SWITCH ENABLED" : "Kill switch"}
          </button>
        </Card>

        <Card title="Coin research">
          <div className="mb-3 grid grid-cols-3 gap-2 text-center text-xs">
            <div><div className="text-lg text-zinc-100">{data.universe.scanned_count}</div><div className="text-zinc-500">Scanned</div></div>
            <div><div className="text-lg text-emerald-300">{data.universe.eligible_count}</div><div className="text-zinc-500">Eligible</div></div>
            <div><div className="text-lg text-zinc-100">{data.watchlist.symbols.length}</div><div className="text-zinc-500">Watchlist</div></div>
          </div>
          {data.universe.data_status !== "ok" ? (
            <p className="text-sm text-amber-300">DATA PENDING · no dynamic discovery PIT record</p>
          ) : (
            <div className="max-h-48 overflow-auto text-xs">
              {[...eligible, ...data.universe.snapshots.filter((row) => !row.eligible)]
                .slice(0, 15)
                .map((row) => (
                  <div key={row.symbol} className="flex items-center gap-2 border-t border-zinc-800 py-1.5">
                    <span className="w-24 text-zinc-200">{row.symbol}</span>
                    <StateBadge value={row.eligible ? "eligible" : "rejected"} />
                    <span className="ml-auto truncate text-zinc-500">
                      {row.rejection_reasons.join(", ") || "quality pass"}
                    </span>
                  </div>
                ))}
            </div>
          )}
        </Card>

        <Card title="System and account boundary">
          <dl className="space-y-2 text-xs">
            {[
              ["Public market data", data.status.public_market_data_available],
              ["Private account", data.status.private_account_available],
              ["User data stream", data.status.user_data_stream_available],
              ["Spot execution", data.status.spot_execution_available],
              ["Live execution", data.status.live_execution_enabled],
            ].map(([label, ready]) => (
              <div key={String(label)} className="flex justify-between">
                <dt className="text-zinc-500">{label}</dt>
                <dd className={ready ? "text-emerald-300" : "text-amber-300"}>
                  {ready ? "AVAILABLE" : "NO_DATA / DISABLED"}
                </dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-zinc-500">{data.account.reasons.join(" · ")}</p>
        </Card>

        <Card title="Research scores">
          {topResearch.length === 0 ? (
            <p className="text-sm text-amber-300">DATA PENDING · no score was fabricated</p>
          ) : (
            <div className="space-y-1 text-xs">
              {topResearch.map((score) => (
                <div key={score.symbol} className="flex items-center border-t border-zinc-800 py-1.5">
                  <span>{score.symbol}</span>
                  <StateBadge value={score.status} />
                  <span className="ml-auto text-zinc-300">
                    {score.final_research_score ?? "NO_DATA"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="News intelligence">
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-zinc-500">Coverage</span>
            <StateBadge value={newsCoverage} />
            {providerHealth.map((provider) => (
              <span key={String(provider.provider_id)} className="text-zinc-500">
                {String(provider.name)}: {String(provider.health_status)}
              </span>
            ))}
          </div>
          {data.news.records.length === 0 ? (
            <p className="text-sm text-amber-300">
              {newsCoverage === "no_relevant_news"
                ? "NO_RELEVANT_NEWS · sources healthy, no relevant recent news"
                : `${newsCoverage.toUpperCase()} · news coverage does not meet the simulation threshold`}
            </p>
          ) : (
            <div className="max-h-52 space-y-2 overflow-auto text-xs">
              {data.news.records.slice(0, 12).map((item) => (
                <div key={item.news_id} className="border-t border-zinc-800 pt-2">
                  <div className="flex items-center gap-2">
                    <StateBadge value={item.verification_status} />
                    <span className="text-zinc-500">{item.event_type}</span>
                    <span className="ml-auto text-zinc-500">{item.related_symbols.join(", ") || "ENTITY NO_DATA"}</span>
                  </div>
                  <a className="mt-1 block text-zinc-200 hover:underline" href={item.canonical_url} target="_blank" rel="noreferrer">
                    {item.title}
                  </a>
                  <div className="mt-1 text-zinc-500">{item.source_name} · reliability {item.source_reliability}</div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="Multi-horizon plans (simulation)">
          {data.plans.plans.length === 0 ? (
            <p className="text-sm text-amber-300">PENDING · no stored TradePlan, nothing was generated</p>
          ) : (
            <div className="max-h-52 space-y-2 overflow-auto text-xs">
              {data.plans.plans.slice(0, 12).map((plan) => (
                <div key={plan.plan_id} className="grid grid-cols-2 gap-1 border-t border-zinc-800 pt-2">
                  <span className="text-zinc-200">{plan.symbol}</span><StateBadge value={plan.horizon} />
                  <span className="text-zinc-500">Duration</span><span className="text-right">{plan.expected_holding_minutes} min</span>
                  <span className="text-zinc-500">Strategy</span><span className="text-right">{plan.strategy_basis}</span>
                  <span className="text-zinc-500">Net edge</span><span className="text-right">{plan.expected_net_return_bps ?? "NO_DATA"} bps</span>
                  <span className="text-zinc-500">Round-trip cost</span><span className="text-right">{plan.estimated_round_trip_cost_bps ?? "NO_DATA"} bps</span>
                  <span className="text-zinc-500">Cost source</span><span className="text-right">{plan.cost_source}</span>
                  <span className="text-zinc-500">Valid until</span><span className="text-right">{new Date(plan.valid_until).toLocaleTimeString("en-US")}</span>
                  {plan.cost_source === "conservative_estimate" && (
                    <span className="col-span-2 text-amber-300">ESTIMATED COST — NOT ELIGIBLE FOR LIVE EXECUTION</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="Portfolio and net performance (simulation)">
          <dl className="grid grid-cols-2 gap-2 text-xs">
            <dt className="text-zinc-500">Positions</dt><dd className="text-right">{data.positions.mission_inventory.length}</dd>
            <dt className="text-zinc-500">Shadow positions</dt><dd className="text-right">{data.positions.shadow_positions.length}</dd>
            <dt className="text-zinc-500">Gross PnL</dt><dd className="text-right">{money(data.performance.gross_pnl)}</dd>
            <dt className="text-zinc-500">Net PnL</dt><dd className="text-right">{money(data.performance.net_pnl)}</dd>
            <dt className="text-zinc-500">Commission</dt><dd className="text-right">{money(data.performance.total_commission)}</dd>
            <dt className="text-zinc-500">Slippage</dt><dd className="text-right">{money(data.performance.estimated_slippage)}</dd>
          </dl>
          {data.performance.data_status !== "ok" && (
            <p className="mt-3 text-xs text-amber-300">{data.performance.reason}</p>
          )}
        </Card>

        <Card title="Cost and commission">
          {data.costs.schedules.length === 0 ? (
            <div className="space-y-2 text-sm text-amber-300">
              <p>{data.costs.reason}</p>
              {data.costs.conservative_estimate_enabled && (
                <p className="text-xs">
                  ESTIMATED COST — NOT ELIGIBLE FOR LIVE EXECUTION · maker {data.costs.conservative_maker_bps} bps · taker {data.costs.conservative_taker_bps} bps · uncertainty {data.costs.uncertainty_buffer_bps} bps
                </p>
              )}
            </div>
          ) : (
            <div className="space-y-1 text-xs">
              {data.costs.schedules.slice(0, 12).map((fee) => (
                <div key={fee.symbol} className="grid grid-cols-3 border-t border-zinc-800 py-1.5">
                  <span>{fee.symbol}</span>
                  <span>{fee.source}</span>
                  <span className="text-right">taker {fee.taker_rate ?? "NO_DATA"}</span>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="Order intents and GUARD">
          <div className="space-y-2 text-xs">
            <div className="flex justify-between"><span className="text-zinc-500">Order intent</span><span>{data.orders.intents.length}</span></div>
            <div className="flex justify-between"><span className="text-zinc-500">Exchange order</span><span>{data.orders.exchange_orders.length}</span></div>
            <div className="flex justify-between"><span className="text-zinc-500">GUARD decisions</span><span>{data.risk.guard_results.length}</span></div>
            <p className="text-amber-300">{data.orders.reason}</p>
            <p className="text-zinc-500">{data.risk.reason}</p>
          </div>
        </Card>

        <Card title="SQLite and retention">
          <dl className="grid grid-cols-2 gap-2 text-xs">
            <dt className="text-zinc-500">DB</dt><dd className="text-right">{bytes(data.storage.health.database_bytes)}</dd>
            <dt className="text-zinc-500">WAL</dt><dd className="text-right">{bytes(data.storage.health.wal_bytes)}</dd>
            <dt className="text-zinc-500">Raw trade</dt><dd className="text-right">{data.storage.health.raw_trade_events}</dd>
            <dt className="text-zinc-500">Raw depth</dt><dd className="text-right">{data.storage.health.raw_depth_events}</dd>
            <dt className="text-zinc-500">Backlog</dt><dd className="text-right">{data.storage.health.retention_backlog_events}</dd>
            <dt className="text-zinc-500">Retention</dt><dd className="text-right">{data.storage.health.retention_enabled ? "ON" : "OFF"}</dd>
          </dl>
          <p className={`mt-3 text-xs ${data.storage.health.healthy ? "text-emerald-300" : "text-red-300"}`}>
            {data.storage.health.healthy ? "Storage health OK" : data.storage.health.reasons.join(" · ")}
          </p>
          <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-xs">
            <span className="text-zinc-500">Trading health</span>
            <StateBadge value={data.health.overall} />
          </div>
          <p className="mt-2 text-xs text-zinc-500">{data.health.reasons.join(" · ")}</p>
        </Card>
      </div>
    </main>
  );
}
