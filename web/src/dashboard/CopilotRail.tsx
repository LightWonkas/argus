// Thin wrapper binding the Copilot to the dashboard's active symbol. The symbol selected in
// the watchlist/chart is passed to the Copilot as `context_symbol` (shown as a context badge).

import { Copilot } from "../components/Copilot";
import { useDashboard } from "./dashboardState";

export function CopilotRail() {
  const { activeSymbol } = useDashboard();
  return <Copilot contextSymbol={activeSymbol} />;
}
