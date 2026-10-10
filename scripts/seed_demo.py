#!/usr/bin/env python
"""Seed the store with synthetic candles so the cockpit has something to show offline.

No network and no API keys. For every watchlist symbol in the config this writes a
deterministic synthetic price curve centred on 100.0, tagged with the source
``synthetic-demo``. The numbers are made up: they are not market data and say nothing
about any real asset.

Symbols that already hold candles from another source are left alone, so running this
next to a live feed never overwrites collected data.

Usage:
    python scripts/seed_demo.py              # 300 bars per symbol and timeframe
    python scripts/seed_demo.py --bars 500
"""

from __future__ import annotations

import argparse
import math
import random
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from argus.config.loader import ConfigLoader
from argus.config.settings import get_settings
from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import Timeframe
from argus.core.schemas import Provenance
from argus.storage.sqlite import SqliteStore

SOURCE = "synthetic-demo"
_SQLITE_PREFIX = "sqlite:///"
_BASE_PRICE = 100.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _store() -> SqliteStore:
    url = get_settings().database_url
    if not url.startswith(_SQLITE_PREFIX):
        raise SystemExit(f"database_url is not SQLite ({url}); seed_demo only writes to SQLite.")
    target = url[len(_SQLITE_PREFIX) :]
    if not target or target == ":memory:":
        raise SystemExit("database_url points at an in-memory database; nothing to seed.")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(target)


def _close_at(key: int, index: int) -> float:
    """Synthetic close for an absolute bar index: slow waves plus a little per-bar noise.

    The value depends only on the symbol/timeframe key and the bar index, so re-seeding
    later extends the same curve instead of rewriting history.
    """
    phase = (key % 360) * math.pi / 180
    wave = 0.08 * math.sin(2 * math.pi * index / 97 + phase)
    wave += 0.04 * math.sin(2 * math.pi * index / 23 + 2 * phase)
    noise = random.Random(key * 1_000_003 + index).uniform(-0.006, 0.006)
    return _BASE_PRICE * math.exp(wave + noise)


def _price(value: float) -> Decimal:
    return Decimal(f"{value:.4f}")


def synthetic_series(
    symbol: str, timeframe: Timeframe, bars: int, *, now: datetime | None = None
) -> OHLCVSeries:
    """Build the synthetic series ending at the last fully closed candle before ``now``."""
    key = zlib.crc32(f"{symbol}:{timeframe.value}".encode())
    step = timedelta(seconds=timeframe.seconds)
    last_index = ((now or datetime.now(UTC)) - _EPOCH) // step - 1

    candles: list[OHLCVBar] = []
    for index in range(last_index - bars + 1, last_index + 1):
        open_ = _price(_close_at(key, index - 1))
        close = _price(_close_at(key, index))
        rng = random.Random(key * 7_919 + index)
        high = max(open_, close) * Decimal(f"{1 + rng.uniform(0, 0.004):.6f}")
        low = min(open_, close) * Decimal(f"{1 - rng.uniform(0, 0.004):.6f}")
        candles.append(
            OHLCVBar(
                timestamp=_EPOCH + step * index,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal(f"{rng.uniform(500, 5000):.2f}"),
            )
        )
    return OHLCVSeries(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(candles),
        provenance=Provenance(source=SOURCE, timestamp=candles[-1].timestamp, confidence=0.0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--bars", type=int, default=300, help="candles per symbol and timeframe")
    args = parser.parse_args(argv)
    if args.bars < 1:
        parser.error("--bars must be positive")

    config = ConfigLoader().load()
    store = _store()
    try:
        written = skipped = 0
        for item in config.watchlist:
            for value in sorted({config.loop.timeframe, *item.timeframes}):
                timeframe = Timeframe(value)
                existing = store.load_series(item.symbol, timeframe, limit=1)
                if existing.bars and existing.provenance.source != SOURCE:
                    skipped += 1
                    continue
                written += store.save_series(synthetic_series(item.symbol, timeframe, args.bars))
        print(f"Seeded {written} synthetic candles for {len(config.watchlist)} symbol(s).")
        if skipped:
            print(f"Left {skipped} series untouched because they hold data from another source.")
        print("Synthetic demo data only. Not market data. Not financial advice.")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
