"""Quote/indicator row (api.quotes) + /api/quotes endpoint tests — offline."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.quotes import QuoteRow, build_quote_row
from argus.config.models import WatchlistItem
from argus.core.contracts import OHLCVBar, OHLCVSeries, Proposal
from argus.core.enums import AssetClass, Stance, Timeframe
from argus.core.schemas import Provenance

NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


def _item(symbol: str = "BTCUSDT") -> WatchlistItem:
    return WatchlistItem(symbol=symbol, asset_class=AssetClass.CRYPTO, tier="watchlist")


def _series(
    closes: Sequence[float], *, last_ts: datetime, tf: Timeframe = Timeframe.H1
) -> OHLCVSeries:
    bars: list[OHLCVBar] = []
    n = len(closes)
    for i, c in enumerate(closes):
        ts = last_ts - timedelta(seconds=tf.seconds * (n - 1 - i))
        open_ = closes[i - 1] if i > 0 else c
        bars.append(
            OHLCVBar(
                timestamp=ts,
                open=open_,
                high=max(open_, c) + 1,
                low=min(open_, c) - 1,
                close=c,
                volume=1,
            )
        )
    prov = Provenance(source="binance", timestamp=last_ts, confidence=0.9)
    return OHLCVSeries(symbol="BTCUSDT", timeframe=tf, bars=tuple(bars), provenance=prov)


# --- build_quote_row ---


def test_quote_row_no_series_is_pending() -> None:
    row = build_quote_row(_item(), None, now=NOW)
    assert row.price is None
    assert row.rsi is None
    assert row.freshness == "none"
    assert row.bar_count == 0


def test_quote_row_computes_indicators() -> None:
    closes = [100.0 + i for i in range(40)]
    row = build_quote_row(_item(), _series(closes, last_ts=NOW), now=NOW)
    assert row.price == 139.0
    assert row.rsi is not None  # warmed up
    assert row.macd is not None
    assert row.atr is not None
    assert row.bar_count == 40
    assert row.freshness == "fresh"  # last candle is now


def test_quote_row_stale_when_old() -> None:
    closes = [100.0 + i for i in range(40)]
    # Last candle 5 hours ago (1h × stale_factor 3 = 3-hour threshold) → stale.
    old = NOW - timedelta(hours=5)
    row = build_quote_row(_item(), _series(closes, last_ts=old), now=NOW)
    assert row.freshness == "stale"


def test_quote_row_24h_change() -> None:
    # 1h candles: 24 candles back = 24 hours. 100→124 → +24%.
    closes = [100.0 + i for i in range(25)]
    row = build_quote_row(_item(), _series(closes, last_ts=NOW), now=NOW)
    assert row.change_pct_24h is not None
    assert round(row.change_pct_24h, 1) == 24.0


def test_quote_row_short_series_no_change() -> None:
    # Fewer than 24 candles → no 24h change can be computed (None, nothing fabricated).
    row = build_quote_row(_item(), _series([100.0, 101.0, 102.0], last_ts=NOW), now=NOW)
    assert row.change_pct_24h is None


# --- /api/quotes endpoint ---


def _fake_quotes() -> list[QuoteRow]:
    return [
        QuoteRow(
            symbol="BTCUSDT",
            asset_class="crypto",
            tier="watchlist",
            price=139.0,
            change_pct_24h=2.5,
            rsi=58.0,
            macd=1.2,
            macd_signal=0.9,
            atr=3.4,
            freshness="fresh",
            bar_time=NOW,
            bar_count=40,
        ),
        QuoteRow(
            symbol="XAUUSD",
            asset_class="metal",
            tier="watchlist",
            price=None,
            change_pct_24h=None,
            rsi=None,
            macd=None,
            macd_signal=None,
            atr=None,
            freshness="none",
            bar_time=None,
            bar_count=0,
        ),
    ]


def test_quotes_endpoint() -> None:
    app = create_app(quotes_provider=_fake_quotes)  # no auth → dev
    client = TestClient(app)
    r = client.get("/api/quotes")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["symbol"] == "BTCUSDT"
    assert body[0]["rsi"] == 58.0
    assert body[0]["freshness"] == "fresh"
    # The no-data row is honestly None + none.
    assert body[1]["price"] is None
    assert body[1]["freshness"] == "none"


def test_quotes_requires_auth() -> None:
    app = create_app(quotes_provider=_fake_quotes, users={"ada": "scrypt$x"})
    assert TestClient(app).get("/api/quotes").status_code == 401


# --- /api/ohlcv/{symbol} (chart candle series) ---


def test_ohlcv_returns_candles() -> None:
    series = _series([100.0, 101.0, 102.0], last_ts=NOW)
    app = create_app(ohlcv_reader=lambda s, tf=None: series if s == "BTCUSDT" else None)
    r = TestClient(app).get("/api/ohlcv/BTCUSDT")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["timeframe"] == "1h"
    assert len(body["candles"]) == 3
    c = body["candles"][0]
    assert {"time", "open", "high", "low", "close", "volume"} <= set(c)
    assert isinstance(c["time"], int)  # epoch saniye (lightweight-charts)


def test_ohlcv_unknown_symbol_404() -> None:
    app = create_app(ohlcv_reader=lambda s, tf=None: None)  # not in config → None
    r = TestClient(app).get("/api/ohlcv/HACKER")
    assert r.status_code == 404


def test_ohlcv_empty_series_404() -> None:
    empty = _series([], last_ts=NOW) if False else None
    # Empty series (no candles) → DATA PENDING → 404.
    app = create_app(ohlcv_reader=lambda s, tf=None: empty)
    assert TestClient(app).get("/api/ohlcv/BTCUSDT").status_code == 404


def test_ohlcv_requires_auth() -> None:
    series = _series([100.0], last_ts=NOW)
    app = create_app(ohlcv_reader=lambda s, tf=None: series, users={"ada": "scrypt$x"})
    assert TestClient(app).get("/api/ohlcv/BTCUSDT").status_code == 401


# --- /api/ohlcv-batch (watchlist sparkline — N symbols in one request) ---


def test_ohlcv_batch_returns_closes_per_symbol() -> None:
    data = {
        "BTCUSDT": _series([100.0, 101.0, 102.0], last_ts=NOW),
        "ETHUSDT": _series([50.0, 51.0], last_ts=NOW),
    }
    app = create_app(ohlcv_reader=lambda s, tf=None: data.get(s))
    r = TestClient(app).get("/api/ohlcv-batch?symbols=BTCUSDT,ETHUSDT")
    assert r.status_code == 200
    items = {i["symbol"]: i["closes"] for i in r.json()["items"]}
    assert items["BTCUSDT"] == [100.0, 101.0, 102.0]
    assert items["ETHUSDT"] == [50.0, 51.0]


def test_ohlcv_batch_skips_unknown_symbols_partial_response() -> None:
    # Unknown/no-data symbols are not a 404 — skipped silently (other sparklines still draw).
    app = create_app(
        ohlcv_reader=lambda s, tf=None: (
            _series([100.0, 101.0], last_ts=NOW) if s == "BTCUSDT" else None
        )
    )
    r = TestClient(app).get("/api/ohlcv-batch?symbols=BTCUSDT,HACKER")
    assert r.status_code == 200
    syms = [i["symbol"] for i in r.json()["items"]]
    assert syms == ["BTCUSDT"]  # HACKER skipped


def test_ohlcv_batch_limits_n_closes() -> None:
    # n caps the number of latest closes (short sparkline). Prices are positive (OHLCVBar > 0).
    app = create_app(
        ohlcv_reader=lambda s, tf=None: _series([float(100 + i) for i in range(100)], last_ts=NOW)
    )
    r = TestClient(app).get("/api/ohlcv-batch?symbols=BTCUSDT&n=10")
    closes = r.json()["items"][0]["closes"]
    assert len(closes) == 10 and closes[-1] == 199.0  # son 10


def test_ohlcv_batch_requires_auth() -> None:
    app = create_app(
        ohlcv_reader=lambda s, tf=None: _series([1.0], last_ts=NOW), users={"ada": "scrypt$x"}
    )
    assert TestClient(app).get("/api/ohlcv-batch?symbols=BTCUSDT").status_code == 401


# --- /api/proposal/{symbol} measurement block (threshold = ref ± k×ATR; NOT a target) ---


def test_proposal_includes_measurement_when_atr_available() -> None:
    closes = [100.0 + i for i in range(40)]  # ATR(14) warms up
    series = _series(closes, last_ts=NOW)
    app = create_app(ohlcv_reader=lambda s, tf=None: series if s == "BTCUSDT" else None)
    r = TestClient(app).get("/api/proposal/BTCUSDT")
    assert r.status_code == 200
    m = r.json()["measurement"]
    assert m is not None
    assert m["ref_price"] == 139.0  # last close (PIT reference)
    assert m["atr"] > 0
    assert m["threshold_abs"] == m["k_atr"] * m["atr"]
    assert m["horizon_bars"] >= 1


def test_proposal_measurement_none_when_atr_unwarmed() -> None:
    # 3 candles < ATR period → no threshold is made up, measurement is null.
    series = _series([100.0, 101.0, 102.0], last_ts=NOW)
    app = create_app(ohlcv_reader=lambda s, tf=None: series)
    r = TestClient(app).get("/api/proposal/BTCUSDT")
    assert r.status_code == 200
    assert r.json()["measurement"] is None


def test_proposal_provider_returns_recorded_watch_result_without_recompute() -> None:
    stored = Proposal(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        direction=Stance.BEARISH,
        strength=73,
        confluence_score=67,
        rationale="recorded by the watch loop",
    )

    def must_not_read_ohlcv(symbol: str, tf: str | None = None) -> OHLCVSeries | None:
        raise AssertionError("the panel must not recompute when a stored proposal exists")

    app = create_app(
        proposal_provider=lambda symbol: stored if symbol == "BTCUSDT" else None,
        ohlcv_reader=must_not_read_ohlcv,
    )
    response = TestClient(app).get("/api/proposal/BTCUSDT")
    assert response.status_code == 200
    assert response.json()["rationale"] == "recorded by the watch loop"
    assert response.json()["direction"] == "bearish"
    assert response.json()["measurement"] is None


def test_ohlcv_tf_param_forwarded_and_unknown_tf_404() -> None:
    # ?tf= is forwarded to the reader; unknown TF → reader None → 404 (no silent fallback).
    series = _series([100.0, 101.0], last_ts=NOW)
    seen: list[str | None] = []

    def reader(symbol: str, tf: str | None = None) -> object | None:
        seen.append(tf)
        return series if tf in (None, "1h") else None

    app = create_app(ohlcv_reader=reader)  # type: ignore[arg-type]
    client = TestClient(app)
    assert client.get("/api/ohlcv/BTCUSDT?tf=1h").status_code == 200
    assert client.get("/api/ohlcv/BTCUSDT?tf=4h").status_code == 404  # not in config
    assert seen == ["1h", "4h"]  # parametre okuyucuya aynen gitti
