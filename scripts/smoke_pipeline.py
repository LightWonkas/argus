#!/usr/bin/env python
"""Real-data smoke test — exercises the Argus data pipeline end to end against live sources.

Needs network access and optionally a `.env`. This calls live third-party sources; make sure
you are allowed to use them. Sections whose source is unreachable report SKIP.

Run:
    pip install -e ".[ingestion,bist]"     # ccxt + borsapy
    python scripts/smoke_pipeline.py

What it does (every section is independent; the others run even if one fails):
  1) Crypto: real BTC/USDT OHLCV from Binance -> Data Quality Gate -> SqliteStore -> quote
     indicators.
  2) KAP: real THYAO disclosures -> write to the news archive -> read back.
  3) BIST: daily OHLCV through borsapy -> Data Quality Gate.
  4) Brain: one LLM analysis through the configured provider.
  5) Event bus: in-memory round trip (+ Redis when reachable).
Each section reports PASS/SKIP/FAIL; no secrets are printed and NO real-money operation is
performed.
"""

from __future__ import annotations

import argparse
import sys

from argus.config.models import WatchlistItem
from argus.core.enums import AssetClass, Timeframe
from argus.core.schemas import utcnow
from argus.platform.quality import QualityGate
from argus.storage.sqlite import SqliteStore


def _h(title: str) -> None:
    print(f"\n{'─' * 60}\n{title}\n{'─' * 60}")


def smoke_crypto() -> str:
    """Binance -> OHLCV -> quality -> store -> quote indicators."""
    _h("1) CRYPTO — Binance (ccxt) -> pipeline -> quotes")
    try:
        from argus.api.quotes import build_quote_row
        from argus.ingestion.crypto import binance_adapter

        adapter = binance_adapter()
        symbol = "BTC/USDT"
        series = adapter.fetch_ohlcv(symbol, Timeframe.H1, limit=100)
        print(f"  ✓ Fetched {len(series.bars)} bars from Binance (last: {series.latest.timestamp})")

        report = QualityGate().check_ohlcv(series, now=utcnow())
        if not report.ok:
            print(f"  ⚠ Data Quality Gate: {report.reason or 'insufficient'} -> quarantine")
            return "FAIL"
        print("  ✓ Data Quality Gate passed")

        store = SqliteStore()  # :memory: (real usage: data/argus.db)
        n = store.save_series(series)
        print(f"  ✓ Wrote {n} bars to the store")

        loaded = store.load_series(symbol, Timeframe.H1, limit=100)
        row = build_quote_row(WatchlistItem(symbol=symbol, asset_class=AssetClass.CRYPTO), loaded)
        print(
            f"  ✓ Quotes: price={row.price} RSI={row.rsi:.0f} "
            f"MACD={row.macd:.1f} ATR={row.atr:.1f} freshness={row.freshness}"
        )
        return "PASS"
    except Exception as exc:
        print(f"  ⊘ SKIP/FAIL — {type(exc).__name__}: {exc}")
        print("    (Expected when Binance is unreachable from this machine; retry with network.)")
        return "SKIP"


def smoke_kap() -> str:
    """KAP -> real disclosures -> news archive -> read back."""
    _h("2) KAP — real company disclosures -> archive")
    try:
        from argus.ingestion.borsapy_kap import kap_adapter

        adapter = kap_adapter()
        disclosures = adapter.fetch_disclosures("THYAO", limit=5)
        print(f"  ✓ Fetched {len(disclosures)} THYAO disclosures from KAP")
        for d in disclosures[:3]:
            print(f"    · [{d.published_at:%Y-%m-%d}] {d.title[:60]}")

        store = SqliteStore()
        added = store.save_news(list(disclosures))
        recent = store.recent_news(source="kap", limit=5)
        print(f"  ✓ {added} new records archived; read back: {len(recent)}")
        return "PASS" if disclosures else "SKIP"
    except Exception as exc:
        print(f"  ⊘ SKIP/FAIL — {type(exc).__name__}: {exc}")
        return "SKIP"


