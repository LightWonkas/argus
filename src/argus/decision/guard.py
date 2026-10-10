"""Non-bypassable, deterministic GUARD risk engine.

GUARD trusts no LLM output and cannot grant live-order permission. Initially only the
simulation-only ``ALLOW_SHADOW`` is possible, and only after human approval; otherwise the
default is ``REJECT`` or ``REQUIRE_HUMAN_REVIEW``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from argus.config.models import RiskConfig
from argus.core.crypto import RiskAssessment, TradeCandidate
from argus.core.enums import MarketType, OrderSide, RiskDecision
from argus.platform.quality import QualityDecision


@dataclass(frozen=True)
class GuardState:
    portfolio_value: Decimal
    proposed_risk_amount: Decimal | None
    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")
    drawdown_pct: float = 0.0
    concurrent_positions: int = 0
    btc_eth_correlation: float | None = None
    correlated_exposure_pct: float = 0.0
    quote_volume_24h: Decimal | None = None
    spread_bps: float | None = None
    estimated_slippage_bps: float | None = None
    data_age_s: float | None = None
    exchange_rest_healthy: bool = False
    exchange_websocket_healthy: bool = False
    event_blackout: bool = False
    normalized_atr_pct: float | None = None
    cooldown_active: bool = False
    quality_decision: QualityDecision = QualityDecision.REJECT
    human_approved: bool = False


class GuardRiskEngine:
    """Runs every check and explains all rejection reasons in a single result."""

    def __init__(self, policy: RiskConfig | None = None) -> None:
        self.policy = policy or RiskConfig()

    def assess(self, candidate: TradeCandidate, state: GuardState) -> RiskAssessment:
        checks: dict[str, bool | float | str | None] = {}
        reasons: list[str] = []

        if self.policy.kill_switch:
            return RiskAssessment(
                candidate_id=candidate.candidate_id,
                decision=RiskDecision.HALT_SYSTEM,
                reasons=("kill switch active",),
                checks={"kill_switch": False},
                human_review_required=True,
            )

        def require(name: str, condition: bool, reason: str, value: float | str | None) -> None:
            checks[name] = condition
            checks[f"{name}_value"] = value
            if not condition:
                reasons.append(reason)

        require(
            "spot_only",
            candidate.market_type is MarketType.SPOT and candidate.leverage == 0,
            "only spot with leverage=0 is accepted",
            f"{candidate.market_type.value}/leverage={candidate.leverage}",
        )
        require(
            "execution_disabled",
            candidate.execution_enabled is False,
            "live-order flag must be off",
            str(candidate.execution_enabled),
        )
        require(
            "buy_only_open",
            candidate.side is OrderSide.BUY,
            "a spot-only shadow open does not accept short/sell candidates",
            candidate.side.value,
        )
        require(
            "quality",
            state.quality_decision.allows_downstream,
            f"data quality result {state.quality_decision.value}",
            state.quality_decision.value,
        )
        require(
            "exchange_health",
            state.exchange_rest_healthy and state.exchange_websocket_healthy,
            "exchange REST/WebSocket health is not acceptable",
            f"rest={state.exchange_rest_healthy},ws={state.exchange_websocket_healthy}",
        )
        require(
            "freshness",
            state.data_age_s is not None and state.data_age_s <= self.policy.max_data_age_s,
            "market data missing or stale",
            state.data_age_s,
        )
        require(
            "liquidity",
            state.quote_volume_24h is not None
            and state.quote_volume_24h >= Decimal(str(self.policy.min_quote_volume_24h)),
            "24h quote volume below minimum liquidity or unknown",
            float(state.quote_volume_24h) if state.quote_volume_24h is not None else None,
        )
        require(
            "spread",
            state.spread_bps is not None and state.spread_bps <= self.policy.max_spread_bps,
            "spread limit exceeded or unknown",
            state.spread_bps,
        )
        require(
            "slippage",
            state.estimated_slippage_bps is not None
            and state.estimated_slippage_bps <= self.policy.max_slippage_bps,
            "slippage estimate limit exceeded or unknown",
            state.estimated_slippage_bps,
        )
        require(
            "position_count",
            state.concurrent_positions < self.policy.max_concurrent_positions,
            "maximum concurrent positions limit",
            float(state.concurrent_positions),
        )

        portfolio_risk_pct = (
            float(state.proposed_risk_amount / state.portfolio_value * Decimal("100"))
            if state.portfolio_value > 0 and state.proposed_risk_amount is not None
            else float("inf")
        )
        require(
            "portfolio_risk",
            portfolio_risk_pct <= self.policy.max_portfolio_risk_pct,
            "per-trade portfolio risk limit exceeded",
            portfolio_risk_pct,
        )
        daily_loss_pct = (
            abs(float(state.daily_pnl / state.portfolio_value * Decimal("100")))
            if state.portfolio_value > 0 and state.daily_pnl < 0
            else 0.0
        )
        weekly_loss_pct = (
            abs(float(state.weekly_pnl / state.portfolio_value * Decimal("100")))
            if state.portfolio_value > 0 and state.weekly_pnl < 0
            else 0.0
        )
        require(
            "daily_loss",
            daily_loss_pct < self.policy.daily_loss_limit_pct,
            "daily loss limit reached",
            daily_loss_pct,
        )
        require(
            "weekly_loss",
            weekly_loss_pct < self.policy.weekly_loss_limit_pct,
            "weekly loss limit reached",
            weekly_loss_pct,
        )
        require(
            "drawdown",
            state.drawdown_pct < self.policy.max_drawdown_pct,
            "maximum drawdown limit reached",
            state.drawdown_pct,
        )
        correlation_ok = not (
            state.btc_eth_correlation is not None
            and abs(state.btc_eth_correlation) > self.policy.max_btc_eth_correlation
            and state.correlated_exposure_pct > self.policy.max_correlated_exposure_pct
        )
        require(
            "correlation",
            correlation_ok,
            "BTC/ETH correlated exposure limit exceeded",
            state.btc_eth_correlation,
        )
        require(
            "event_blackout",
            not state.event_blackout,
            "news/event blackout active",
            str(state.event_blackout),
        )
        require(
            "volatility",
            state.normalized_atr_pct is not None
            and state.normalized_atr_pct <= self.policy.max_normalized_atr_pct,
            "volatility excessive or unknown",
            state.normalized_atr_pct,
        )
        require(
            "cooldown",
            not state.cooldown_active,
            "candidate is in cooldown",
            str(state.cooldown_active),
        )

        if reasons:
            decision = RiskDecision.REJECT
        elif self.policy.human_approval_required and not state.human_approved:
            decision = RiskDecision.REQUIRE_HUMAN_REVIEW
            reasons.append("human approval required before a shadow trade")
        else:
            decision = RiskDecision.ALLOW_SHADOW
            reasons.append("shadow simulation only; live orders disabled")

        return RiskAssessment(
            candidate_id=candidate.candidate_id,
            decision=decision,
            reasons=tuple(reasons),
            checks=checks,
            human_review_required=decision is not RiskDecision.ALLOW_SHADOW,
        )
