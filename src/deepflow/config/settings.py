"""Process settings, loaded from environment / .env.

Credentials only ever arrive via the environment. Nothing here carries a
usable default for a secret, and ``SecretStr`` keeps values out of reprs and
tracebacks.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from deepflow.config.thresholds import Thresholds
from deepflow.core.enums import RunMode
from deepflow.core.errors import LiveModeNotConfirmedError

#: Typed in by a human to enable LIVE mode. Deliberately awkward.
LIVE_ACK_PHRASE = "I ACCEPT REAL CAPITAL RISK"


class PolymarketSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Literal["prod", "staging"] = "prod"
    private_key: SecretStr | None = None
    """Order signer. Sufficient to sign and place orders; not sufficient for
    gasless relayer operations."""

    wallet_address: str | None = None
    """Account wallet that holds collateral and positions.

    Required rather than derived: for a Deposit, Safe or Proxy wallet the signer
    is not the wallet, and the signature type baked into every order depends on
    which of the four it is. Given only a key, the SDK would sign as an EOA.
    """

    wallet_type: Literal["deposit", "proxy", "safe", "eoa"] = "deposit"
    """Deposit Wallet is the default for accounts created on or after
    2026-05-04; Proxy and Safe are legacy. Selects the order ``signature_type``
    (3 / 1 / 2 / 0)."""

    relayer_api_key: SecretStr | None = None
    relayer_api_key_address: str | None = None
    """Needed for gasless approvals, redemptions, splits and merges. Without one,
    ``ensure_allowances`` and ``redeem_positions`` cannot run even though order
    placement works -- which surfaces as every order being rejected for
    allowance on a fresh wallet."""

    funder_address: str | None = None
    """Deprecated alias for ``wallet_address``, kept so existing .env files keep
    working."""
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    ws_ping_interval_seconds: float = Field(default=20.0, gt=0)
    ws_reconnect_base_delay_seconds: float = Field(default=1.0, gt=0)
    ws_reconnect_max_delay_seconds: float = Field(default=60.0, gt=0)

    # Section 2 of the brief excludes Gamma from the data plane. The official
    # SDK nevertheless serves market discovery metadata from Gamma internally
    # (see docs/ADR-0002). This flag records the policy explicitly so the
    # decision is visible rather than buried in an adapter.
    allow_gamma_backed_discovery: bool = False

    @property
    def account_wallet(self) -> str | None:
        return self.wallet_address or self.funder_address

    @property
    def is_authenticated(self) -> bool:
        return self.private_key is not None and self.account_wallet is not None

    @property
    def can_submit_relayer_transactions(self) -> bool:
        """Whether approvals and redemptions are reachable.

        Checked separately from :attr:`is_authenticated` because the failure is
        separate: a key-only LIVE run places orders right up until the first one
        needs an allowance that was never granted.
        """
        return self.is_authenticated and self.relayer_api_key is not None


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn: SecretStr = SecretStr("postgresql+asyncpg://deepflow:deepflow@localhost:5432/deepflow")
    pool_size: int = Field(default=10, gt=0)
    max_overflow: int = Field(default=5, ge=0)
    echo: bool = False


class RedisSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn: SecretStr = SecretStr("redis://localhost:6379/0")
    enabled: bool = True


class ApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, le=65535)
    jwt_secret: SecretStr | None = None
    jwt_ttl_seconds: int = Field(default=3600, gt=0)
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)


class Settings(BaseSettings):
    """Root settings object.

    Nested values use a double-underscore delimiter, e.g.
    ``DEEPFLOW_POLYMARKET__PRIVATE_KEY``.
    """

    model_config = SettingsConfigDict(
        env_prefix="DEEPFLOW_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    mode: RunMode = RunMode.PAPER
    live_trading_confirmed: bool = False
    live_trading_ack: str = ""

    discovery_interval_seconds: float = Field(default=300.0, gt=0)
    """How often the discovery sweep re-reads the market catalogue.

    Market *metadata* changes slowly -- a new market is listed, an old one closes --
    so this is deliberately far slower than the price feed. Polling it quickly
    would burn rate limit re-reading fields that did not move, and prices come
    from the stream regardless."""

    live_game_interval_seconds: float = Field(default=20.0, gt=0)
    """How often the in-play fixture sweep runs.

    Faster than discovery and slower than the book feed, because that is the rate
    the underlying data moves at: a score or period change is a discrete event a
    few times an hour per fixture, and one request covers every fixture at once.
    Polling it at book speed would burn rate limit re-reading an unchanged score."""

    max_tracked_markets: int = Field(default=100, gt=0)
    """Ceiling on markets subscribed at once.

    The stream subscribes per connection, so the token set is fixed until the next
    sweep reopens it. An unbounded set would mean one reconnect churning thousands
    of subscriptions."""

    persist_snapshots: bool = True
    """Write streamed snapshots to the database.

    On by default: the snapshot history is what a backtest replays later, and it
    can only be collected in real time. Turning it off is for a diagnostic run,
    not for saving disk."""

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    polymarket: PolymarketSettings = PolymarketSettings()
    database: DatabaseSettings = DatabaseSettings()
    redis: RedisSettings = RedisSettings()
    api: ApiSettings = ApiSettings()
    thresholds: Thresholds = Thresholds()

    @model_validator(mode="after")
    def _guard_live_mode(self) -> Settings:
        """LIVE is refused unless three independent things agree.

        Section 25: live execution is disabled by default. A single stray
        environment variable must not be able to arm real capital.
        """
        if self.mode is not RunMode.LIVE:
            return self

        if not self.live_trading_confirmed:
            raise LiveModeNotConfirmedError(
                "LIVE mode requires DEEPFLOW_LIVE_TRADING_CONFIRMED=true"
            )
        if self.live_trading_ack.strip() != LIVE_ACK_PHRASE:
            raise LiveModeNotConfirmedError(
                f"LIVE mode requires DEEPFLOW_LIVE_TRADING_ACK to be exactly {LIVE_ACK_PHRASE!r}"
            )
        if not self.polymarket.is_authenticated:
            raise LiveModeNotConfirmedError(
                "LIVE mode requires both a Polymarket private key and an account "
                "wallet address (DEEPFLOW_POLYMARKET__WALLET_ADDRESS)"
            )
        if not self.polymarket.can_submit_relayer_transactions:
            raise LiveModeNotConfirmedError(
                "LIVE mode requires DEEPFLOW_POLYMARKET__RELAYER_API_KEY: without it "
                "exchange approvals cannot be granted and resolved positions cannot "
                "be redeemed"
            )
        return self

    @model_validator(mode="after")
    def _guard_api_secret(self) -> Settings:
        """The dashboard exposes kill switches; it is never left unauthenticated
        outside a local paper run."""
        if self.mode in (RunMode.SHADOW, RunMode.LIVE) and self.api.jwt_secret is None:
            raise LiveModeNotConfirmedError(
                "SHADOW and LIVE modes require DEEPFLOW_API__JWT_SECRET to be set"
            )
        return self

    @property
    def sends_real_orders(self) -> bool:
        return self.mode is RunMode.LIVE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
