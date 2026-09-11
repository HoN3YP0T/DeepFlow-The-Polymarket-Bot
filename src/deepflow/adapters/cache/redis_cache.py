"""Redis helpers.

Used for three things, all of which want a shared store rather than process
memory:

* hot market state, so the API can read without touching Postgres
* distributed locks around order submission, so two workers cannot act on the
  same signal
* circuit-breaker flags, so a halt applies to every process at once
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from deepflow.config.settings import Settings
from deepflow.core.logging import get_logger

log = get_logger(__name__)


class RedisCache:
    """Thin wrapper over redis.asyncio."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: object | None = None

    async def connect(self) -> None:
        raise NotImplementedError("RedisCache.connect")

    async def close(self) -> None:
        raise NotImplementedError("RedisCache.close")

    def lock(
        self, key: str, *, lease_seconds: float = 10.0
    ) -> AbstractAsyncContextManager[None]:
        """Distributed lock.

        ``lease_seconds`` is the lock's own expiry, not a wait timeout: if the
        holder dies mid-submission the lease must lapse on its own, or a crash
        during an order would block that market permanently.

        Held around the submit path so a duplicate signal in another worker
        cannot race the database's unique constraint into a rejected write.
        """
        raise NotImplementedError("RedisCache.lock")
