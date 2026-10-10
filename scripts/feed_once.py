#!/usr/bin/env python
"""One-shot data feed — scans the watchlist once and writes real OHLCV to the PIT store.

Purpose: fill the web cockpit (``/api/quotes``, charts) with **real data** without setting up
the Telegram/watch daemon. The cockpit does NOT collect data; it only reads OHLCV from
``SqliteStore``. This script fills that store. It is the bare *data-writing* step of the
``--watch`` daemon — no triggers/analysis/notifications, just fetch prices -> Data Quality
Gate -> store.

Run (inside the virtualenv):
    pip install -e ".[ingestion,bist]"       # ccxt + borsapy
    python scripts/feed_once.py              # feed the config/config.yaml watchlist
    python scripts/feed_once.py --loop 300   # repeat every 300 s (simple heartbeat)

Notes:
- Needs network access (Binance + TradingView/borsapy). An unreachable source is SKIPped and
  the others are still written.
- Symbols are stored under their config name (e.g. BTCUSDT) -> the cockpit reads the same name.
- There is no adapter for fx/equity yet -> those symbols are SKIPped (nothing is fabricated).
- Prints no secrets, performs NO real-money operation, sends no notifications.
"""

from __future__ import annotations

import argparse
import sys
import time

# The Windows console may use a legacy code page that fails on non-ASCII output -> UTF-8 stdio.
if sys.platform == "win32":  # pragma: no cover
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from argus.config.loader import ConfigLoader
from argus.config.models import WatchlistItem
from argus.config.settings import get_settings
from argus.core.enums import AssetClass, Timeframe
from argus.core.schemas import utcnow
from argus.ingestion.base import MarketDataAdapter
from argus.interface.factory import _SQLITE_PREFIX, build_adjuster, to_quality_gate
from argus.platform.adjust import derive_corporate_actions
from argus.storage.sqlite import SqliteStore


def _build_store() -> SqliteStore:
    """Build the persistent store when ``database_url`` is SQLite (the file the cockpit reads)."""
    url = get_settings().database_url
    if not url.startswith(_SQLITE_PREFIX):
        raise SystemExit(f"database_url is not SQLite ({url}) — feed_once only writes to SQLite.")
    target = url[len(_SQLITE_PREFIX) :] or ":memory:"
    if target not in (":memory:", ""):
        from pathlib import Path

        Path(target).parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(target)


def _adapters() -> dict[AssetClass, MarketDataAdapter]:
    """Live adapters (lazy). Only installed/working classes; otherwise the symbol is SKIPped.

    Metals (GLDTR/GMSTR etc.) are ETFs traded on BIST -> the same borsapy adapter.
    """
    from argus.ingestion.borsapy_provider import bist_adapter
    from argus.ingestion.crypto import binance_adapter

    bist = bist_adapter()
    return {
        AssetClass.CRYPTO: binance_adapter(),
        AssetClass.BIST: bist,
        AssetClass.METAL: bist,
    }


def feed_once(*, verbose: bool = True, limit_override: int | None = None) -> tuple[int, int, int]:
    """Feed the watchlist once. Returns the (written, skipped, failed) counters.

    Every symbol is fed on ALL of its configured timeframes (the loop default when empty).
    ``limit_override``: deep-history backfill (--limit) — Binance caps a single request at
    ~1000 bars (no pagination here), borsapy fetches by start date.
    """
    cfg = get_settings()
    runtime = ConfigLoader().get()
    store = _build_store()
    adapters = _adapters()
    quality = to_quality_gate(runtime.quality)
    default_tf = Timeframe(runtime.loop.timeframe)
    limit = limit_override or runtime.loop.limit

    # Union of watchlist + holdings symbols (the cockpit reads both).
    items: dict[str, WatchlistItem] = {i.symbol: i for i in runtime.watchlist}
    for h in runtime.holdings:
        items.setdefault(h.symbol, WatchlistItem(symbol=h.symbol, asset_class=h.asset_class))

    written = skipped = failed = 0
    for item in items.values():
        adapter = adapters.get(item.asset_class)
        if adapter is None:
            skipped += 1
            if verbose:
                print(f"  ⊘ {item.symbol:12} SKIP — no adapter for {item.asset_class.value}")
            continue
        timeframes = [Timeframe(t) for t in item.timeframes] or [default_tf]
        for timeframe in timeframes:
            label = f"{item.symbol} {timeframe.value}"
            try:
                series = adapter.fetch_ohlcv(item.symbol, timeframe, limit=limit)
                report = quality.check_ohlcv(series, now=utcnow())
                if not report.ok:
                    failed += 1
                    if verbose:
                        reason = "; ".join(i.detail for i in report.issues) or "insufficient"
                        print(f"  ✗ {label:16} quality check failed ({reason}) -> not written")
                    continue
                n = store.save_series(report.series)
                written += 1
                last = report.series.latest
                if verbose and last is not None:
                    print(
                        f"  ✓ {label:16} {n:>4} bars written  "
                        f"(last close {last.close} @ {last.timestamp:%Y-%m-%d %H:%M})"
                    )
                # BIST/metal is stored raw -> derive corporate actions from the adjust=True
                # view and save them (input for read-time adjustment). Fail-safe: a derivation
                # error does not fail the feed (the raw data is already in the store).
                if item.asset_class in (AssetClass.BIST, AssetClass.METAL):
                    try:
                        adjusted = adapter.fetch_ohlcv(
                            item.symbol, timeframe, limit=limit, adjust=True
                        )
                        actions = derive_corporate_actions(report.series, adjusted)
                        if actions:
                            n_act = store.save_corporate_actions(actions)
                            if verbose:
                                print(f"    ↳ {n_act} corporate actions derived/updated")
                    except Exception as exc:
                        if verbose:
                            print(
                                f"    ↳ could not derive corporate actions ({type(exc).__name__})"
                            )
            except Exception as exc:  # network/adapter error (incl. unsupported TF) -> SKIP
                skipped += 1
                if verbose:
                    print(f"  ⊘ {label:16} SKIP — {type(exc).__name__}: {str(exc)[:70]}")

    if verbose:
        print(
            f"\nSummary: {written} written · {skipped} skipped · {failed} failed quality  "
            f"(db: {cfg.database_url})"
        )
    return written, skipped, failed


