"""Spot-only deterministic allocation engine bound to the mission capital envelope."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from argus.config.models import PortfolioPolicyConfig
from argus.core.trading import (
    AllocationProposal,
    AllocationStatus,
    ExchangeFilterSet,
    MissionLedgerSnapshot,
)


def _decimal(value: float | int | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class PortfolioAllocator:
    POLICY_VERSION = "portfolio-risk-v1"

    def __init__(self, config: PortfolioPolicyConfig) -> None:
        self._config = config

    def allocate_spot_buy(
        self,
        *,
        ledger: MissionLedgerSnapshot,
        symbol: str,
        entry_price: Decimal,
        stop_price: Decimal,
        filters: ExchangeFilterSet,
        estimated_depth_quote: Decimal,
        round_trip_fee_rate: Decimal,
        open_position_count: int,
        current_symbol_value: Decimal = Decimal("0"),
        btc_correlation: Decimal | None = None,
    ) -> AllocationProposal:
        reasons: list[str] = []
        symbol = symbol.upper()
        if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
            reasons.append("for a spot long the stop must be positive and below entry")
        if not filters.complete_for_spot_order:
            reasons.append("exchange filters incomplete")
        if symbol in ledger.external_inventory_symbols:
            reasons.append("external inventory cannot be separated for this symbol")
        if open_position_count >= self._config.maximum_positions:
            reasons.append("maximum concurrent position count")
        if (
            symbol != "BTCUSDT"
            and btc_correlation is not None
            and btc_correlation > _decimal(self._config.maximum_btc_correlation)
        ):
            reasons.append("BTC correlation limit")
        if estimated_depth_quote <= 0:
            reasons.append("no liquidity depth")
        if round_trip_fee_rate < 0:
            reasons.append("commission rate cannot be negative")
        if reasons:
            return self._rejected(ledger, symbol, entry_price, stop_price, reasons)

        risk_budget = ledger.mission_equity * _decimal(self._config.risk_per_trade_pct) / 100
        risk_per_unit = entry_price - stop_price
        risk_quantity = risk_budget / risk_per_unit
        reserve = ledger.capital_limit * _decimal(self._config.cash_reserve_pct) / 100
        cash_cap = max(ledger.available_cash - reserve, Decimal("0"))
        weight_cap = (
            ledger.mission_equity * _decimal(self._config.maximum_symbol_weight_pct) / 100
            - current_symbol_value
        )
        liquidity_cap = (
            estimated_depth_quote * _decimal(self._config.liquidity_participation_pct) / 100
        )
        max_notional = min(cash_cap, max(weight_cap, Decimal("0")), liquidity_cap)
        quantity = min(risk_quantity, max_notional / entry_price)
        assert filters.lot_step is not None
        quantity = (quantity / filters.lot_step).to_integral_value(rounding=ROUND_DOWN)
        quantity *= filters.lot_step
        notional = quantity * entry_price
        fee_reserve = notional * round_trip_fee_rate

        if quantity <= 0:
            reasons.append("quantity is zero after filters")
        if filters.min_quantity is not None and quantity < filters.min_quantity:
            reasons.append("LOT_SIZE minimum quantity")
        if filters.max_quantity is not None and quantity > filters.max_quantity:
            reasons.append("LOT_SIZE maximum quantity")
        if filters.min_notional is not None and notional < filters.min_notional:
            reasons.append("MIN_NOTIONAL")
        if filters.max_notional is not None and notional > filters.max_notional:
            reasons.append("NOTIONAL maximum")
        if notional + fee_reserve > ledger.available_cash:
            reasons.append("mission available cash limit")
        if reasons:
            return self._rejected(ledger, symbol, entry_price, stop_price, reasons)
        return AllocationProposal(
            mission_id=ledger.mission_id,
            symbol=symbol,
            status=AllocationStatus.PROPOSED,
            quantity=quantity,
            reference_price=entry_price,
            stop_price=stop_price,
            notional=notional,
            risk_amount=quantity * risk_per_unit,
            fee_reserve=fee_reserve,
            policy_version=self.POLICY_VERSION,
        )

    def _rejected(
        self,
        ledger: MissionLedgerSnapshot,
        symbol: str,
        entry_price: Decimal,
        stop_price: Decimal,
        reasons: list[str],
    ) -> AllocationProposal:
        return AllocationProposal(
            mission_id=ledger.mission_id,
            symbol=symbol,
            status=AllocationStatus.REJECTED,
            reference_price=entry_price,
            stop_price=stop_price,
            reasons=tuple(reasons),
            policy_version=self.POLICY_VERSION,
        )
