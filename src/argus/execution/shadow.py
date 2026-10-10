"""Persistent shadow (simulated) trading engine with no live exchange order surface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from argus.config.models import ShadowConfig
from argus.core.crypto import (
    RiskAssessment,
    ShadowFill,
    ShadowOrder,
    ShadowPosition,
    TradeCandidate,
)
from argus.core.enums import (
    OrderSide,
    RiskDecision,
    ShadowOrderStatus,
    ShadowPositionStatus,
)
from argus.core.errors import RiskRejectedError
from argus.core.schemas import utcnow


class ShadowStore(Protocol):
    def save_trade_candidate(self, candidate: TradeCandidate) -> None: ...

    def save_risk_assessment(self, assessment: RiskAssessment) -> None: ...

    def save_shadow_order(self, order: ShadowOrder) -> None: ...

    def save_shadow_fill(self, fill: ShadowFill) -> None: ...

    def save_shadow_position(self, position: ShadowPosition) -> None: ...

    def shadow_positions(self, *, open_only: bool = False) -> list[ShadowPosition]: ...


@dataclass(frozen=True)
class ShadowExecution:
    order: ShadowOrder
    fill: ShadowFill
    position: ShadowPosition


class ShadowEngine:
    """Local simulation only, with bid/ask, cost and partial-fill assumptions."""

    def __init__(self, store: ShadowStore, config: ShadowConfig | None = None) -> None:
        self._store = store
        self.config = config or ShadowConfig()
        self._positions = {
            position.position_id: position for position in store.shadow_positions(open_only=True)
        }

    @property
    def open_positions(self) -> tuple[ShadowPosition, ...]:
        return tuple(self._positions.values())

    @staticmethod
    def _validate_book(bid: Decimal, ask: Decimal) -> None:
        if bid <= 0 or ask <= 0 or bid > ask:
            raise RiskRejectedError("no shadow fill with an invalid/crossed bid-ask")

    def submit(
        self,
        candidate: TradeCandidate,
        assessment: RiskAssessment,
        *,
        quantity: Decimal,
        bid: Decimal,
        ask: Decimal,
        available_quantity: Decimal | None = None,
        now: datetime | None = None,
    ) -> ShadowExecution:
        """Turns a GUARD-approved BUY candidate into a simulated order/fill/position."""
        at = now or utcnow()
        if not self.config.enabled:
            raise RiskRejectedError("shadow engine disabled by config")
        if assessment.candidate_id != candidate.candidate_id:
            raise RiskRejectedError("risk assessment does not belong to this candidate")
        if assessment.decision is not RiskDecision.ALLOW_SHADOW:
            raise RiskRejectedError(
                f"GUARD did not allow shadow simulation: {assessment.decision.value}"
            )
        if candidate.execution_enabled:
            raise RiskRejectedError(
                "a candidate flagged for live execution cannot enter the shadow boundary"
            )
        if candidate.side is not OrderSide.BUY:
            raise RiskRejectedError("the spot-only engine does not open new short/sell positions")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._validate_book(bid, ask)

        modeled_quantity = quantity * Decimal(str(self.config.partial_fill_ratio))
        if available_quantity is not None:
            if available_quantity < 0:
                raise ValueError("available_quantity cannot be negative")
            modeled_quantity = min(modeled_quantity, available_quantity)
        if modeled_quantity <= 0:
            raise RiskRejectedError("no available liquidity; no fill produced")
        filled_quantity = min(quantity, modeled_quantity)
        slip_rate = Decimal(str(self.config.slippage_bps)) / Decimal("10000")
        commission_rate = Decimal(str(self.config.commission_bps)) / Decimal("10000")
        fill_price = ask * (Decimal("1") + slip_rate)
        commission = fill_price * filled_quantity * commission_rate
        slippage_cost = (fill_price - ask) * filled_quantity
        status = (
            ShadowOrderStatus.FILLED
            if filled_quantity == quantity
            else ShadowOrderStatus.PARTIALLY_FILLED
        )
        order = ShadowOrder(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            submitted_at=at,
            status=status,
            filled_quantity=filled_quantity,
        )
        fill = ShadowFill(
            order_id=order.order_id,
            symbol=candidate.symbol,
            side=OrderSide.BUY,
            filled_at=at,
            price=fill_price,
            quantity=filled_quantity,
            commission=commission,
            slippage=slippage_cost,
        )
        expires_at = candidate.expires_at or at + timedelta(seconds=self.config.timeout_s)
        position = ShadowPosition(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            side=OrderSide.BUY,
            opened_at=at,
            quantity=filled_quantity,
            average_entry_price=fill_price,
            mark_price=bid,
            unrealized_pnl=(bid - fill_price) * filled_quantity,
            fees=commission,
            stop_price=candidate.stop_price,
            invalidation=candidate.invalidation,
            expires_at=expires_at,
        )
        self._store.save_trade_candidate(candidate)
        self._store.save_risk_assessment(assessment)
        self._store.save_shadow_order(order)
        self._store.save_shadow_fill(fill)
        self._store.save_shadow_position(position)
        self._positions[position.position_id] = position
        return ShadowExecution(order=order, fill=fill, position=position)

    def mark_to_market(
        self,
        symbol: str,
        *,
        bid: Decimal,
        ask: Decimal,
        now: datetime | None = None,
        invalidation_triggered: bool = False,
    ) -> tuple[ShadowPosition, ...]:
        """Marks to market; on stop/invalidation/timeout closes the simulated position at bid."""
        at = now or utcnow()
        self._validate_book(bid, ask)
        updated: list[ShadowPosition] = []
        for position_id, position in tuple(self._positions.items()):
            if position.symbol != symbol:
                continue
            stop_hit = position.stop_price is not None and bid <= position.stop_price
            timed_out = position.expires_at is not None and at >= position.expires_at
            if stop_hit or timed_out or invalidation_triggered:
                closed = self._close(position, bid=bid, at=at)
                self._positions.pop(position_id, None)
                updated.append(closed)
            else:
                marked = position.model_copy(
                    update={
                        "mark_price": bid,
                        "unrealized_pnl": (bid - position.average_entry_price) * position.quantity,
                    }
                )
                self._positions[position_id] = marked
                self._store.save_shadow_position(marked)
                updated.append(marked)
        return tuple(updated)

    def _close(self, position: ShadowPosition, *, bid: Decimal, at: datetime) -> ShadowPosition:
        slip_rate = Decimal(str(self.config.slippage_bps)) / Decimal("10000")
        commission_rate = Decimal(str(self.config.commission_bps)) / Decimal("10000")
        exit_price = bid * (Decimal("1") - slip_rate)
        exit_fee = exit_price * position.quantity * commission_rate
        order = ShadowOrder(
            candidate_id=position.candidate_id,
            symbol=position.symbol,
            side=OrderSide.SELL,
            quantity=position.quantity,
            submitted_at=at,
            status=ShadowOrderStatus.FILLED,
            filled_quantity=position.quantity,
        )
        fill = ShadowFill(
            order_id=order.order_id,
            symbol=position.symbol,
            side=OrderSide.SELL,
            filled_at=at,
            price=exit_price,
            quantity=position.quantity,
            commission=exit_fee,
            slippage=(bid - exit_price) * position.quantity,
        )
        total_fees = position.fees + exit_fee
        realized = (
            (exit_price - position.average_entry_price) * position.quantity
            - total_fees
            - position.funding_cost
        )
        closed = position.model_copy(
            update={
                "status": ShadowPositionStatus.CLOSED,
                "closed_at": at,
                "mark_price": exit_price,
                "realized_pnl": realized,
                "unrealized_pnl": Decimal("0"),
                "fees": total_fees,
            }
        )
        self._store.save_shadow_order(order)
        self._store.save_shadow_fill(fill)
        self._store.save_shadow_position(closed)
        return closed

    def apply_funding(self, position_id: str, cost: Decimal) -> ShadowPosition:
        """Funding accounting for a perpetual extension; a negative value can be a rebate."""
        if position_id not in self._positions:
            raise KeyError(position_id)
        position = self._positions[position_id]
        updated = position.model_copy(update={"funding_cost": position.funding_cost + cost})
        self._positions[position_id] = updated
        self._store.save_shadow_position(updated)
        return updated
