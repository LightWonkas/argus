"""Portfolio maths (api.portfolio) + /api/portfolio endpoint tests — offline."""

from __future__ import annotations

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.portfolio import PortfolioSummary, build_portfolio, build_position
from argus.config.models import HoldingItem
from argus.core.enums import AssetClass


def _holding(
    symbol: str = "BTCUSDT", qty: float = 2.0, cost: float = 60000.0, ccy: str = "USD"
) -> HoldingItem:
    return HoldingItem(
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        quantity=qty,
        avg_cost=cost,
        cost_currency=ccy,  # type: ignore[arg-type]
        owner="alice",
    )


# --- build_position ---


def test_position_pnl_positive() -> None:
    p = build_position(_holding(qty=2, cost=60000), price=65000)
    assert p.value_usd == 130000  # 2 × 65000
    assert p.pnl_usd == 10000  # (65000-60000) × 2
    assert round(p.pnl_pct or 0, 2) == 8.33


def test_position_no_price_is_pending() -> None:
    p = build_position(_holding(), price=None)
    assert p.value_usd is None
    assert p.pnl_usd is None
    assert p.pnl_pct is None


def test_position_currency_conversion_with_rate() -> None:
    # USD cost + FX rate 40 → the TRY value is computed.
    p = build_position(_holding(qty=1, cost=100, ccy="USD"), price=120, usdtry=40.0)
    assert p.value_usd == 120
    assert p.value_try == 120 * 40
    assert p.pnl_usd == 20
    assert p.pnl_try == 20 * 40


def test_position_no_rate_other_currency_none() -> None:
    # Without an FX rate the other currency is None (nothing fabricated).
    p = build_position(_holding(ccy="USD"), price=65000, usdtry=None)
    assert p.value_usd is not None
    assert p.value_try is None


# --- build_portfolio ---


def test_portfolio_totals_skip_unknown() -> None:
    holdings = [_holding("BTCUSDT", 1, 60000), _holding("XAUUSD", 1, 2000)]
    prices: dict[str, float | None] = {"BTCUSDT": 65000, "XAUUSD": None}  # no gold price
    summary = build_portfolio(holdings, prices)
    # The total comes only from the holding with a known price (BTC).
    assert summary.total_value_usd == 65000
    assert summary.total_pnl_usd == 5000


def test_portfolio_all_unknown_is_none() -> None:
    summary = build_portfolio([_holding()], {"BTCUSDT": None})
    assert summary.total_value_usd is None  # None, not zero (DATA PENDING)


# --- /api/portfolio endpoint ---


def _fake_portfolio() -> PortfolioSummary:
    holdings = [_holding("BTCUSDT", 2, 60000)]
    return build_portfolio(holdings, {"BTCUSDT": 65000})


def test_portfolio_endpoint() -> None:
    app = create_app(portfolio_provider=_fake_portfolio)  # no auth → dev
    r = TestClient(app).get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert len(body["positions"]) == 1
    assert body["positions"][0]["symbol"] == "BTCUSDT"
    assert body["total_value_usd"] == 130000


def test_portfolio_empty_not_connected() -> None:
    app = create_app(portfolio_provider=lambda: build_portfolio([], {}))
    body = TestClient(app).get("/api/portfolio").json()
    assert body["connected"] is False
    assert body["positions"] == []


def test_portfolio_requires_auth() -> None:
    app = create_app(portfolio_provider=_fake_portfolio, users={"ada": "scrypt$x"})
    assert TestClient(app).get("/api/portfolio").status_code == 401
