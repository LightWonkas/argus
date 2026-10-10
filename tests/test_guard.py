"""Deterministic GUARD risk engine tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from argus.config.models import RiskConfig
from argus.core.crypto import TradeCandidate
from argus.core.enums import OrderSide, RiskDecision, Timeframe
from argus.decision.guard import GuardRiskEngine, GuardState
from argus.platform.quality import QualityDecision

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _candidate(*, side: OrderSide = OrderSide.BUY) -> TradeCandidate:
    return TradeCandidate(
        candidate_id="candidate-1",
        symbol="BTCUSDT",
        side=side,
        created_at=NOW,
        horizon=Timeframe.H1,
        entry_reference="100",
        stop_price="96",
        invalidation="1h close below 96",
        evidence_source_ids=("technical:ohlcv", "flow:trades"),
        confidence_pct=70,
    )


def _state(*, human_approved: bool = False, **updates: object) -> GuardState:
    data: dict[str, object] = {
        "portfolio_value": Decimal("10000"),
        "proposed_risk_amount": Decimal("40"),
        "quote_volume_24h": Decimal("100000000"),
        "spread_bps": 2.0,
        "estimated_slippage_bps": 3.0,
        "data_age_s": 1.0,
        "exchange_rest_healthy": True,
        "exchange_websocket_healthy": True,
        "normalized_atr_pct": 2.0,
        "quality_decision": QualityDecision.ACCEPT,
        "human_approved": human_approved,
    }
    data.update(updates)
    return GuardState(**data)  # type: ignore[arg-type]


def test_guard_requires_human_then_allows_only_shadow_after_approval() -> None:
    engine = GuardRiskEngine()
    review = engine.assess(_candidate(), _state())
    allowed = engine.assess(_candidate(), _state(human_approved=True))

    assert review.decision is RiskDecision.REQUIRE_HUMAN_REVIEW
    assert review.human_review_required
    assert allowed.decision is RiskDecision.ALLOW_SHADOW
    assert not allowed.human_review_required
    assert allowed.reasons[0] == "shadow simulation only; live orders disabled"


def test_guard_reports_every_failed_control() -> None:
    assessment = GuardRiskEngine().assess(
        _candidate(side=OrderSide.SELL),
        _state(
            proposed_risk_amount=None,
            quote_volume_24h=None,
            spread_bps=None,
            estimated_slippage_bps=99.0,
            data_age_s=99.0,
            exchange_rest_healthy=False,
            exchange_websocket_healthy=False,
            concurrent_positions=1,
            daily_pnl=Decimal("-300"),
            weekly_pnl=Decimal("-600"),
            drawdown_pct=11,
            btc_eth_correlation=0.95,
            correlated_exposure_pct=30,
            event_blackout=True,
            normalized_atr_pct=9,
            cooldown_active=True,
            quality_decision=QualityDecision.QUARANTINE,
        ),
    )
    assert assessment.decision is RiskDecision.REJECT
    assert len(assessment.reasons) >= 12
    assert assessment.checks["quality"] is False
    assert assessment.checks["exchange_health"] is False


def test_kill_switch_halts_before_all_other_checks() -> None:
    engine = GuardRiskEngine(RiskConfig(kill_switch=True))
    assessment = engine.assess(_candidate(), _state(human_approved=True))
    assert assessment.decision is RiskDecision.HALT_SYSTEM
    assert assessment.reasons == ("kill switch active",)
