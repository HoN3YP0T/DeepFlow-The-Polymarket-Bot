"""Minimal in-process async event bus.

Decouples producers (stream adapters) from consumers (feature engine, exit
manager, dashboard broadcaster) without pulling in a broker for V1. Bounded
queues with an explicit drop policy: under a burst, a slow consumer must not
grow memory without limit or stall the ingest loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Generic, TypeVar

from deepflow.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


@dataclass(slots=True)
class SubscriberStats:
    delivered: int = 0
    dropped: int = 0


class EventBus(Generic[T]):
    """Fan-out publish/subscribe over asyncio queues."""

    def __init__(self, *, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._queues: dict[asyncio.Queue[T], SubscriberStats] = {}

    def subscribe(self) -> asyncio.Queue[T]:
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self._maxsize)
        self._queues[queue] = SubscriberStats()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[T]) -> None:
        self._queues.pop(queue, None)

    def publish(self, event: T) -> None:
        """Non-blocking fan-out.

        Drops for a subscriber whose queue is full and records the drop, rather
        than applying backpressure to the market-data loop. A lagging dashboard
        consumer must never be able to stall ingest.
        """
        for queue, stats in self._queues.items():
            try:
                queue.put_nowait(event)
                stats.delivered += 1
            except asyncio.QueueFull:
                stats.dropped += 1
                if stats.dropped % 100 == 1:
                    log.warning("event_bus.dropped", dropped=stats.dropped)

    async def stream(self) -> AsyncIterator[T]:
        """Consume as an async iterator, cleaning up the subscription on exit."""
        queue = self.subscribe()
        try:
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)