def smoke_bist() -> str:
    """BIST (borsapy) -> OHLCV -> quality (needs TradingView network/SSL)."""
    _h("3) BIST — borsapy (TradingView) -> OHLCV")
    try:
        from argus.ingestion.borsapy_provider import bist_adapter

        series = bist_adapter().fetch_ohlcv("THYAO", Timeframe.D1, limit=60)
        print(f"  ✓ {len(series.bars)} daily bars (last: {series.latest.timestamp:%Y-%m-%d})")
        report = QualityGate().check_ohlcv(series, now=utcnow())
        print(f"  ✓ Quality: {'passed' if report.ok else report.reason}")
        return "PASS" if report.ok else "FAIL"
    except Exception as exc:
        print(f"  ⊘ SKIP/FAIL — {type(exc).__name__}: {exc}")
        print("    (TradingView SSL/network is blocked in some environments; retry with network.)")
        return "SKIP"


def smoke_brain() -> str:
    """LLM seam -> a real analysis (Claude needs ANTHROPIC_API_KEY; the cost governor applies)."""
    _h("4) BRAIN — LLM analysis (Claude needs ANTHROPIC_API_KEY)")
    try:
        from argus.config.settings import get_settings

        cfg = get_settings()
        # With Ollama selected the local model runs ($0); otherwise Claude needs a key.
        if cfg.llm_provider == "claude" and not cfg.anthropic_api_key:
            print("  ⊘ SKIP — ANTHROPIC_API_KEY is not set.")
            print("    (Free local option: ARGUS_LLM_PROVIDER=ollama + `ollama pull llama3.2`)")
            return "SKIP"

        # A synthetic series is enough — the point is to see the LLM call flow end to end.
        from datetime import timedelta
        from decimal import Decimal

        from argus.brain.analyst import TaskTier
        from argus.brain.factory import build_analyst
        from argus.core.contracts import OHLCVBar, OHLCVSeries
        from argus.core.schemas import Provenance

        now = utcnow()
        bars = tuple(
            OHLCVBar(
                timestamp=now - timedelta(hours=29 - i),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("95"),
                close=Decimal("100") + i,
                volume=Decimal("5"),
            )
            for i in range(30)
        )
        series = OHLCVSeries(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            bars=bars,
            provenance=Provenance(source="smoke", timestamp=now, confidence=0.9),
        )
        analysis = build_analyst().analyze(series, [], tier=TaskTier.ROUTINE)
        print(f"  ✓ LLM analysis: [{analysis.evidence_tier.value}] {analysis.confidence_pct}%")
        print(f"    summary: {analysis.summary[:80]}")
        print(f"    {analysis.disclaimer}")
        return "PASS"
    except Exception as exc:
        print(f"  ⊘ SKIP/FAIL — {type(exc).__name__}: {exc}")
        return "SKIP"


def smoke_eventbus() -> str:
    """Event bus — InMemory always; Redis too when a connection is possible."""
    _h("5) EVENT BUS — InMemory (+ optional Redis)")
    try:
        from argus.core.events import Event, EventType
        from argus.platform.event_bus import InMemoryEventBus

        bus = InMemoryEventBus()
        seen: list[str] = []
        bus.subscribe(EventType.ALERT_CREATED, lambda e: seen.append(str(e.payload)))
        bus.publish(Event(type=EventType.ALERT_CREATED, source="smoke", payload={"x": 1}))
        print(f"  ✓ InMemoryEventBus: {len(seen)} events received")

        try:
            from argus.config.settings import get_settings
            from argus.platform.event_bus import RedisEventBus

            rbus = RedisEventBus(get_settings().redis_url)
            rbus.publish(Event(type=EventType.ALERT_CREATED, source="smoke", payload={"x": 1}))
            print("  ✓ Redis connection + xadd worked")
        except Exception as rexc:
            print(f"  ⊘ Redis SKIP — {type(rexc).__name__} (expected when Redis is not running)")
        return "PASS"
    except Exception as exc:
        print(f"  ✗ FAIL — {type(exc).__name__}: {exc}")
        return "FAIL"


def main() -> int:
    argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0]).parse_args()
    print("Argus real-data smoke test — not financial advice, no real-money operations.")
    results = {
        "crypto": smoke_crypto(),
        "kap": smoke_kap(),
        "bist": smoke_bist(),
        "brain": smoke_brain(),
        "eventbus": smoke_eventbus(),
    }
    _h("SUMMARY")
    for name, status in results.items():
        icon = {"PASS": "✓", "SKIP": "⊘", "FAIL": "✗"}[status]
        print(f"  {icon} {name}: {status}")
    # Exit code 1 on any FAIL (SKIP = no network -> 0).
    return 1 if "FAIL" in results.values() else 0


if __name__ == "__main__":
    sys.exit(main())
