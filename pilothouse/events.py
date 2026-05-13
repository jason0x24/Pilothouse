"""In-process pub/sub for run events.

The AgentRunner writes every step to the events table (durable, auditable).
That's the source of truth. The bus here is a *non-durable side-channel*:
SSE subscribers attach to a run's queue and receive events as they happen,
and the metrics counter increments on every published event.

Design notes:

- The bus is intentionally bounded per-subscriber (drop-newest on full
  queue). A slow SSE client must not block the runtime.
- Topic = run_id. A "*" topic exists for fan-out subscribers
  (metrics, future websocket dashboards). Publishers always emit to both.
- No durability guarantees — if no one's listening when an event is
  published, it's gone from the bus (but still in the DB).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 256


@dataclass
class RunEvent:
    run_id: str
    kind: str
    data: dict
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "data": self.data,
            "created_at": self.created_at.isoformat(),
        }


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = {}
        # Process-wide running counters used by /metrics.
        self.event_counts: Counter[str] = Counter()
        self.tool_invocations: Counter[str] = Counter()
        self.run_status_counts: Counter[str] = Counter()
        self.approvals_resolved: Counter[str] = Counter()  # approve|reject|expired

    def publish(self, event: RunEvent) -> None:
        self.event_counts[event.kind] += 1
        if event.kind == "tool_call":
            tool = event.data.get("tool", "?")
            self.tool_invocations[tool] += 1
        elif event.kind == "approval_resolved":
            decision = event.data.get("decision", "?")
            self.approvals_resolved[decision] += 1
        elif event.kind == "approval_expired":
            self.approvals_resolved["expired"] += 1

        for topic in (event.run_id, "*"):
            for q in list(self._subscribers.get(topic, ())):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning(
                        "subscriber queue full for topic=%s; dropping event %s",
                        topic,
                        event.kind,
                    )

    def record_status(self, status: str) -> None:
        self.run_status_counts[status] += 1

    def subscribe_queue(self, topic: str) -> asyncio.Queue[RunEvent]:
        """Synchronous registration — returns a queue that immediately
        starts receiving events. Caller is responsible for unsubscribing
        by calling `unsubscribe(topic, q)`. Use this when you need
        guaranteed delivery of events emitted *right after* the call.
        """
        q: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.setdefault(topic, set()).add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue[RunEvent]) -> None:
        subs = self._subscribers.get(topic)
        if subs is not None:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(topic, None)

    async def subscribe(self, topic: str) -> AsyncIterator[RunEvent]:
        """Async-iterator convenience over subscribe_queue.

        Note: this has a small race — the subscription is not active
        until the consumer does `async for`. For "subscribe before any
        emit" semantics use `subscribe_queue` directly.
        """
        q = self.subscribe_queue(topic)
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(topic, q)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus() -> None:
    """Test helper — start each test from a clean bus."""
    global _bus
    _bus = EventBus()
