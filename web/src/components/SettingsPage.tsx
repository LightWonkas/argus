import { useEffect, useState } from "react";
import {
  ApiError,
  api,
  type AlertConfig,
  type HoldingItemT,
  type SettingsView,
  type TriggerThresholds,
  type WatchlistItemT,
} from "../api";
import { Section, SecretField, input, label } from "./settingsUi";
import { validateTime, validateWatchHoldings } from "../validation";

const ASSET_CLASSES = ["bist", "metal", "crypto", "fx", "equity"];
const TIERS = ["holdings", "watchlist", "broad"] as const;
const SEVERITIES = ["info", "low", "medium", "high", "critical"];

export function SettingsPage({ onBack }: { onBack: () => void }) {
  const [s, setS] = useState<SettingsView | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Local edit state (filled once loaded)
  const [botToken, setBotToken] = useState<string | null>(null);
  const [chatIds, setChatIds] = useState("");
  // Data source keys (write-only; null = leave untouched)
  const [evdsKey, setEvdsKey] = useState<string | null>(null);
  const [cryptopanicToken, setCryptopanicToken] = useState<string | null>(null);
  const [finnhubKey, setFinnhubKey] = useState<string | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItemT[]>([]);
  const [holdings, setHoldings] = useState<HoldingItemT[]>([]);
  const [triggers, setTriggers] = useState<TriggerThresholds | null>(null);
  const [alerts, setAlerts] = useState<AlertConfig | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then((d) => {
        setS(d);
        setChatIds(d.telegram.authorized_chat_ids.join(", "));
        setWatchlist(d.watch.watchlist);
        setHoldings(d.watch.holdings);
        setTriggers(d.triggers.triggers);
        setAlerts(d.triggers.alerts);
      })
      .catch((e) => setErr(e instanceof ApiError ? e.message : "Could not load"));
  }, []);

  if (err) return <p className="p-8 text-red-400">{err}</p>;
  if (!s || !triggers || !alerts)
    return <p className="p-8 text-zinc-500">Loading…</p>;

  return (
    <main className="mx-auto max-w-3xl px-4 py-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Settings</h1>
        <button
          onClick={onBack}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          ← Back to cockpit
        </button>
      </div>

      <p className="mb-4 text-xs text-zinc-500">
        AI keys, providers and role→model settings live on the{" "}
        <span className="text-zinc-400">Models</span> page.
      </p>

      <div className="space-y-6">
        {/* Telegram */}
        <Section
          title="Telegram"
          onSave={() =>
            api.saveTelegram({
              bot_token: botToken,
              authorized_chat_ids: chatIds
                .split(",")
                .map((x) => parseInt(x.trim(), 10))
                .filter((n) => !Number.isNaN(n)),
            })
          }
        >
          <SecretField
            title="Bot Token"
            status={s.telegram.bot_token}
            value={botToken}
            onChange={setBotToken}
          />
          <div>
            <span className={label}>Authorized chat IDs (comma-separated)</span>
            <input
              value={chatIds}
              onChange={(e) => setChatIds(e.target.value)}
              className={input}
              placeholder="111111, 222222"
            />
          </div>
        </Section>

        {/* Data source keys — macro (EVDS) + news APIs */}
        <Section
          title="Data Sources"
          onSave={() =>
            api.saveDataKeys({
              evds_key: evdsKey,
              cryptopanic_token: cryptopanicToken,
              finnhub_key: finnhubKey,
            })
          }
        >
          <SecretField
            title="EVDS Key (CBRT macro — rates/inflation/reserves)"
            status={s.data.evds_key}
            value={evdsKey}
            onChange={setEvdsKey}
          />
          <SecretField
            title="CryptoPanic Token (crypto news feed)"
            status={s.data.cryptopanic_token}
            value={cryptopanicToken}
            onChange={setCryptopanicToken}
          />
          <SecretField
            title="Finnhub Key (general market news)"
            status={s.data.finnhub_key}
            value={finnhubKey}
            onChange={setFinnhubKey}
          />
          <p className="text-[11px] text-zinc-500">
            Sources without a key are skipped — the relevant agent sees that data as "DATA PENDING"
            (nothing is fabricated). EVDS is free: evds2.tcmb.gov.tr · the CryptoPanic/Finnhub
            free tier is enough.
          </p>
        </Section>

        {/* Watchlist + portfolio */}
        <Section
          title="Watchlist & Portfolio"
          validate={() => validateWatchHoldings(watchlist, holdings)}
          onSave={() => api.saveWatchlist({ watchlist, holdings })}
        >
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className={label}>Watched symbols</span>
              <button
                type="button"
                onClick={() =>
                  setWatchlist([
                    ...watchlist,
                    {
                      symbol: "",
                      asset_class: "crypto",
                      tier: "watchlist",
                      note: "",
                    },
                  ])
                }
                className="text-xs text-emerald-400 hover:text-emerald-300"
              >
                + Add symbol
              </button>
            </div>
            <div className="space-y-2">
              {watchlist.map((w, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    value={w.symbol}
                    onChange={(e) =>
                      setWatchlist(
                        watchlist.map((x, j) =>
                          j === i ? { ...x, symbol: e.target.value } : x,
                        ),
                      )
                    }
                    className={`${input} flex-1`}
                    placeholder="BTCUSDT"
                  />
                  <select
                    value={w.asset_class}
                    onChange={(e) =>
                      setWatchlist(
                        watchlist.map((x, j) =>
                          j === i ? { ...x, asset_class: e.target.value } : x,
                        ),
                      )
                    }
                    className={`${input} w-24`}
                  >
                    {ASSET_CLASSES.map((a) => (
                      <option key={a} value={a}>
                        {a}
                      </option>
                    ))}
                  </select>
                  <select
                    value={w.tier}
                    onChange={(e) =>
                      setWatchlist(
                        watchlist.map((x, j) =>
                          j === i
                            ? {
                                ...x,
                                tier: e.target.value as (typeof TIERS)[number],
                              }
                            : x,
                        ),
                      )
                    }
                    className={`${input} w-28`}
                  >
                    {TIERS.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() =>
                      setWatchlist(watchlist.filter((_, j) => j !== i))
                    }
                    className="shrink-0 px-2 text-red-400 hover:text-red-300"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className={label}>Portfolio holdings</span>
              <button
                type="button"
                onClick={() =>
                  setHoldings([
                    ...holdings,
                    {
                      symbol: "",
                      asset_class: "crypto",
                      quantity: 0,
                      avg_cost: 0,
                      cost_currency: "USD",
                      owner: "",
                    },
                  ])
                }
                className="text-xs text-emerald-400 hover:text-emerald-300"
              >
                + Add holding
              </button>
            </div>
            <div className="space-y-2">
              {holdings.map((h, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    value={h.symbol}
                    onChange={(e) =>
                      setHoldings(
                        holdings.map((x, j) =>
                          j === i ? { ...x, symbol: e.target.value } : x,
                        ),
                      )
                    }
                    className={`${input} flex-1`}
                    placeholder="BTCUSDT"
                  />
                  <input
                    type="number"
                    value={h.quantity}
                    onChange={(e) =>
                      setHoldings(
                        holdings.map((x, j) =>
                          j === i
                            ? {
                                ...x,
                                quantity: parseFloat(e.target.value) || 0,
                              }
                            : x,
                        ),
                      )
                    }
                    className={`${input} w-24`}
                    placeholder="quantity"
                  />
                  <input
                    type="number"
                    value={h.avg_cost}
                    onChange={(e) =>
                      setHoldings(
                        holdings.map((x, j) =>
                          j === i
                            ? {
                                ...x,
                                avg_cost: parseFloat(e.target.value) || 0,
                              }
                            : x,
                        ),
                      )
                    }
                    className={`${input} w-28`}
                    placeholder="cost"
                  />
                  <select
                    value={h.cost_currency}
                    onChange={(e) =>
                      setHoldings(
                        holdings.map((x, j) =>
                          j === i
                            ? {
                                ...x,
                                cost_currency: e.target.value as "TRY" | "USD",
                              }
                            : x,
                        ),
                      )
                    }
                    className={`${input} w-20`}
                  >
                    <option value="USD">USD</option>
                    <option value="TRY">TRY</option>
                  </select>
                  <input
                    value={h.owner ?? ""}
                    onChange={(e) =>
                      setHoldings(
                        holdings.map((x, j) =>
                          j === i ? { ...x, owner: e.target.value } : x,
                        ),
                      )
                    }
                    className={`${input} w-24`}
                    placeholder="owner"
                    title="Holding owner (optional) — may be left empty"
                  />
                  <button
                    type="button"
                    onClick={() =>
                      setHoldings(holdings.filter((_, j) => j !== i))
                    }
                    className="shrink-0 px-2 text-red-400 hover:text-red-300"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          </div>
        </Section>

        {/* Trigger thresholds + watch */}
        <Section
          title="Trigger Thresholds & Alerts"
          validate={() =>
            validateTime(alerts.quiet_start, "Quiet hours start") ??
            validateTime(alerts.quiet_end, "Quiet hours end")
          }
          onSave={() => api.saveTriggers({ triggers, alerts })}
        >
          <div className="grid grid-cols-3 gap-3">
            {(
              [
                ["RSI overbought", "rsi_overbought"],
                ["RSI oversold", "rsi_oversold"],
                ["Price move %", "price_move_pct"],
                ["ATR spike ×", "atr_spike_mult"],
                ["EMA fast", "ema_fast"],
                ["EMA slow", "ema_slow"],
              ] as const
            ).map(([lbl, key]) => (
              <div key={key}>
                <span className={label}>{lbl}</span>
                <input
                  type="number"
                  step="0.5"
                  value={triggers[key]}
                  onChange={(e) =>
                    setTriggers({
                      ...triggers,
                      [key]: parseFloat(e.target.value) || 0,
                    })
                  }
                  className={input}
                />
              </div>
            ))}
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <span className={label}>Instant alert threshold</span>
              <select
                value={alerts.min_instant_severity}
                onChange={(e) =>
                  setAlerts({ ...alerts, min_instant_severity: e.target.value })
                }
                className={input}
              >
                {SEVERITIES.map((sv) => (
                  <option key={sv} value={sv}>
                    {sv}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <span className={label}>Quiet hours start (HH:MM)</span>
              <input
                value={alerts.quiet_start ?? ""}
                onChange={(e) =>
                  setAlerts({ ...alerts, quiet_start: e.target.value || null })
                }
                className={input}
                placeholder="23:00"
              />
            </div>
            <div>
              <span className={label}>Quiet hours end (HH:MM)</span>
              <input
                value={alerts.quiet_end ?? ""}
                onChange={(e) =>
                  setAlerts({ ...alerts, quiet_end: e.target.value || null })
                }
                className={input}
                placeholder="08:00"
              />
            </div>
          </div>
        </Section>
      </div>

      <p className="mt-8 text-center text-xs text-zinc-500">
        Secrets are write-only and never shown back · Not financial advice.
      </p>
    </main>
  );
}
