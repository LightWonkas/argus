"""Event bus (L2) — event publishing for loose coupling.

In-memory :class:`InMemoryEventBus` (synchronous, testable, dependency-free) and a lazy
:class:`RedisEventBus` (Redis Streams) for production. Services communicate through
events here; one crashing does not take the others down.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from typing import Any

from argus.core.events import Event, EventType
from argus.observability.logging import get_logger

_log = get_logger("argus.platform.event_bus")

EventHandler = Callable[[Event], None]


class EventBus(ABC):
    """Event publish/subscribe contract."""

    @abstractmethod
    def publish(self, event: Event) -> None: ...

    @abstractmethod
    def subscribe(self, event_type: EventType, handler: EventHandler) -> None: ...


class InMemoryEventBus(EventBus):
    """Synchronous, in-process event bus (for tests and single-process deployments).

    A failing subscriber does not take the other subscribers down (fail-safe).
    """

    def __init__(self, *, max_history: int = 10_000) -> None:
        if max_history <= 0:
            raise ValueError("max_history must be positive")
        self._handlers: dict[EventType, list[EventHandler]] = {}
        # The observability history does not grow with the event count in production.
        self.published: deque[Event] = deque(maxlen=max_history)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: Event) -> None:
        self.published.append(event)
        for handler in self._handlers.get(event.type, []):
            try:
                handler(event)
            except Exception as exc:  # one subscriber must not break the whole publish
                _log.warning("event-handler-error", topic=event.topic, error=repr(exc))


def _topic_for(event_type: EventType) -> str:
    """Redis stream key of an event type (same rule as ``Event.topic``)."""
    return f"argus.{event_type.value}"


class RedisEventBus(EventBus):
    """Redis Streams based event bus (production) — publish + **push consumer**.

    ``redis`` is imported lazily; ``client`` can be injected (fakeredis/fake → offline tests).
    The consumer reads the subscribed streams with **XREAD** and keeps the last seen id
    (checkpoint) per stream → idempotent (the same event is never handled twice). A failing
    subscriber does not take the others down (fail-safe). ``decode_responses=True`` → fields
    are str (not bytes).
    """

    def __init__(self, url: str, *, maxlen: int = 10_000, client: Any | None = None) -> None:
        if client is not None:
            self._redis: Any = client
        else:
            import redis  # lazy import

            self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._maxlen = maxlen
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._last_ids: dict[str, str] = {}  # topic → last processed id (checkpoint)

    def publish(self, event: Event) -> None:
        fields = {
            "id": event.id,
            "type": event.type.value,
            "source": event.source,
            "created_at": event.created_at.isoformat(),
            "data": json.dumps(event.payload),
        }
        self._redis.xadd(event.topic, fields, maxlen=self._maxlen, approximate=True)

    def subscribe(
        self, event_type: EventType, handler: EventHandler, *, from_beginning: bool = False
    ) -> None:
        """Attach a handler to an event type. ``from_beginning`` → read from the start of
        the stream (default: new events only, i.e. a real "push")."""
        self._handlers.setdefault(event_type, []).append(handler)
        self._last_ids.setdefault(_topic_for(event_type), "0" if from_beginning else "$")

    def consume_once(self, *, block_ms: int = 1000, count: int = 100) -> int:
        """Read one XREAD batch from the subscribed streams and dispatch it to the handlers.

        Returns the number of handled events. The checkpoint (last id) is advanced → the next
        call only sees new events.
        """
        if not self._handlers:
            return 0
        streams = {_topic_for(et): self._last_ids[_topic_for(et)] for et in self._handlers}
        result = self._redis.xread(streams, count=count, block=block_ms)
        if not result:
            return 0
        dispatched = 0
        for stream_key, entries in result:
            for entry_id, raw in entries:
                self._last_ids[stream_key] = entry_id  # advance the checkpoint (idempotent)
                event = self._decode(raw)
                if event is None:
                    continue
                for handler in self._handlers.get(event.type, []):
                    try:
                        handler(event)
                        dispatched += 1
                    except Exception as exc:  # one subscriber must not break the whole consumer
                        _log.warning("event-handler-error", topic=stream_key, error=repr(exc))
        return dispatched

    def run(self, *, block_ms: int = 5000) -> None:
        """Consumer loop (blocking) — handles push events continuously. CLI ``--consumer``."""
        _log.info("push-consumer-started", topics=sorted(_topic_for(et) for et in self._handlers))
        while True:  # pragma: no cover (endless daemon loop)
            self.consume_once(block_ms=block_ms)

    @staticmethod
    def _decode(fields: dict[str, str]) -> Event | None:
        """Decode a stream entry into an ``Event``; a malformed entry is skipped (None)."""
        try:
            from datetime import datetime

            return Event(
                id=fields["id"],
                type=EventType(fields["type"]),
                source=fields.get("source") or "redis",
                created_at=datetime.fromisoformat(fields["created_at"]),
                payload=json.loads(fields.get("data") or "{}"),
            )
        except (KeyError, ValueError) as exc:
            _log.warning("event-decode-error", error=repr(exc))
            return None
