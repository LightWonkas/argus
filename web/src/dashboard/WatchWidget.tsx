import { WatchTable } from "../components/WatchTable";
import { useDashboard } from "./dashboardState";
import { WidgetFrame } from "./WidgetFrame";

export function WatchWidget({ onClose }: { onClose: () => void }) {
  const { setActiveSymbol } = useDashboard();
  return (
    <WidgetFrame title="Watchlist / Holdings" icon="👁" onClose={onClose}>
      <WatchTable onSelect={setActiveSymbol} />
    </WidgetFrame>
  );
}
