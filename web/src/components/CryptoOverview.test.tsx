import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type CryptoDashboardResponse } from "../api";
import { DashboardContext } from "../dashboard/dashboardState";
import { CryptoOverview } from "./CryptoOverview";

vi.mock("../api", () => ({ api: { cryptoDashboard: vi.fn() } }));

const mockedDashboard = vi.mocked(api.cryptoDashboard);

function response(overrides: Partial<CryptoDashboardResponse> = {}): CryptoDashboardResponse {
  return {
    symbol: "BTCUSDT",
    data_status: "pending",
    as_of: null,
    source: "stored_point_in_time",
    spot: null,
    perpetual: null,
    funding: null,
    open_interest: null,
    basis: null,
    long_short_ratio: null,
    order_book: null,
    liquidations: [],
    quality: null,
    features: null,
    regime: null,
    experts: null,
    trade_candidate: null,
    guard: null,
    shadow_positions: [],
    backtest: null,
    missing_data: ["spot"],
    ...overrides,
  };
}

function renderWidget() {
  return render(
    <DashboardContext.Provider
      value={{
        activeSymbol: "BTCUSDT",
        setActiveSymbol: vi.fn(),
        symbols: ["BTCUSDT", "ETHUSDT"],
        timeframesBySymbol: {},
      }}
    >
      <CryptoOverview instance={{ i: "crypto", type: "crypto" }} onClose={vi.fn()} update={vi.fn()} />
    </DashboardContext.Provider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("CryptoOverview", () => {
  it("shows the loading state while the request is in flight", () => {
    mockedDashboard.mockReturnValue(new Promise(() => undefined));
    renderWidget();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an explicit pending state without fabricating zeros when there is no market record", async () => {
    mockedDashboard.mockResolvedValue(response());
    renderWidget();
    await waitFor(() => expect(screen.getByText("DATA PENDING")).toBeInTheDocument());
    expect(screen.getByText(/No stored point-in-time market data/)).toBeInTheDocument();
    expect(screen.queryByText("0.00")).not.toBeInTheDocument();
  });

  it("switches to flying blind instead of presenting an API error as current data", async () => {
    mockedDashboard.mockRejectedValue(new Error("offline"));
    renderWidget();
    await waitFor(() => expect(screen.getByText("FLYING BLIND")).toBeInTheDocument());
    expect(screen.getByText(/Could not reach the crypto API/)).toBeInTheDocument();
  });
});
