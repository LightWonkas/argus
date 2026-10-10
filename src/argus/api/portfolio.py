"""Portfolio value/P&L calculation — pure, network-free.

**No execution** in phase 1: holdings live in config (`HoldingItem`) and are entered
manually. Value/P&L is computed from the last price (quotes). P&L is shown in **both TRY and
USD** — both when a USD↔TRY rate is supplied, otherwise only the cost currency (the other is
``None``).

**Collects no new data**: prices come from outside (store-backed quotes).
"""

from __future__ import annotations

from dataclasses import dataclass

from argus.config.models import HoldingItem


@dataclass(frozen=True)
class PositionValue:
    """Value/P&L summary of one holding (both currencies; None when unknown)."""

    symbol: str
    asset_class: str
    owner: str
    quantity: float
    avg_cost: float
    cost_currency: str
    price: float | None  # last price (assumed to be in cost_currency)
    value_try: float | None
    value_usd: float | None
    pnl_try: float | None
    pnl_usd: float | None
    pnl_pct: float | None


@dataclass(frozen=True)
class PortfolioSummary:
    """Portfolio totals + holdings."""

    positions: tuple[PositionValue, ...]
    total_value_try: float | None
    total_value_usd: float | None
    total_pnl_try: float | None
    total_pnl_usd: float | None


def _convert(amount: float, frm: str, to: str, usdtry: float | None) -> float | None:
    """Convert ``frm`` → ``to`` (USD↔TRY). Without a rate, cross-currency → None."""
    if frm == to:
        return amount
    if usdtry is None or usdtry <= 0:
        return None
    if frm == "USD" and to == "TRY":
        return amount * usdtry
    if frm == "TRY" and to == "USD":
        return amount / usdtry
    return None


def build_position(
    item: HoldingItem, price: float | None, *, usdtry: float | None = None
) -> PositionValue:
    """Builds a holding's value/P&L from the holding and its last price.

    ``price`` is assumed to be in ``cost_currency`` (simple enough for the cockpit). Without
    a price, value/P&L are ``None`` (DATA PENDING). P&L is converted to both TRY and USD
    (when a rate is available).
    """
    value: float | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    if price is not None:
        value = price * item.quantity
        cost = item.avg_cost * item.quantity
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else None

    ccy = item.cost_currency
    return PositionValue(
        symbol=item.symbol,
        asset_class=item.asset_class.value,
        owner=item.owner,
        quantity=item.quantity,
        avg_cost=item.avg_cost,
        cost_currency=ccy,
        price=price,
        value_try=_convert(value, ccy, "TRY", usdtry) if value is not None else None,
        value_usd=_convert(value, ccy, "USD", usdtry) if value is not None else None,
        pnl_try=_convert(pnl, ccy, "TRY", usdtry) if pnl is not None else None,
        pnl_usd=_convert(pnl, ccy, "USD", usdtry) if pnl is not None else None,
        pnl_pct=pnl_pct,
    )


def build_portfolio(
    holdings: list[HoldingItem],
    prices: dict[str, float | None],
    *,
    usdtry: float | None = None,
) -> PortfolioSummary:
    """Builds the portfolio summary from holdings + a symbol→price mapping.

    Totals sum only the holdings **whose value is known in that currency**; if none is known
    the total is ``None`` (DATA PENDING — no made-up zero).
    """
    positions = tuple(build_position(h, prices.get(h.symbol), usdtry=usdtry) for h in holdings)

    def _sum(attr: str) -> float | None:
        vals = [getattr(p, attr) for p in positions if getattr(p, attr) is not None]
        return sum(vals) if vals else None

    return PortfolioSummary(
        positions=positions,
        total_value_try=_sum("value_try"),
        total_value_usd=_sum("value_usd"),
        total_pnl_try=_sum("pnl_try"),
        total_pnl_usd=_sum("pnl_usd"),
    )
