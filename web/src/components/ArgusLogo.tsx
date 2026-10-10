// Argus emblem — gradient rounded-square background + Lucide ScanEye (a scanning eye: the
// always-awake watchman). Shared by Sidebar + StatusBar; the favicon in index.html embeds the
// same ScanEye geometry.

import { ScanEye } from "lucide-react";

export function ArgusLogo({
  size = 28,
  className = "",
  pulse = false,
}: {
  size?: number;
  className?: string;
  pulse?: boolean;
}) {
  return (
    <span
      className={`relative grid shrink-0 place-items-center rounded-lg bg-gradient-to-br from-sky-500 to-indigo-600 shadow-sm shadow-sky-500/20 ${className}`}
      style={{ width: size, height: size }}
      role="img"
      aria-label="Argus"
    >
      <ScanEye
        size={Math.round(size * 0.62)}
        strokeWidth={2}
        className={`text-white ${pulse ? "animate-pulse" : ""}`}
      />
    </span>
  );
}
