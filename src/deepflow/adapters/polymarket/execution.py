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
from typing import Any, Final

from polymarket.models.clob.enums import AssetType

from deepflow.adapters.polymarket import mapping, venue
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent, OrderRecord, Position
from deepflow.core.enums import RunMode
from deepflow.core.errors import (
    ConfigurationError,
    MatchingEngineRestartingError,
    PolymarketApiError,
    RateLimitedError,
)
from deepflow.core.logging import get_logger
from deepflow.core.types import OrderId

log = get_logger(__name__)

#: pUSD has 6 decimals. The venue reports integer units and this is the divisor, kept
#: named so a raw integer is never mistaken for a dollar figure.
COLLATERAL_SCALE: Final = Decimal(10) ** 6


def _to_collateral(raw: object) -> Decimal:
    """Venue integer units -> collateral. Tolerant of an already-decimal value."""
    value = Decimal(str(raw or 0))
    # A fractional value is already scaled; only whole numbers are raw units.
    return value / COLLATERAL_SCALE if value == value.to_integral_value() else value


def _status_of(exc: Exception) -> int | None:
    for attribute in ("status_code", "status", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _is_not_found(exc: Exception) -> bool:
    """Whether a failure means "the venue does not have it" rather than "it broke"."""
    if _status_of(exc) == 404:
        return True
    return "not found" in str(exc).lower()


def _matches_intent(order: object, intent: OrderIntent) -> bool:
    """Whether a venue order is the one this intent asked for.

    A fingerprint match on the fields the client key is itself derived from -- token,
    side, price, size -- because the venue carries no client id to match on. Two
    intents identical in all four *are* the same intent as far as the idempotency key
    is concerned, so a collision here is not a false positive: it is the key doing its
    job.

    Price and size are compared exactly. Both were snapped to the venue's grid before
    submission, so the venue echoes what was sent; a tolerance here would let a
    genuinely different order at an adjacent tick be mistaken for this one, which is
    the direction that suppresses a legitimate retry.
    """
    if str(getattr(order, "asset_id", "")) != str(intent.token_id):
        return False
    if str(getattr(order, "side", "")).upper() != str(intent.side).upper():
        return False
    price = getattr(order, "price", None)
    size = getattr(order, "original_size", None)
    if price is None or size is None:
        return False
    return Decimal(str(price)) == intent.limit_price and Decimal(str(size)) == intent.size_shares


def _position_shares(position: object) -> Decimal:
    for attribute in ("size", "shares", "quantity"):
        value = getattr(position, attribute, None)
        if value is not None:
            return Decimal(str(value))
    return Decimal(0)


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
        which uses ``find_by_intent`` to establish whether the order landed.
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
        """One order as the venue sees it. ``None`` when it does not have it.

        **Not** gated by ``_assert_live``: reading is safe in every mode, and a
        PAPER or SHADOW run that cannot see the venue's view of an order cannot
        reconcile against it. The gate belongs on writes.

        A "not found" response is an absence, not an error. That distinction is what
        :meth:`~deepflow.execution.reconciliation.Reconciler.resolve_uncertain_order`
        turns into permission to re-intend, so collapsing it into an exception would
        make every absence unresolvable.
        """
        client = self._secure("get_order")
        try:
            order = await client.get_order(order_id=str(order_id))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise self._translate(exc, f"get_order {order_id}") from exc
        return mapping.to_order_record(order)

    async def find_by_intent(self, intent: OrderIntent) -> OrderRecord | None:
        """Resolve an uncertain submission by fingerprint, not by client id.

        The CLOB accepts **no client-supplied order id**: ``create_limit_order``
        takes no such parameter, ``OpenOrder`` echoes only the venue's own ``id``,
        and ``client_order_id`` exists solely on the perps API (finding 66). So
        the only thing we can match on is the material our own client key is
        derived from -- token id, side, exact price, exact size -- read back off
        the open-order set. That is why the port takes the intent rather than the
        key, and why a scale-in at the same price inside one idempotency window
        must pass an explicit salt: two such intents are indistinguishable here.
        """
        client = self._secure("find_by_intent")
        try:
            page = await client.list_open_orders(token_id=str(intent.token_id)).first_page()
        except Exception as exc:
            raise self._translate(exc, f"find_by_intent {intent.client_key}") from exc

        for order in page.items:
            if _matches_intent(order, intent):
                return mapping.to_order_record(order, client_key=intent.client_key)

        # Absent from the open set. That is **not** proof the order never existed -- a
        # filled or cancelled order is not "open" -- and the caller must read this as
        # "not currently working", not as "never landed". The reconciler pairs it with
        # a position read for exactly that reason: an order that filled between our
        # request and this lookup shows up as inventory, not as an open order.
        return None

    async def get_closed_only_mode(self) -> bool:
        """Whether the account may only place position-reducing orders.

        An account-level venue circuit breaker. Checked before an entry so the
        refusal is legible once, rather than arriving as a rejection on every new
        position while exits continue to work normally.
        """
        client = self._secure("get_closed_only_mode")
        try:
            return bool(await client.get_closed_only_mode())
        except Exception as exc:
            raise self._translate(exc, "get_closed_only_mode") from exc

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
        """Every order the venue is currently working for this account.

        Reconciliation's view of the venue side, and the direction that finds
        orphans -- an order live at the venue with no local record, which appears in
        no exposure figure and is what a process killed mid-submission leaves behind.
        """
        client = self._secure("list_open_orders")
        try:
            page = await client.list_open_orders().first_page()
            return tuple(mapping.to_order_record(order) for order in page.items)
        except Exception as exc:
            raise self._translate(exc, "list_open_orders") from exc

    # --- AccountPort ------------------------------------------------------
    async def get_collateral_balance(self) -> Decimal:
        """Deployable collateral, as the venue reports it.

        Queried with ``AssetType.COLLATERAL`` and the wallet's own signature type.
        The signature type matters even for a read: the balance is looked up for the
        account the signature type implies, so a wallet configured as ``eoa`` when it
        is a deposit wallet reads a *different account's* balance -- most likely
        zero, which would size every trade to nothing and look like a limit rather
        than a misconfiguration.

        The venue reports pUSD in 6-decimal integer units. Converted here so nothing
        downstream has to know that, and so a raw integer can never be mistaken for
        a dollar figure.
        """
        client = self._secure("get_collateral_balance")
        try:
            result = await client.get_balance_allowance(asset_type=AssetType.COLLATERAL)
        except Exception as exc:
            raise self._translate(exc, "get_collateral_balance") from exc
        return _to_collateral(getattr(result, "balance", 0))

    async def get_allowances(self) -> dict[str, Decimal]:
        """Collateral balance and allowance together, for the approvals check.

        Separate from :meth:`get_collateral_balance` because it answers a different
        question: whether the exchange contracts are approved to move our collateral
        at all. A zero allowance with a healthy balance is the state in which every
        order is rejected for "not enough balance / allowance" while the account
        plainly has funds.
        """
        client = self._secure("get_allowances")
        try:
            result = await client.get_balance_allowance(asset_type=AssetType.COLLATERAL)
        except Exception as exc:
            raise self._translate(exc, "get_allowances") from exc
        return {
            "balance": _to_collateral(getattr(result, "balance", 0)),
            "allowance": _to_collateral(getattr(result, "allowance", 0)),
        }

    async def list_positions(self) -> Sequence[Position]:
        """Open positions as the venue sees them.

        The settlement record, and therefore the winning side of any disagreement
        with the local database during reconciliation.
        """
        client = self._secure("list_positions")
        try:
            page = await client.list_positions().first_page()
            return tuple(
                mapping.to_position(position)
                for position in page.items
                if _position_shares(position) != 0
            )
        except Exception as exc:
            raise self._translate(exc, "list_positions") from exc

    # --- Internals --------------------------------------------------------
    def _secure(self, action: str) -> Any:
        """The authenticated client, or a clear failure explaining what is missing.

        Reads need credentials but not LIVE mode, so this deliberately does not call
        :meth:`_assert_live`. Without credentials the session has no secure client at
        all, and the resulting message names the two settings involved -- a private
        key alone is not enough, because the wallet type decides the signature type
        and given only a key the SDK signs as an EOA.
        """
        client = getattr(self._session, "secure", None)
        if client is None:
            raise ConfigurationError(
                f"cannot {action}: no authenticated client. Set "
                "DEEPFLOW_POLYMARKET__PRIVATE_KEY and "
                "DEEPFLOW_POLYMARKET__WALLET_ADDRESS (plus WALLET_TYPE)."
            )
        return client

    @staticmethod
    def _translate(exc: Exception, action: str) -> Exception:
        """Map a venue failure onto this system's error vocabulary.

        The distinction that matters is not the HTTP code but whether the request is
        *known* not to have executed. Restricted modes are waits rather than
        failures, so they raise their own types and the supervisor sits them out
        instead of tripping the API_FAILURE breaker.
        """
        status = _status_of(exc)
        if status == venue.STATUS_ENGINE_RESTARTING:
            return MatchingEngineRestartingError(f"{action}: matching engine restarting")
        if status == venue.STATUS_RATE_LIMITED:
            return RateLimitedError(f"{action}: rate limited")
        if status == venue.STATUS_UNAVAILABLE:
            return PolymarketApiError(f"{action}: venue unavailable (503)")
        return PolymarketApiError(f"{action}: {type(exc).__name__}: {exc}")
