"""InMemoryEventBus tests."""

from __future__ import annotations

from argus.core.events import Event, EventType
from argus.platform import InMemoryEventBus


def _ev(t: EventType) -> Event:
    return Event(type=t, source="test")


def test_publish_records() -> None:
    bus = InMemoryEventBus()
    bus.publish(_ev(EventType.PRICE_UPDATED))
    assert len(bus.published) == 1
    assert bus.published[0].type is EventType.PRICE_UPDATED


def test_subscribe_dispatch() -> None:
    bus = InMemoryEventBus()
    seen: list[str] = []
    bus.subscribe(EventType.TRIGGER_FIRED, lambda e: seen.append(e.id))
    bus.publish(_ev(EventType.TRIGGER_FIRED))
    bus.publish(_ev(EventType.PRICE_UPDATED))  # different type → not dispatched
    assert len(seen) == 1


def test_failing_handler_does_not_break_others() -> None:
    bus = InMemoryEventBus()
    calls: list[str] = []

    def boom(_e: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(EventType.ALERT_CREATED, boom)
    bus.subscribe(EventType.ALERT_CREATED, lambda e: calls.append("ok"))
    bus.publish(_ev(EventType.ALERT_CREATED))  # must not raise
    assert calls == ["ok"]


def test_observability_history_is_bounded() -> None:
    bus = InMemoryEventBus(max_history=2)
    events = [_ev(EventType.PRICE_UPDATED) for _ in range(3)]
    for event in events:
        bus.publish(event)

    assert list(bus.published) == events[-2:]