def feed_news(*, verbose: bool = True) -> int:
    """Fetch KAP disclosures for BIST symbols and write them to the news archive.

    Only real company symbols (THYAO etc.) yield meaningful disclosures; an ETF/index that
    returns nothing is simply skipped. ``save_news`` is idempotent (``(source, ext_id)``
    dedup) -> re-running creates no duplicates. A network/access error skips one symbol and
    the rest continue (fail-safe).
    """
    from argus.ingestion.borsapy_kap import kap_adapter

    runtime = ConfigLoader().get()
    store = _build_store()
    bist_symbols = [i.symbol for i in runtime.watchlist if i.asset_class is AssetClass.BIST]
    if not bist_symbols:
        return 0

    adapter = kap_adapter()
    total_added = 0
    for symbol in bist_symbols:
        try:
            disclosures = adapter.fetch_disclosures(symbol, limit=20)
            added = store.save_news(list(disclosures))
            total_added += added
            if verbose:
                print(f"  ✓ {symbol:12} {len(disclosures):>3} KAP disclosures ({added} new)")
        except Exception as exc:  # access/parse error -> this symbol is skipped
            if verbose:
                print(f"  ⊘ {symbol:12} KAP SKIP — {type(exc).__name__}: {str(exc)[:60]}")
    if verbose:
        print(f"\nNews summary: {total_added} new KAP disclosures archived.")
    return total_added


def feed_macro(*, verbose: bool = True) -> int:
    """Fetch EVDS macro series and write them to the store — input for the macro agent.

    Without a key this is SKIPped (no fabricated macro data). Returns the number of trends
    written. Series codes come from the config (``macro.series``).
    """
    cfg = get_settings()
    if cfg.evds_key is None:
        if verbose:
            print("  ⊘ SKIP — ARGUS_EVDS_KEY is not set (no macro feed; the agent reports NO_DATA)")
        return 0
    from argus.ingestion.macro import evds_adapter

    runtime = ConfigLoader().get()
    store = _build_store()
    try:
        snapshot = evds_adapter(cfg.evds_key.get_secret_value()).fetch(
            runtime.macro.series, lookback=runtime.macro.lookback
        )
    except Exception as exc:  # network/key/code error -> the feed keeps going, reported as-is
        if verbose:
            print(f"  ⊘ EVDS SKIP — {type(exc).__name__}: {str(exc)[:80]}")
        return 0
    n = store.save_macro(snapshot.values, snapshot.trends)
    if verbose:
        pairs = ", ".join(f"{k}={v:+.0f}" for k, v in sorted(snapshot.trends.items()))
        print(f"  ✓ {n} macro trends written ({pairs or 'no direction'})")
    return n


def feed_outcomes(*, verbose: bool = True) -> int:
    """Evaluate pending analyses + proposals against real prices -> record the hit rate.

    Threshold = k x ATR at analysis time (volatility-normalized), two metrics (touch/close),
    and baselines (random/bullish/momentum) are written next to every definitive system
    decision. Items whose horizon has not elapsed wait for the next round; definitive
    no-decision items (skip) are recorded and leave the queue. Returns the number of
    definitive outcomes.
    """
    from argus.storage.evaluator import OutcomeEvaluator

    runtime = ConfigLoader().get()
    oc = runtime.outcome
    store = _build_store()
    summary = OutcomeEvaluator(
        store,
        horizon_bars=oc.horizon_bars,
        k_atr=oc.k_atr,
        atr_period=oc.atr_period,
        # Evaluate on the adjusted view (a split discontinuity must not create a false outcome).
        adjuster=build_adjuster(store),
    ).run()
    if verbose:
        print(
            f"  evaluated={summary.evaluated} "
            f"(hit={summary.correct} · miss={summary.incorrect}) · "
            f"too-early={summary.too_early} · skipped={summary.skipped} · "
            f"baseline-rows={summary.baseline_rows} · proposals={summary.proposals_evaluated}"
        )
    return summary.evaluated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feed_once", description="Argus — feed the watchlist (write OHLCV for the cockpit)"
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SEC",
        help="Repeat continuously at the given interval in seconds (0 = run once)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="BARS",
        help="Deep-history backfill: override the config limit (e.g. 1000 ≈ 4 years of daily "
        "bars; Binance caps a single request at ~1000). 0 = config value",
    )
    args = parser.parse_args(argv)

    print(f"{'═' * 60}\nArgus data feed — {utcnow():%Y-%m-%d %H:%M:%S} UTC\n{'═' * 60}")
    while True:
        print("── OHLCV (prices/indicators) ──")
        feed_once(limit_override=args.limit or None)
        print("\n── News (KAP disclosures) ──")
        feed_news()
        print("\n── Macro (EVDS) ──")
        feed_macro()
        print("\n── Outcome tracking (analysis outcomes -> hit rate) ──")
        feed_outcomes()
        if args.loop <= 0:
            return 0
        print(f"\n… waiting {args.loop} s (Ctrl+C to quit) …\n")
        try:
            time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
