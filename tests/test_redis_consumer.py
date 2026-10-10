"""RedisEventBus push-consumer tests — fakeredis (real Streams) + fake client (no network)."""

from __future__ import annotations

from typing import Any

import fakeredis

from argus.core.events import Event, EventType
from argus.platform.event_bus import RedisEventBus


def _bus() -> RedisEventBus:
    return RedisEventBus("redis://x", client=fakeredis.FakeStrictRedis(decode_responses=True))


# --- real Streams roundtrip via fakeredis ---


def test_publish_consume_roundtrip() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(EventType.DISCLOSURE_KAP, seen.append, from_beginning=True)

    bus.publish(
        Event(type=EventType.DISCLOSURE_KAP, source="kap", payload={"symbol": "THYAO", "n": 1})
    )
    n = bus.consume_once(block_ms=10)
    assert n == 1
    assert seen[0].type is EventType.DISCLOSURE_KAP
    assert seen[0].source == "kap"  # source roundtrip
    assert seen[0].payload == {"symbol": "THYAO", "n": 1}  # payload roundtrip


def test_only_subscribed_types_dispatched() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(EventType.DISCLOSURE_KAP, seen.append, from_beginning=True)

    bus.publish(Event(type=EventType.DISCLOSURE_KAP, source="kap"))
    bus.publish(Event(type=EventType.PRICE_UPDATED, source="loop"))  # not subscribed
    assert bus.consume_once(block_ms=10) == 1  # disclosure only


def test_checkpoint_is_idempotent() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(EventType.NEWS_RECEIVED, seen.append, from_beginning=True)

    bus.publish(Event(type=EventType.NEWS_RECEIVED, source="cp"))
    assert bus.consume_once(block_ms=10) == 1
    assert bus.consume_once(block_ms=10) == 0  # checkpoint advanced → not reprocessed
    bus.publish(Event(type=EventType.NEWS_RECEIVED, source="cp"))
    assert bus.consume_once(block_ms=10) == 1  # the new event is processed


def test_multiple_handlers_same_type() -> None:
    bus = _bus()
    a: list[Event] = []
    b: list[Event] = []
    bus.subscribe(EventType.ALERT_CREATED, a.append, from_beginning=True)
    bus.subscribe(EventType.ALERT_CREATED, b.append, from_beginning=True)
    bus.publish(Event(type=EventType.ALERT_CREATED, source="x"))
    bus.consume_once(block_ms=10)
    assert len(a) == 1 and len(b) == 1


def test_failing_handler_does_not_break_consumer() -> None:
    bus = _bus()
    good: list[Event] = []

    def boom(_: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(EventType.TRIGGER_FIRED, boom, from_beginning=True)
    bus.subscribe(EventType.TRIGGER_FIRED, good.append, from_beginning=True)
    bus.publish(Event(type=EventType.TRIGGER_FIRED, source="x"))
    bus.consume_once(block_ms=10)
    assert len(good) == 1  # the healthy handler still ran (fail-safe)


def test_no_handlers_consumes_nothing() -> None:
    assert _bus().consume_once(block_ms=10) == 0


# --- unit tests with a fake client (no Redis server) ---


def test_decode_skips_malformed_entry() -> None:
    class FakeClient:
        def xread(self, streams: Any, count: int, block: int) -> Any:
            return [("argus.news.received", [("1-0", {"type": "INVALID", "id": "x"})])]

    bus = RedisEventBus("redis://x", client=FakeClient())
    seen: list[Event] = []
    bus.subscribe(EventType.NEWS_RECEIVED, seen.append)
    # Malformed 'type' → decode returns None → skipped without crashing; checkpoint advances.
    assert bus.consume_once(block_ms=10) == 0
    assert seen == []


def test_publish_uses_xadd() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def xadd(self, key: str, fields: dict[str, str], **kw: Any) -> str:
            calls.append((key, fields))
            return "1-0"

    bus = RedisEventBus("redis://x", client=FakeClient())
    bus.publish(Event(type=EventType.PRICE_UPDATED, source="loop", payload={"p": 1}))
    assert calls[0][0] == "argus.price.updated"  # correct stream key
    assert calls[0][1]["source"] == "loop"
    assert '"p": 1' in calls[0][1]["data"]
