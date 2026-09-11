"""SDK session lifecycle.

One place that owns constructing, sharing and closing the SDK clients, so
connection pools are not duplicated per adapter and shutdown is deterministic.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from deepflow.config.settings import Settings
from deepflow.core.errors import ConfigurationError
from deepflow.core.logging import get_logger

log = get_logger(__name__)


class PolymarketSession:
    """Owns the SDK client objects for the process.

    Public reads work with no credentials. The secure client is only built when
    a signing key is configured -- PAPER and BACKTEST runs never need one, and
    a run that cannot sign is a run that cannot accidentally trade.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._public: Any | None = None
        self._secure: Any | None = None

    async def start(self) -> None:
        """Construct clients.

        TODO(skeleton): build ``AsyncPublicClient`` and, when credentials are
        present, ``AsyncSecureClient.create(...)``. Left unwired so that
        importing this module never opens a socket; the runner calls
        ``start()`` explicitly.
        """
        raise NotImplementedError("PolymarketSession.start")

    async def close(self) -> None:
        """Close both clients, tolerating either being absent."""
        for client in (self._secure, self._public):
            if client is None:
                continue
            try:
                await client.close()
            except Exception:
                log.warning("polymarket.close_failed", exc_info=True)
        self._public = None
        self._secure = None

    @property
    def public(self) -> Any:
        if self._public is None:
            raise ConfigurationError("Polymarket session not started")
        return self._public

    @property
    def secure(self) -> Any:
        """Authenticated client.

        Raises rather than returning ``None`` so an unauthenticated process
        fails loudly at the call site instead of silently skipping execution.
        """
        if self._secure is None:
            raise ConfigurationError(
                "Authenticated Polymarket client unavailable: no private key configured"
            )
        return self._secure

    @property
    def has_credentials(self) -> bool:
        return self._secure is not None

    async def __aenter__(self) -> PolymarketSession:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
