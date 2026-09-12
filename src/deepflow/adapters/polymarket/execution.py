"""Order execution adapter.

Implements :class:`~deepflow.ports.execution.ExecutionPort` and
:class:`~deepflow.ports.execution.AccountPort`.

Confirmed on ``AsyncSecureClient`` (0.10.0): ``create_limit_order``,
``create_market_order``, ``place_limit_order``, ``place_market_order``,
``post_order(s)``, ``cancel_order(s)``, ``cancel_all``,
``cancel_market_orders``, ``list_open_orders``, ``get_order``,
``get_balance_allowance``, ``get_closed_only_mode``, ``list_positions``,
``wait_for_order_fill_settlement``.

The mode guard is enforced here, at the last hop before the network. Placing it
at the boundary means no upstream bug -- a mis-set flag, a stale config object,
a test harness -- can produce a live order from a PAPER or SHADOW run.

Venue semantics this adapter is responsible for translating, none of which are
optional:

* **Market BUYs are denominated in collateral, not shares.**
  ``place_market_order(amount=...)`` spends ``amount`` of pUSD; ``shares=...`` is
  the SELL-side form. Passing a share count as ``amount`` sizes the trade by a
  factor of ``1 / price`` -- at 0.95 that is a 5% error, and at 0.05 a twentyfold
  one. ``max_spend`` additionally caps the *all-in* cost, because taker fees are
  charged on top of ``amount``.
* **A response status of ``delayed`` is not a fill and not a rejection.** On a
  market with ``seconds_delay`` set, an accepted order returns zero filled
  amounts and no trade ids, and matches later. It must map to
  ``OrderStatus.DELAYED`` and be followed on the user stream.
* **GTD cannot express a short-lived order.** Minimum expiry is 3 minutes out
  and the venue expires an order a minute early, so working orders under ~2
  minutes are GTC plus a client-side cancel. See ``venue.gtd_expiration``.
* **Prices and sizes must be pre-rounded** onto the market's tick grid and size
  precision, per ``venue.round_price_to_tick`` / ``round_shares``. A sub-tick
  price is a hard rejection, and the tick size can change mid-session via the
  ``tick_size_change`` stream event.
* **Restricted modes are waits, not failures.** HTTP 425 means the matching
  engine is restarting; for 2 minutes after it returns, only cancels and
  ``post_only`` orders are accepted (HTTP 503, code ``post_only_mode``, with
  ``retry_after_seconds``). These raise the ``VenueModeError`` subclasses so the
  supervisor waits them out instead of tripping the API_FAILURE breaker.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent, OrderRecord, Position
from deepflow.core.enums import RunMode
from deepflow.core.logging import get_logger
from deepflow.core.types import ClientOrderKey, OrderId

log = get_logger(__name__)


class PolymarketExecution:
    """Live order placement, gated by run mode."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def _assert_live(self, action: str) -> None:
        """Final interlock. Nothing reaches the venue outside LIVE mode."""
        if self._settings.mode is not RunMode.LIVE:
            raise RuntimeError(f"refusing to {action}: run mode is {self._settings.mode}, not LIVE")

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """Sign and post an order.

        TODO(skeleton): build via ``create_limit_order`` / ``create_market_order``
        then ``post_order``.

        Failure semantics that callers depend on:
        * definitive refusal (400/401/404) -> ``OrderRejectedError``
        * 425                              -> ``MatchingEngineRestartingError``
        * 503 post-only / cancel-only      -> ``PostOnlyModeRequiredError`` /
          ``CancelOnlyModeError``
        * 429                              -> ``RateLimitedError``
        * timeout / dropped response / 5xx -> ``ExecutionUncertainError``

        The last case must never be retried here. It goes to reconciliation,
        which uses ``find_by_client_key`` to establish whether the order landed.
        The venue documents ``500 order timed out`` as provably not-executed, but
        recognising it means matching an error string, and being wrong about it
        costs a duplicate position -- so it stays in the uncertain bucket.
        """
        self._assert_live("submit order")
        raise NotImplementedError("PolymarketExecution.submit")

    async def cancel(self, order_id: OrderId) -> OrderRecord:
        self._assert_live("cancel order")
        raise NotImplementedError("PolymarketExecution.cancel")

    async def cancel_all(self) -> Sequence[OrderRecord]:
        """Panic button behind the dashboard's Cancel All control."""
        self._assert_live("cancel all orders")
        raise NotImplementedError("PolymarketExecution.cancel_all")

    async def get_order(self, order_id: OrderId) -> OrderRecord | None:
        raise NotImplementedError("PolymarketExecution.get_order")

    async def find_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None:
        """Resolve an uncertain submission.

        TODO(skeleton): scan ``list_open_orders`` and recent ``list_trades`` for
        the intent's key. The signed order carries a ``metadata`` bytes32 field
        and the CLOB does not index or echo it back, so there is no server-side
        client order id to query by: the key is reconstructed from (token id,
        side, price, size, time window) -- see
        ``deepflow.execution.idempotency``. That reconstruction is why the
        idempotency window matters, and why a scale-in at the same price inside
        one window must pass an explicit salt.
        """
        raise NotImplementedError("PolymarketExecution.find_by_client_key")

    async def get_closed_only_mode(self) -> bool:
        """Whether the account may only place position-reducing orders.

        An account-level venue circuit breaker. Checked before an entry so the
        refusal is legible once, rather than arriving as a rejection on every new
        position while exits continue to work normally.
        """
        raise NotImplementedError("PolymarketExecution.get_closed_only_mode")

    # --- Dead-man's switch ------------------------------------------------
    async def start_order_heartbeat(self) -> None:
        """Arm the venue-side heartbeat that cancels our resting orders.

        The venue will cancel every open order owned by these CLOB credentials
        if a valid heartbeat does not arrive within 10 seconds (swept every 5s,
        so cancellation can lag by up to 5s more). Once the first heartbeat is
        accepted the venue *expects* the stream to continue.

        This is the one safety mechanism that survives the process dying. Every
        circuit breaker in this codebase assumes a running supervisor; a crashed
        or partitioned bot leaves resting orders exposed with nothing watching
        them, and this is what closes that hole. Each response returns the next
        ``heartbeat_id`` to send.
        """
        raise NotImplementedError("PolymarketExecution.start_order_heartbeat")

    async def list_open_orders(self) -> Sequence[OrderRecord]:
        raise NotImplementedError("PolymarketExecution.list_open_orders")

    # --- AccountPort ------------------------------------------------------
    async def get_collateral_balance(self) -> Decimal:
        raise NotImplementedError("PolymarketExecution.get_collateral_balance")

    async def list_positions(self) -> Sequence[Position]:
        raise NotImplementedError("PolymarketExecution.list_positions")
