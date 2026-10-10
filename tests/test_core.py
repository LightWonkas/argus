"""Core schema/enum/event tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from argus.core import (
    DATA_PENDING,
    AssetClass,
    Event,
    EventType,
    EvidenceTier,
    FreshnessLabel,
    Provenance,
    utcnow,
)


def test_data_pending_sentinel() -> None:
    assert DATA_PENDING == "DATA PENDING"


def test_asset_class_priority_order() -> None:
    # BIST > metal > crypto > (fx/equity)
    assert AssetClass.BIST.priority < AssetClass.METAL.priority
    assert AssetClass.METAL.priority < AssetClass.CRYPTO.priority
    assert AssetClass.CRYPTO.priority < AssetClass.FX.priority


def test_evidence_tier_values() -> None:
    assert [t.value for t in EvidenceTier] == ["A", "B", "C"]


def test_provenance_requires_tz_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        Provenance(source="x", timestamp=datetime(2026, 1, 1), confidence=0.9)


def test_provenance_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Provenance(source="x", timestamp=utcnow(), confidence=1.5)


def test_provenance_is_frozen() -> None:
    p = Provenance(source="x", timestamp=utcnow(), confidence=0.5)
    with pytest.raises(ValidationError):
        p.confidence = 0.9  # type: ignore[misc]


def test_freshness_computation() -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    ts = now - timedelta(seconds=30)
    p = Provenance(source="x", timestamp=ts, confidence=1.0)

    fresh = p.with_computed_freshness(stale_after_s=60, expired_after_s=300, now=now)
    assert fresh.freshness is FreshnessLabel.FRESH

    stale = p.with_computed_freshness(stale_after_s=10, expired_after_s=300, now=now)
    assert stale.freshness is FreshnessLabel.STALE

    expired = p.with_computed_freshness(stale_after_s=5, expired_after_s=10, now=now)
    assert expired.freshness is FreshnessLabel.EXPIRED


def test_event_topic_and_frozen() -> None:
    ev = Event(type=EventType.TRIGGER_FIRED, source="loop")
    assert ev.topic == "argus.trigger.fired"
    assert len(ev.id) == 32  # uuid4 hex
    with pytest.raises(ValidationError):
        ev.source = "other"  # type: ignore[misc]
