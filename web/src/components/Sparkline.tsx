// Tiny trend sparkline — a single SVG polyline from the close series. Lightweight for inline
// (watchlist) use; not lightweight-charts. Direction color: last ≥ first → green, else red (not
// color-blind friendly on its own, so it is only a hint; the numeric 24h% column carries the sign).

export function Sparkline({
  values,
  width = 72,
  height = 22,
}: {
  values: number[];
  width?: number;
  height?: number;
}) {
  if (values.length < 2) {
    return <span className="text-xs text-zinc-500">—</span>;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1; // flat series → avoid division by zero
  const stepX = width / (values.length - 1);

  const points = values
    .map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const up = values[values.length - 1] >= values[0];
  const color = up ? "#10b981" : "#ef4444"; // emerald / red

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className="overflow-visible"
    >
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth={1.25}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
