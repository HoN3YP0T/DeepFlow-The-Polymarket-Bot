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

#: The SDK ships exactly one environment: ``polymarket.environments.PRODUCTION``.
#: There is no staging or testnet target, so ``PolymarketSettings.environment``
#: has only one reachable value and anything else must fail at startup rather
#: than quietly pointing a "staging" run at the live exchange.
_SUPPORTED_ENVIRONMENTS = ("prod",)

log = get_logger(__name__)


class PolymarketSession:
    """Owns the SDK client objects for the process.

    Public reads work with no credentials. The secure client is only built when
    a signing key is configured -- PAPER and BACKTEST runs never need one, and
    a run that cannot sign is a run that cannot accidentally trade.

    Credentials come in two tiers, and conflating them is why "it signs fine but
    approvals fail" happens:

    * ``AsyncSecureClient.create(private_key=...)`` is enough to sign and place
      orders.
    * Gasless relayer operations -- the four trading approvals, redemptions,
      splits and merges -- additionally need the account **wallet address** plus a
      Relayer or Builder API key. A signer-only client cannot submit them.

    The wallet type also decides the signature type baked into every order
    (Deposit Wallet 3, Safe 2, Proxy 1, EOA 0) and, for a Deposit Wallet, that
    the signature is wrapped for ERC-7739. The SDK handles this, which is exactly
    why the wallet address must be configured rather than derived: given only a
    key, the SDK has no way to know the key is a session signer for a smart
    wallet rather than the wallet itself.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._public: Any | None = None
        self._secure: Any | None = None

    async def start(self) -> None:
        """Construct clients.

        Idempotent, so a supervisor restart that calls ``start`` twice does not
        leak a second connection pool.

        The secure client is built only when a signing key *and* an account
        wallet are both configured. Requiring the wallet is not defensive
        paperwork: ``AsyncSecureClient.create`` accepts ``wallet=None`` and then
        signs as an EOA, which is the wrong signature type for the Deposit, Safe
        and Proxy wallets almost every real account uses. The resulting orders
        are rejected on signature with nothing pointing at the cause.
        """
        if self._public is not None:
            return

        from polymarket import AsyncPublicClient, AsyncSecureClient
        from polymarket.environments import PRODUCTION

        configured = self._settings.polymarket.environment
        if configured not in _SUPPORTED_ENVIRONMENTS:
            raise ConfigurationError(
                f"Polymarket environment {configured!r} does not exist: the SDK ships only "
                "production. A run configured for staging would otherwise trade live."
            )

        environment = PRODUCTION
        self._public = AsyncPublicClient(environment)
        log.info(
            "polymarket.public_client_started",
            environment=self._settings.polymarket.environment,
        )

        settings = self._settings.polymarket
        if settings.private_key is None:
            log.info("polymarket.secure_client_skipped", reason="no private key configured")
            return
        if settings.account_wallet is None:
            log.warning(
                "polymarket.secure_client_skipped",
                reason="private key configured without an account wallet address",
            )
            return

        self._secure = await AsyncSecureClient.create(
            private_key=settings.private_key.get_secret_value(),
            wallet=settings.account_wallet,
            environment=environment,
        )
        log.info(
            "polymarket.secure_client_started",
            wallet_type=settings.wallet_type,
            relayer_available=settings.can_submit_relayer_transactions,
        )

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
