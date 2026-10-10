#!/usr/bin/env python
"""Argus minimal backtest CLI — deterministic walk-forward report from the PIT store.

Usage (inside the virtualenv; the store must hold data — feed it with scripts/feed_once.py):
    python scripts/backtest.py --symbol BTCUSDT --tf 1d
    python scripts/backtest.py --symbol THYAO --tf 1d --split 0.7 --horizons 1,5,20

Output: ``reports/backtest_<date>_<symbol>_<tf>.md`` (+ a stdout summary). No LLM -> $0; no
network -> only the store is read. A report is written whatever the outcome — a negative
result is a finding too.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

# The Windows console may use a legacy code page that fails on non-ASCII output -> UTF-8 stdio.
if sys.platform == "win32":  # pragma: no cover
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from argus.core.enums import Timeframe
from argus.decision.backtest import BacktestReport, HorizonSegmentStats, run_backtest

_SEGMENT_LABEL = {"in_sample": "In-sample", "out_of_sample": "Out-of-sample"}


def _fmt(v: float | None, suffix: str = "") -> str:
    return f"{v}{suffix}" if v is not None else "—"


def _metric_cell(s: HorizonSegmentStats, metric: str) -> str:
    stat = s.touch if metric == "touch" else s.close
    delta = s.delta_pts(metric)
    if stat.pct is None:
        return "—"
    delta_txt = f" ({'+' if delta is not None and delta >= 0 else ''}{_fmt(delta)} pts vs random)"
    return f"{stat.pct}% (n={stat.n}){delta_txt if delta is not None else ''}"


def render_report(report: BacktestReport, *, generated_at: datetime) -> str:
    """Render the report as markdown (numbers come from the engine, prose from the template)."""
    lines: list[str] = [
        f"# Backtest report — {report.symbol} ({report.timeframe})",
        "",
        f"> Generated: {generated_at:%Y-%m-%d %H:%M} UTC · deterministic replay (no LLM, $0)",
        "> **Not financial advice. Compute ≠ edge — a negative result is a finding too.**",
        "",
        "## Setup",
        "",
        f"- Bars: {report.n_bars} · warm-up: {report.warmup_bars} bars · "
        f"split: {report.split} (boundary at bar {report.split_index})",
        f"- Threshold: k_atr={report.k_atr} x ATR at signal time (period {report.atr_period}) — "
        "the same yardstick as live outcome tracking",
        "- Data view: "
        + ("**back-adjusted (adjust=True)**" if report.adjusted_series else "**raw (PIT)**")
        + " (Provenance.adjusted)",
        "- Signal: TriggerEngine→deterministic agents→confluence; "
        f"**{len(report.signals)}** signals in total",
        "",
        "## Results (versus baseline)",
        "",
        "| Horizon | Segment | n | Mean return % | Median % | 95% CI | Touch | Close |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in report.stats:
        ci = (
            f"[{s.ci_low_pct}, {s.ci_high_pct}]"
            if s.ci_low_pct is not None and s.ci_high_pct is not None
            else "—"
        )
        lines.append(
            f"| {s.horizon_bars} bars | {_SEGMENT_LABEL[s.segment]} | {s.n_signals} "
            f"| {_fmt(s.mean_ret_pct)} | {_fmt(s.median_ret_pct)} | {ci} "
            f"| {_metric_cell(s, 'touch')} | {_metric_cell(s, 'close')} |"
        )
    lines += [
        "",
        "## Honesty notes",
        "",
        f"- **Multiple testing:** {report.n_tests} cells were tested (horizon x metric x "
        f"segment) -> Bonferroni-corrected threshold alpha' = {report.bonferroni_alpha}. "
        "An isolated positive cell is NOT evidence.",
        "- Signals can overlap (one trend produces several triggers) -> n is not the number of "
        "independent samples; the CI may be optimistic.",
        "- No parameter optimization was done (single-config replay); the IS/OOS split is an "
        "indicator of regime shift.",
        "- Returns are **gross**: commission/slippage/spread are not included.",
    ]
    if report.adjusted_series:
        lines.append(
            "- ⚠️ The series is back-adjusted (BIST adjust=True exception) — a historical "
            "replay may carry a small PIT deviation."
        )
    lines += [
        "",
        "*Argus — this report is a self-assessment; an edge claim requires sufficient n + a "
        "corrected threshold + repeated out-of-sample positivity.*",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backtest", description="Argus deterministic backtest (store -> report)"
    )
    parser.add_argument("--symbol", required=True, help="Symbol in the store (e.g. BTCUSDT)")
    parser.add_argument("--tf", default="1d", help="Timeframe (e.g. 1d, 1h)")
    parser.add_argument("--split", type=float, default=0.7, help="In-sample ratio (0-1)")
    parser.add_argument(
        "--horizons", default="1,5,20", help="Comma-separated horizon list (in bars)"
    )
    parser.add_argument("--limit", type=int, default=5000, help="Bars to read from the store")
    parser.add_argument(
        "--view",
        choices=("adjusted", "raw"),
        default="adjusted",
        help="Data view: adjusted = read-time corporate-action adjustment (indicator "
        "continuity, default) · raw = pure PIT (as stored)",
    )
    parser.add_argument("--out-dir", default="reports", help="Report directory (default: reports)")
    args = parser.parse_args(argv)

    from argus.config.settings import get_settings
    from argus.platform.adjust import apply_actions
    from argus.storage.sqlite import SqliteStore

    url = get_settings().database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        print(f"ERROR: database_url is not SQLite ({url}).", file=sys.stderr)
        return 2
    store = SqliteStore(url[len(prefix) :] or ":memory:")
    try:
        timeframe = Timeframe(args.tf)
        series = store.load_series(args.symbol, timeframe, limit=args.limit)
        if len(series.bars) < 60:
            print(
                f"DATA PENDING: the store holds only {len(series.bars)} bars for "
                f"{args.symbol} {args.tf} (>=60 recommended). Run scripts/feed_once.py first.",
                file=sys.stderr,
            )
            return 1
        if args.view == "adjusted":
            actions = store.load_corporate_actions(args.symbol)
            if actions:
                series = apply_actions(series, actions)
                print(f"Read-time adjustment: applied {len(actions)} corporate actions.")
            else:
                print("No corporate actions on record -> raw = adjusted (no-op).")
        horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
        report = run_backtest(series, split=args.split, horizons=horizons)
    finally:
        store.close()

    now = datetime.now(UTC)
    text = render_report(report, generated_at=now)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"backtest_{now:%Y%m%d}_{args.symbol.replace('/', '-')}_{args.tf}.md"
    out_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Report written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
