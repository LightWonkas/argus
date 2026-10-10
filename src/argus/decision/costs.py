"""Order-book based cost model with explicit commission and uncertainty."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from argus.core.crypto import OrderBookSnapshot
from argus.core.enums import OrderSide
from argus.core.errors import RiskRejectedError
from argus.core.schemas import DataStatus, utcnow
from argus.core.trading import BookFillEstimate, CommissionSchedule, CostEstimate, FeeSource

_BPS = Decimal("10000")


class FeeAndCostEngine:
    """Estimates fills by walking the book; never deducts spread/slippage twice."""

    @staticmethod
    def simulate_market_fill(
        book: OrderBookSnapshot,
        *,
        side: OrderSide,
        quantity: Decimal,
    ) -> BookFillEstimate:
        if quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if book.data_status is not DataStatus.OK or book.crossed:
            raise RiskRejectedError("order book did not pass the Data Quality Gate")
        levels = book.asks if side is OrderSide.BUY else book.bids
        ordered = sorted(levels, key=lambda level: level.price, reverse=side is OrderSide.SELL)
        ordered = [level for level in ordered if level.quantity > 0]
        if not ordered:
            raise RiskRejectedError("no usable depth on this side of the order book")

        remaining = quantity
        filled = Decimal("0")
        notional = Decimal("0")
        for level in ordered:
            take = min(remaining, level.quantity)
            filled += take
            notional += take * level.price
            remaining -= take
            if remaining == 0:
                break
        average = notional / filled if filled > 0 else None
        best = ordered[0].price
        if average is None:
            slippage = None
        elif side is OrderSide.BUY:
            slippage = max((average - best) / best * _BPS, Decimal("0"))
        else:
            slippage = max((best - average) / best * _BPS, Decimal("0"))
        return BookFillEstimate(
            side=side,
            requested_quantity=quantity,
            filled_quantity=filled,
            average_price=average,
            best_price=best,
            notional=notional,
            slippage_bps=slippage,
            fully_fillable=remaining == 0,
        )

    def estimate_round_trip(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        entry_book: OrderBookSnapshot,
        expected_exit_book: OrderBookSnapshot,
        commission: CommissionSchedule,
        minimum_net_edge_bps: Decimal,
        uncertainty_buffer_bps: Decimal,
        additional_execution_buffer_bps: Decimal,
        now: datetime | None = None,
    ) -> CostEstimate:
        at = now or utcnow()
        if commission.source is FeeSource.NO_DATA:
            raise RiskRejectedError("commission data NO_DATA")
        if commission.valid_until < at:
            raise RiskRejectedError("commission data stale")
        if commission.taker_rate is None:
            raise RiskRejectedError("taker commission unknown")

        entry = self.simulate_market_fill(entry_book, side=OrderSide.BUY, quantity=quantity)
        exit_fill = self.simulate_market_fill(
            expected_exit_book,
            side=OrderSide.SELL,
            quantity=quantity,
        )
        if not entry.fully_fillable or not exit_fill.fully_fillable:
            raise RiskRejectedError("requested quantity cannot be fully filled on the order book")
        assert entry.average_price is not None
        assert exit_fill.average_price is not None

        entry_fee = entry.notional * commission.taker_rate
        exit_fee = exit_fill.notional * commission.taker_rate
        buffer_rate = additional_execution_buffer_bps / _BPS
        additional_buffer = (entry.notional + exit_fill.notional) * buffer_rate
        entry_cost = entry.notional + entry_fee + additional_buffer / 2
        exit_proceeds = max(exit_fill.notional - exit_fee - additional_buffer / 2, Decimal("0"))
        net_pnl = exit_proceeds - entry_cost
        net_return = net_pnl / entry_cost
        net_edge_bps = net_return * _BPS
        required = minimum_net_edge_bps + uncertainty_buffer_bps

        entry_mid = self._mid(entry_book)
        exit_mid = self._mid(expected_exit_book)
        slippage_quote = abs(entry.notional - entry_mid * quantity) + abs(
            exit_fill.notional - exit_mid * quantity
        )
        spread_quote = (
            self._spread(entry_book) * quantity / 2
            + self._spread(expected_exit_book) * quantity / 2
        )
        return CostEstimate(
            symbol=symbol.upper(),
            quantity=quantity,
            expected_buy_fill_price=entry.average_price,
            expected_sell_fill_price=exit_fill.average_price,
            expected_entry_commission_quote=entry_fee,
            expected_exit_commission_quote=exit_fee,
            estimated_slippage_quote=slippage_quote,
            spread_cost_quote=spread_quote,
            additional_execution_buffer=additional_buffer,
            expected_entry_cost=entry_cost,
            expected_exit_proceeds=exit_proceeds,
            expected_net_pnl=net_pnl,
            expected_net_return=net_return,
            expected_net_edge_bps=net_edge_bps,
            minimum_required_edge_bps=required,
            passes_edge=net_edge_bps > required,
            fee_source=commission.source,
            calculated_at=at,
        )

    @staticmethod
    def _mid(book: OrderBookSnapshot) -> Decimal:
        if book.best_bid is None or book.best_ask is None:
            raise RiskRejectedError("order book mid-price cannot be computed")
        return (book.best_bid + book.best_ask) / 2

    @staticmethod
    def _spread(book: OrderBookSnapshot) -> Decimal:
        if book.best_bid is None or book.best_ask is None:
            raise RiskRejectedError("order book spread cannot be computed")
        return book.best_ask - book.best_bid
