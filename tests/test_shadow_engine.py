"""Shadow fill, cost, PnL, stop/timeout and restart tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from argus.config.models import ShadowConfig
from argus.core.crypto import RiskAssessment, TradeCandidate
from argus.core.enums import (
    OrderSide,
    RiskDecision,
    ShadowOrderStatus,
    ShadowPositionStatus,
    Timeframe,
)
from argus.core.errors import RiskRejectedError
from argus.execution.shadow import ShadowEngine
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _candidate(
    *, side: OrderSide = OrderSide.BUY, expires_at: datetime | None = None
) -> TradeCandidate:
    return TradeCandidate(
        candidate_id="c1",
        symbol="BTCUSDT",
        side=side,
        created_at=NOW,
        horizon=Timeframe.H1,
        entry_reference="100",
        stop_price="95",
        expires_at=expires_at,
        invalidation="price below 95 or structure breaks",
        evidence_source_ids=("technical:1", "orderflow:1"),
        confidence_pct=70,
    )


def _assessment(decision: RiskDecision = RiskDecision.ALLOW_SHADOW) -> RiskAssessment:
    return RiskAssessment(
        candidate_id="c1",
        assessed_at=NOW,
        decision=decision,
        reasons=("test",),
        human_review_required=decision is not RiskDecision.ALLOW_SHADOW,
    )


def _engine(store: SqliteStore) -> ShadowEngine:
    return ShadowEngine(
        store,
        ShadowConfig(enabled=True, commission_bps=10, slippage_bps=5, partial_fill_ratio=0.5),
    )


def test_shadow_partial_fill_pnl_funding_close_and_restart(tmp_path: object) -> None:
    path = str(tmp_path) + "/shadow.db"
    with SqliteStore(path) as store:
        engine = _engine(store)
        execution = engine.submit(
            _candidate(),
            _assessment(),
            quantity=Decimal("2"),
            bid=Decimal("99.9"),
            ask=Decimal("100"),
            available_quantity=Decimal("2"),
            now=NOW,
        )
        assert execution.order.status is ShadowOrderStatus.PARTIALLY_FILLED
        assert execution.fill.quantity == Decimal("1.0")
        assert execution.fill.price == Decimal("100.0500")
        assert execution.position.fees == Decimal("0.1000500")

    with SqliteStore(path) as reopened:
        engine = _engine(reopened)
        assert len(engine.open_positions) == 1
        position_id = engine.open_positions[0].position_id
        funded = engine.apply_funding(position_id, Decimal("1"))
        assert funded.funding_cost == Decimal("1")

        marked = engine.mark_to_market(
            "BTCUSDT", bid=Decimal("110"), ask=Decimal("110.1"), now=NOW + timedelta(minutes=1)
        )[0]
        assert marked.status is ShadowPositionStatus.OPEN
        assert marked.unrealized_pnl == Decimal("9.95000")

        closed = engine.mark_to_market(
            "BTCUSDT",
            bid=Decimal("110"),
            ask=Decimal("110.1"),
            now=NOW + timedelta(minutes=2),
            invalidation_triggered=True,
        )[0]
        assert closed.status is ShadowPositionStatus.CLOSED
        assert closed.realized_pnl < Decimal(
            "9"
        )  # net of entry+exit commission, slippage and funding
        assert not engine.open_positions
        assert len(reopened.shadow_positions()) == 1
        assert reopened.shadow_positions()[0].status is ShadowPositionStatus.CLOSED


def test_shadow_timeout_closes_position(tmp_path: object) -> None:
    path = str(tmp_path) + "/timeout.db"
    with SqliteStore(path) as store:
        engine = _engine(store)
        engine.submit(
            _candidate(expires_at=NOW + timedelta(seconds=10)),
            _assessment(),
            quantity=Decimal("1"),
            bid=Decimal("99.9"),
            ask=Decimal("100"),
            now=NOW,
        )
        result = engine.mark_to_market(
            "BTCUSDT",
            bid=Decimal("101"),
            ask=Decimal("101.1"),
            now=NOW + timedelta(seconds=11),
        )
        assert result[0].status is ShadowPositionStatus.CLOSED


def test_shadow_rejects_without_guard_allow_or_when_disabled() -> None:
    with SqliteStore() as store:
        disabled = ShadowEngine(store)
        with pytest.raises(RiskRejectedError, match="disabled by config"):
            disabled.submit(
                _candidate(),
                _assessment(),
                quantity=Decimal("1"),
                bid=Decimal("99"),
                ask=Decimal("100"),
            )

        enabled = _engine(store)
        with pytest.raises(RiskRejectedError, match="GUARD"):
            enabled.submit(
                _candidate(),
                _assessment(RiskDecision.REJECT),
                quantity=Decimal("1"),
                bid=Decimal("99"),
                ask=Decimal("100"),
            )
