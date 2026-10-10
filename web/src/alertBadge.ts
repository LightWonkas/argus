import { useEffect, useRef, useState } from "react";
import { api, type AlertItem } from "./api";

const SEEN_KEY = "argus.alerts.seenAt";
const BASE_TITLE = "Argus Cockpit";

/** Tab title: "(n) Argus Cockpit" when there are unread criticals. Pure — unit tested. */
export function titleWithBadge(count: number, base: string = BASE_TITLE): string {
  return count > 0 ? `(${count}) ${base}` : base;
}

/** Returns the critical alerts newer than the last-seen instant. Pure — unit tested. */
export function unseenCritical(alerts: AlertItem[], seenAt: string | null): AlertItem[] {
  return alerts.filter(
    (a) => a.severity === "critical" && (seenAt == null || a.created_at > seenAt),
  );
}

/** Critical alert watch: title badge while the tab is in the background + a notification
 *  (if permitted). Returning to the tab resets the counter (seenAt = now). Permission is
 *  never requested here — notifications show only if the user opted in via 🔔 in StatusBar. */
export function useAlertBadge(enabled: boolean): number {
  const [count, setCount] = useState(0);
  const notified = useRef<Set<string>>(new Set()); // one notification per alert

  useEffect(() => {
    if (!enabled) return;

    const markSeen = () => {
      localStorage.setItem(SEEN_KEY, new Date().toISOString());
      setCount(0);
    };
    if (document.hasFocus()) markSeen();

    const poll = () =>
      api
        .alerts(20)
        .then((alerts) => {
          if (document.hasFocus()) {
            markSeen();
            return;
          }
          const fresh = unseenCritical(alerts, localStorage.getItem(SEEN_KEY));
          setCount(fresh.length);
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            for (const a of fresh.slice(0, 3)) {
              const key = `${a.analysis_id}-${a.kind}-${a.created_at}`;
              if (notified.current.has(key)) continue;
              notified.current.add(key);
              new Notification(`Argus — ${a.symbol} critical`, { body: a.message });
            }
          }
        })
        .catch(() => undefined); // on network errors the badge stays as is (no false alarms)

    void poll();
    const t = setInterval(poll, 30_000);
    window.addEventListener("focus", markSeen);
    return () => {
      clearInterval(t);
      window.removeEventListener("focus", markSeen);
    };
  }, [enabled]);

  useEffect(() => {
    document.title = titleWithBadge(count);
  }, [count]);

  return count;
}
