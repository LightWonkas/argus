"""EVDS macro feed tests — adapter + trend + store + pipeline (no network, no borsapy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.core.errors import AdapterError
from argus.ingestion.macro import MacroAdapter, derive_trend
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)


def _obs(values: list[float]) -> list[tuple[datetime, float]]:
    return [(NOW - timedelta(days=len(values) - 1 - i), v) for i, v in enumerate(values)]


# ── derive_trend (pure) ──────────────────────────────────────────────────────


def test_derive_trend_up_down_flat() -> None:
    assert derive_trend(_obs([10.0, 11.0])) == 1.0
    assert derive_trend(_obs([11.0, 10.0])) == -1.0
    assert derive_trend(_obs([10.0, 10.0])) is None  # no change → no direction is invented
    assert derive_trend(_obs([10.0])) is None  # single observation → no trend can be derived
    assert derive_trend([]) is None


# ── MacroAdapter ─────────────────────────────────────────────────────────────


class FakeEVDS:
    """Fake provider mapping series code → observations (supports failing codes)."""

    def __init__(self, data: dict[str, list[float]], *, boom: set[str] | None = None) -> None:
        self._data = data
        self._boom = boom or set()

    def series(self, code: str, *, lookback: int) -> list[tuple[datetime, float]]:
        if code in self._boom:
            raise RuntimeError("EVDS 403")
        return _obs(self._data.get(code, []))[-lookback:]


_SERIES_MAP = {"usdtry": "FX", "policy_rate": "RATE", "cpi_yoy": "CPI"}


def test_fetch_values_trends_and_real_rate() -> None:
    adapter = MacroAdapter(
        FakeEVDS({"FX": [32.0, 33.0], "RATE": [50.0, 47.5], "CPI": [65.0, 62.0]})
    )
    snap = adapter.fetch(_SERIES_MAP)
    assert snap.values["usdtry"] == 33.0
    assert snap.trends["usdtry"] == 1.0  # exchange rate rising
    assert snap.trends["policy_rate"] == -1.0
    # Real rate is computed [A]: 47.5 − 62.0 = −14.5. Policy −1 and CPI −1 give a
    # composite of 0, so there is no trend, but the VALUE is still present.
    assert snap.values["real_rate"] == pytest.approx(-14.5)
    assert "real_rate" not in snap.trends  # composite 0 → no direction is invented
    assert snap.provenance.source == "evds"


def test_fetch_real_rate_trend_when_divergent() -> None:
    # Rate ↑, inflation ↓ → real rate clearly ↑ (+1).
    adapter = MacroAdapter(
        FakeEVDS({"FX": [32.0, 32.0], "RATE": [47.5, 50.0], "CPI": [65.0, 62.0]})
    )
    snap = adapter.fetch(_SERIES_MAP)
    assert snap.trends["real_rate"] == 1.0


def test_fetch_one_series_failure_does_not_break_others() -> None:
    adapter = MacroAdapter(FakeEVDS({"FX": [32.0, 33.0], "CPI": [65.0, 62.0]}, boom={"RATE"}))
    snap = adapter.fetch(_SERIES_MAP)
    assert "usdtry" in snap.values and "cpi_yoy" in snap.values
    assert "policy_rate" not in snap.values  # failing series is absent — not invented
    assert "real_rate" not in snap.values  # no rate → real rate cannot be computed


def test_fetch_all_failed_raises() -> None:
    adapter = MacroAdapter(FakeEVDS({}, boom=set(_SERIES_MAP.values())))
    with pytest.raises(AdapterError):
        adapter.fetch(_SERIES_MAP)  # NO silent empty snapshot


# ── store: save_macro / load_macro_trends ────────────────────────────────────


def test_store_macro_roundtrip_and_freshness() -> None:
    store = SqliteStore()
    n = store.save_macro({"usdtry": 33.0, "cpi_yoy": 62.0}, {"usdtry": 1.0, "cpi_yoy": -1.0})
    assert n == 2
    trends = store.load_macro_trends(max_age_h=48)
    assert trends == {"usdtry": 1.0, "cpi_yoy": -1.0}
    # Staleness: read far in the future → None (NO_DATA; no direction from stale macro).
    later = store.load_macro_trends(max_age_h=48, now=NOW + timedelta(days=365 * 10))
    assert later is None
    store.close()


def test_store_macro_trendless_value_persists_but_not_in_trends() -> None:
    # The VALUE is always written (like the F&G level); the trend context does not see
    # directionless metrics → none leak into MacroIntermarketAgent.
    store = SqliteStore()
    n = store.save_macro({"usdtry": 33.0}, {})  # no trend → value stored with trend=0
    assert n == 1
    assert store.load_macro_trends() is None  # directionless → not in the trend context
    values = store.load_macro_values()
    assert values is not None and values["usdtry"] == 33.0
    store.close()


# ── pipeline: macro fills the WatchLoop context, agent leaves NO_DATA ────────


def test_macro_reaches_agent_via_context() -> None:
    from decimal import Decimal

    from argus.brain.agents import AnalysisContext, MacroIntermarketAgent
    from argus.core.contracts import OHLCVBar, OHLCVSeries
    from argus.core.enums import AssetClass, Stance, Timeframe
    from argus.core.schemas import Provenance

    bar = OHLCVBar(
        timestamp=NOW,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )
    series = OHLCVSeries(
        symbol="GLDTR",
        timeframe=Timeframe.D1,
        bars=(bar,),
        provenance=Provenance(source="t", timestamp=NOW, confidence=0.9),
    )
    # usdtry ↑ → supports a TRY-denominated gold ETF (direct relation) → BULLISH.
    ctx = AnalysisContext(
        symbol="GLDTR",
        timeframe=Timeframe.D1,
        series=series,
        asset_class=AssetClass.METAL,
        macro={"usdtry": 1.0},
    )
    opinion = MacroIntermarketAgent().assess(ctx)
    assert opinion.stance is Stance.BULLISH
    assert "macro:usdtry" in opinion.evidence_sources
