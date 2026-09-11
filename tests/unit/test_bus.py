"""Event bus tests: a slow consumer must never stall ingest."""

from __future__ import annotations

import asyncio

from deepflow.core.bus import EventBus


async def test_publish_reaches_every_subscriber() -> None:
    bus: EventBus[int] = EventBus(maxsize=10)
    a, b = bus.subscribe(), bus.subscribe()

    bus.publish(42)

    assert await a.get() == 42
    assert await b.get() == 42


async def test_full_queue_drops_instead_of_blocking() -> None:
    """The critical property. A dashboard tab on a sleeping laptop must not be
    able to apply backpressure to the market-data loop."""
    bus: EventBus[int] = EventBus(maxsize=2)
    queue = bus.subscribe()

    for i in range(10):
        bus.publish(i)

    assert queue.qsize() == 2


async def test_a_slow_subscriber_does_not_starve_a_fast_one() -> None:
    bus: EventBus[int] = EventBus(maxsize=1)
    slow = bus.subscribe()
    fast = bus.subscribe()

    bus.publish(1)
    assert await fast.get() == 1

    bus.publish(2)  # `slow` is full and drops; `fast` must still receive.
    assert await asyncio.wait_for(fast.get(), timeout=0.5) == 2
    assert slow.qsize() == 1


async def test_unsubscribe_stops_delivery() -> None:
    bus: EventBus[int] = EventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)

    bus.publish(1)

    assert queue.empty()
    assert bus.subscriber_count == 0


async def test_publish_with_no_subscribers_is_a_noop() -> None:
    bus: EventBus[int] = EventBus()
    bus.publish(1)
    assert bus.subscriber_count == 0
