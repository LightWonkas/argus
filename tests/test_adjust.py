"""Read-time adjustment tests — pure derive/apply functions + store + integration.

Scenario: 1:2 split — in the raw series the price halves overnight; in the back-adjusted view
the history BEFORE the split is halved. ``derive_corporate_actions`` finds the action from the
difference; ``apply_actions(raw) == adjusted`` (roundtrip).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.contracts import OHLCVBar, OHLCVSeries, Provenance
from argus.core.enums import Timeframe
from argus.platform.adjust import CorporateAction, apply_actions, derive_corporate_actions
from argus.storage.sqlite import SqliteStore

BASE = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def _series(closes: list[float], *, symbol: str = "THYAO") -> OHLCVSeries:
    bars = tuple(
        OHLCVBar(
            timestamp=BASE + DAY * i,
            open=Decimal(str(c)),
            high=Decimal(str(c * 1.01)),
            low=Decimal(str(c * 0.99)),
            close=Decimal(str(c)),
            volume=Decimal("10"),
        )
        for i, c in enumerate(closes)
    )
    prov = Provenance(source="test", timestamp=bars[-1].timestamp, confidence=0.9)
    return OHLCVSeries(symbol=symbol, timeframe=Timeframe.D1, bars=bars, provenance=prov)


# 1:2 split at candle 5: raw drops 100→50; the adjusted view has the first 5 candles halved.
RAW = [100.0, 101.0, 102.0, 101.0, 100.0, 50.0, 51.0, 52.0]
ADJUSTED = [50.0, 50.5, 51.0, 50.5, 50.0, 50.0, 51.0, 52.0]


def test_derive_finds_split() -> None:
    actions = derive_corporate_actions(_series(RAW), _series(ADJUSTED))
    assert len(actions) == 1
    a = actions[0]
    assert a.symbol == "THYAO"
    assert a.date == BASE + DAY * 5  # first candle the action applies to
    assert abs(a.factor - 0.5) < 1e-6  # ×0.5 on pre-split history


def test_derive_no_actions_when_views_match() -> None:
    # No corporate action means both views match → no action is derived (nothing fabricated).
    assert derive_corporate_actions(_series(RAW), _series(RAW)) == []


def test_derive_tolerates_noise() -> None:
    # Ratio noise below 0.5% does not count as an action.
    noisy = [c * 1.001 for c in RAW]
    assert derive_corporate_actions(_series(RAW), _series(noisy)) == []


def test_apply_roundtrip_reproduces_adjusted_view() -> None:
    raw = _series(RAW)
    actions = derive_corporate_actions(raw, _series(ADJUSTED))
    view = apply_actions(raw, actions)
    for got, expected in zip(view.bars, ADJUSTED, strict=True):
        assert abs(float(got.close) - expected) < 0.01
    assert view.provenance.adjusted is True  # the view is honestly labelled
    # The (raw) series written to the store is UNCHANGED (PIT preserved).
    assert float(raw.bars[0].close) == 100.0


def test_apply_no_actions_is_noop() -> None:
    raw = _series(RAW)
    assert apply_actions(raw, []) is raw


def test_apply_preserves_volume() -> None:
    # Volume is not scaled (a volume factor cannot be reliably derived from the price ratio).
    view = apply_actions(_series(RAW), [CorporateAction("THYAO", BASE + DAY * 5, 0.5)])
    assert all(b.volume == Decimal("10") for b in view.bars)


# ── store: corporate_actions + delete_ohlcv ──────────────────────────────────


def test_store_actions_roundtrip_idempotent() -> None:
    store = SqliteStore()
    actions = [CorporateAction("THYAO", BASE + DAY * 5, 0.5)]
    assert store.save_corporate_actions(actions) == 1
    assert store.save_corporate_actions(actions) == 1  # (symbol,date) upsert — no duplicate
    loaded = store.load_corporate_actions("THYAO")
    assert len(loaded) == 1 and abs(loaded[0].factor - 0.5) < 1e-9
    assert store.load_corporate_actions("BTCUSDT") == []  # no action → empty (nothing fabricated)
    store.close()


def test_delete_ohlcv_removes_only_target_symbol() -> None:
    store = SqliteStore()
    store.save_series(_series(RAW, symbol="THYAO"))
    store.save_series(_series(RAW, symbol="GLDTR"))
    deleted = store.delete_ohlcv("THYAO")
    assert deleted == len(RAW)
    assert store.load_bars("THYAO", Timeframe.D1) == []
    assert len(store.load_bars("GLDTR", Timeframe.D1)) == len(RAW)  # other symbol is kept
    store.close()


# ── integration: the store stays raw, the view is adjusted ───────────────────


def test_store_backed_adjuster_view() -> None:
    from argus.interface.factory import build_adjuster

    store = SqliteStore()
    store.save_series(_series(RAW))  # raw (PIT) written
    store.save_corporate_actions([CorporateAction("THYAO", BASE + DAY * 5, 0.5)])

    adjuster = build_adjuster(store)
    assert adjuster is not None
    view = adjuster(store.load_series("THYAO", Timeframe.D1))
    assert abs(float(view.bars[0].close) - 50.0) < 0.01  # adjusted view
    assert float(store.load_bars("THYAO", Timeframe.D1)[0].close) == 100.0  # store is raw
    # No-op for a symbol without actions.
    store.save_series(_series(RAW, symbol="BTCUSDT"))
    raw_view = adjuster(store.load_series("BTCUSDT", Timeframe.D1))
    assert float(raw_view.bars[0].close) == 100.0
    store.close()
