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

import asyncio
import re
from collections.abc import Sequence
from contextlib import suppress
from decimal import Decimal
from typing import Any, Final

from deepflow.adapters.polymarket import mapping, venue
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent, OrderRecord, Position
from deepflow.core.enums import OrderSide, OrderStatus, OrderType, RunMode, TimeInForce
from deepflow.core.errors import (
    CancelOnlyModeError,
    ConfigurationError,
    ExecutionUncertainError,
    MatchingEngineRestartingError,
    OrderRejectedError,
    PolymarketApiError,
    PostOnlyModeRequiredError,
    RateLimitedError,
)
from deepflow.core.logging import get_logger
from deepflow.core.types import ClientOrderKey, OrderId

log = get_logger(__name__)

#: The venue's asset-type discriminator for collateral.
#:
#: A plain string on purpose. ``polymarket.models.clob.AssetType`` is a
#: ``Literal["COLLATERAL", "CONDITIONAL", "CONDITIONAL-V2"]`` type alias, **not** an
#: enum, so ``AssetType.COLLATERAL`` raises ``AttributeError`` at call time -- and
#: mypy cannot see it, because ``polymarket.*`` is under ``ignore_missing_imports``
#: (finding 67). The SDK sends this value through to the query string unchanged.
COLLATERAL: Final = "COLLATERAL"

#: pUSD has 6 decimals. The venue reports integer units and this is the divisor, kept
#: named so a raw integer is never mistaken for a dollar figure.
COLLATERAL_SCALE: Final = Decimal(10) ** 6


#: Venue order-post statuses (``AcceptedOrder.status``) mapped onto ours.
#:
#: ``delayed`` is the one that must not be guessed at: the order is accepted, filled
#: amounts are zero and no trade ids exist, and it matches later. Read as a fill it
#: books a position that does not exist; read as a rejection it abandons a live order.
_POST_STATUS: Final[dict[str, OrderStatus]] = {
    "live": OrderStatus.OPEN,
    "matched": OrderStatus.MATCHED_UNSETTLED,
    "delayed": OrderStatus.DELAYED,
}

#: Rejection codes that mean "this order could not fill", not "this order was wrong".
#:
#: A FOK that cannot fill completely, or a FAK with nothing to match, is *killed* --
#: a legitimate terminal outcome of a correct order. Raising ``OrderRejectedError``
#: for it would make a normal no-liquidity result indistinguishable in the journal
#: from a malformed order, so these return a CANCELLED record with a zero fill.
_UNFILLED_CODES: Final = frozenset({"fok_not_filled", "fak_not_filled", "unmatched"})

#: The venue's own market-order lifetimes, keyed by our intent vocabulary.
_MARKET_ORDER_TYPE: Final[dict[TimeInForce, str]] = {
    TimeInForce.FOK: "FOK",
    TimeInForce.FAK: "FAK",
}

#: Error text that makes an otherwise-definitive status *uncertain*.
#:
#: The CLOB overrides the status code from the message: anything containing
#: "context canceled" returns **400** regardless of what actually happened
#: (finding 71). A cancelled context is a dropped request -- precisely the case that
#: must never be retried -- so matching the text is the only way not to read it as a
#: clean refusal.
_UNCERTAIN_TEXT: Final = "context canceled"

#: Last-resort reader for the expected heartbeat id when the SDK surfaces the 400 as
#: text rather than as a parsed body.
_HEARTBEAT_ID_IN_TEXT: Final = re.compile(r'"?heartbeat_id"?\s*[:=]\s*"?([A-Za-z0-9_-]+)"?')


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


def _expected_heartbeat_id(exc: Exception) -> str | None:
    """The id the venue says it wanted, from a rejected heartbeat.

    An invalid or expired ``heartbeat_id`` returns 400 with
    ``{"error_msg": "Invalid Heartbeat ID", "heartbeat_id": "<expected>"}``, so the
    recovery is to adopt that id rather than to treat the beat as broken. Without this
    a single dropped response ends protection permanently, and the only symptom is a
    log line every five seconds.
    """
    for attribute in ("body", "response", "detail", "payload"):
        payload = getattr(exc, attribute, None)
        if isinstance(payload, dict):
            expected = payload.get("heartbeat_id")
            if isinstance(expected, str) and expected:
                return expected
    match = _HEARTBEAT_ID_IN_TEXT.search(str(exc))
    return match.group(1) if match else None


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


class PolymarketExecution:
    """Live order placement, gated by run mode."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._heartbeat: asyncio.Task[None] | None = None

    def _assert_live(self, action: str) -> None:
        """Final interlock. Nothing reaches the venue outside LIVE mode."""
        if self._settings.mode is not RunMode.LIVE:
            raise RuntimeError(f"refusing to {action}: run mode is {self._settings.mode}, not LIVE")

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """Sign and post an order.

        **Unverified against the venue.** No order has ever been submitted with these
        credentials; every branch below is written from the published spec and the
        installed SDK models, and the first live submission is the test. Reads are
        verified (§70); this is not.

        Two things here are easy to get wrong and expensive:

        * **A rejection is a return value, not an exception.** ``place_*_order``
          returns ``AcceptedOrder | RejectedOrder``, and the venue even sends
          ``"success": true`` alongside an ``errorMsg`` for a post-only-mode refusal.
          Code that only catches exceptions reads every rejection as a live order.
        * **A market BUY is denominated in collateral.** ``amount`` is pUSD spent,
          not shares; ``shares`` is the SELL-side parameter. Passing a share count as
          ``amount`` sizes the trade by ``1 / price`` -- a 5% error at 0.95 and
          twentyfold at 0.05.

        Failure semantics the rest of the system depends on:

        * definitive refusal (400/401/404) -> ``OrderRejectedError``
        * 425                              -> ``MatchingEngineRestartingError``
        * 503 post-only / cancel-only      -> ``PostOnlyModeRequiredError`` /
          ``CancelOnlyModeError``
        * 429                              -> ``RateLimitedError``
        * timeout / dropped response / 5xx -> ``ExecutionUncertainError``

        The last case is never retried here. It goes to reconciliation, which uses
        ``find_by_intent`` to establish whether the order landed. The venue documents
        ``500 order timed out`` as provably not-executed, but recognising it means
        matching an error string and being wrong costs a duplicate position, so it
        stays uncertain.
        """
        self._assert_live("submit order")
        client = self._secure("submit")

        try:
            response = await self._place(client, intent)
        except Exception as exc:
            raise self._translate_write(exc, f"submit {intent.client_key}") from exc

        if not getattr(response, "ok", False):
            return self._refusal(response, intent)
        return self._accepted(response, intent)

    async def _place(self, client: Any, intent: OrderIntent) -> Any:
        """Sign the order, then post it. Two calls, deliberately.

        Not ``place_limit_order`` / ``place_market_order``, though they are one call
        and look like the obvious choice: both wrap
        ``post_order_with_allowance_recovery``, which on a 400 allowance rejection
        **submits an on-chain approval transaction and re-posts the order** (finding
        73). Two things about that are wrong for this system rather than wrong in
        general:

        * **Approvals are the relayer's explicit job**, run once at startup where a
          failure is legible, not a side effect of the first trade. A submission path
          that quietly spends gas is a submission path that does something the journal
          does not record.
        * **Retry policy belongs to the order manager.** Its rule is that only a
          definitively-refused order may be re-sent, under a fresh key. A retry buried
          in the SDK is invisible to that rule even when, as here, the rejection it
          retries on happens to be definitive.

        ``create_*`` signs without posting and ``post_order`` posts what was signed, so
        the pair is the same request minus the hidden recovery.

        ``MARKETABLE_LIMIT`` stays a limit order: it crosses the spread but keeps a
        worst-price bound, which a venue market order does not have. Only ``MARKET``
        reaches ``create_market_order``.
        """
        signed = await self._sign(client, intent)
        return await client.post_order(signed)

    async def _sign(self, client: Any, intent: OrderIntent) -> Any:
        """Build and sign the venue order the intent describes."""
        token_id = str(intent.token_id)

        if intent.order_type is OrderType.MARKET:
            order_type = _MARKET_ORDER_TYPE.get(intent.time_in_force, "FAK")
            if intent.side is OrderSide.BUY:
                # ``amount`` is collateral, priced at the intent's bound. The bound
                # also goes in as ``max_price`` so the venue enforces it too, and
                # ``max_spend`` caps the *all-in* cost, fees included -- without it a
                # position sized to the last cent of collateral fails on balance
                # because the taker fee was never in the budget.
                return await client.create_market_order(
                    token_id=token_id,
                    side="BUY",
                    amount=intent.size_shares * intent.limit_price,
                    max_spend=intent.max_spend,
                    max_price=intent.limit_price,
                    order_type=order_type,
                )
            return await client.create_market_order(
                token_id=token_id,
                side="SELL",
                shares=intent.size_shares,
                min_price=intent.limit_price,
                order_type=order_type,
            )

        return await client.create_limit_order(
            token_id=token_id,
            price=intent.limit_price,
            size=intent.size_shares,
            side=str(intent.side),
            post_only=intent.post_only,
            expiration=self._expiration(intent),
        )

    @staticmethod
    def _expiration(intent: OrderIntent) -> int | None:
        """GTD expiry as a Unix timestamp, or ``None`` for a GTC order.

        Passed through rather than derived: ``ExecutionEngine`` already refused a
        lifetime the venue cannot express (under ~2 minutes -- see
        ``venue.gtd_expiration``), so an expiry arriving here is already legal, and
        re-deriving one from the current clock would silently move it.
        """
        if intent.time_in_force is not TimeInForce.GTD or intent.expires_at is None:
            return None
        return int(intent.expires_at.timestamp())

    def _refusal(self, response: Any, intent: OrderIntent) -> OrderRecord:
        """Turn a ``RejectedOrder`` into either a no-fill record or an exception."""
        code = str(getattr(response, "code", "unknown"))
        message = str(getattr(response, "message", ""))

        if code in _UNFILLED_CODES:
            log.info(
                "execution.order_unfilled",
                code=code,
                client_key=str(intent.client_key),
                token_id=str(intent.token_id),
            )
            return OrderRecord(
                client_key=intent.client_key,
                order_id=None,
                status=OrderStatus.CANCELLED,
                filled_shares=Decimal(0),
            )

        if code == venue.POST_ONLY_MODE_CODE:
            raise PostOnlyModeRequiredError()
        raise OrderRejectedError(f"submit {intent.client_key} refused ({code}): {message}")

    def _accepted(self, response: Any, intent: OrderIntent) -> OrderRecord:
        """Map an ``AcceptedOrder`` onto an :class:`OrderRecord`.

        An unrecognised post status becomes ``UNKNOWN`` rather than a guess, which
        routes the order to reconciliation instead of booking a state we do not
        understand.
        """
        raw_status = str(getattr(response, "status", ""))
        status = _POST_STATUS.get(raw_status)
        if status is None:
            log.warning("execution.unknown_post_status", status=raw_status)
            status = OrderStatus.UNKNOWN

        filled = self._filled_shares(response, intent)
        if status is OrderStatus.MATCHED_UNSETTLED and filled >= intent.size_shares:
            status = OrderStatus.FILLED

        order_id = getattr(response, "order_id", None)
        return OrderRecord(
            client_key=intent.client_key,
            order_id=OrderId(str(order_id)) if order_id else None,
            status=status,
            filled_shares=filled,
            average_fill_price=intent.limit_price if filled > 0 else None,
        )

    @staticmethod
    def _filled_shares(response: Any, intent: OrderIntent) -> Decimal:
        """Shares filled at placement, from the response's two amounts.

        **Which amount is shares depends on the side.** The venue documents, and the
        signed struct confirms: for a BUY ``makerAmount`` is ``price x size`` in pUSD
        and ``takerAmount`` is shares; for a SELL the two swap. Reading one of them
        unconditionally as shares is right half the time and off by ``1 / price`` the
        rest -- 20x on a 0.05 contract.

        Both are **6-decimal fixed math**, per the response schema, so both are
        scaled. That scaling is the one part of this method not confirmed against a
        live response, so it is *checked* rather than trusted: a fill cannot exceed
        the order, and a violation raises ``ExecutionUncertainError`` so the truth
        comes from reconciliation reading ``size_matched`` -- an unambiguous share
        count -- instead of from arithmetic that may be off by a million.
        """
        making = Decimal(str(getattr(response, "making_amount", 0) or 0))
        taking = Decimal(str(getattr(response, "taking_amount", 0) or 0))
        raw = taking if intent.side is OrderSide.BUY else making
        filled = raw / venue.AMOUNT_SCALE

        if filled > intent.size_shares:
            raise ExecutionUncertainError(
                f"submit {intent.client_key}: venue reported a fill of {filled} shares "
                f"against an order for {intent.size_shares}. The response amounts do not "
                "scale as documented; reconcile against size_matched before retrying."
            )
        return filled

    async def cancel(self, order_id: OrderId) -> OrderRecord:
        """Cancel one order, and refuse to claim success the venue did not confirm.

        ``CancelOrdersResponse`` splits its answer: ``canceled`` lists what is gone,
        ``not_canceled`` maps an id to a reason. An order in the second set may still
        be **live and able to fill**, so returning a CANCELLED record for it would book
        a state the venue disagrees with -- and the caller would stop watching an order
        that is still working. The venue's own view settles it, and when that is also
        unavailable the outcome is uncertain rather than assumed.
        """
        self._assert_live("cancel order")
        client = self._secure("cancel")

        try:
            response = await client.cancel_order(order_id=str(order_id))
        except Exception as exc:
            if _is_not_found(exc):
                # Not on the book. It filled, expired or was already cancelled; which
                # one is a question for the order's own record, not for the cancel.
                existing = await self.get_order(order_id)
                if existing is not None:
                    return existing
                raise ExecutionUncertainError(
                    f"cancel {order_id}: venue reports no such order and no record of it"
                ) from exc
            raise self._translate_write(exc, f"cancel {order_id}") from exc

        if str(order_id) in {str(cancelled) for cancelled in getattr(response, "canceled", ())}:
            return OrderRecord(
                client_key=ClientOrderKey(str(order_id)),
                order_id=order_id,
                status=OrderStatus.CANCELLED,
            )

        reason = dict(getattr(response, "not_canceled", {}) or {}).get(str(order_id), "no reason")
        existing = await self.get_order(order_id)
        if existing is not None:
            log.warning("execution.cancel_refused", order_id=str(order_id), reason=str(reason))
            return existing
        raise ExecutionUncertainError(f"cancel {order_id} refused ({reason}) and order not found")

    async def cancel_all(self) -> Sequence[OrderRecord]:
        """Panic button behind the dashboard's Cancel All control.

        Returns only what the venue confirmed cancelled, and logs at error level
        anything it refused -- because a partial cancel during a panic is the state
        most worth seeing. Deliberately does **not** raise on a partial result: the
        orders that did cancel are cancelled, and failing the whole call would hide
        that. The caller confirms an empty book with ``list_open_orders``; this method
        cannot promise it.
        """
        self._assert_live("cancel all orders")
        client = self._secure("cancel_all")

        try:
            response = await client.cancel_all()
        except Exception as exc:
            raise self._translate_write(exc, "cancel_all") from exc

        refused = dict(getattr(response, "not_canceled", {}) or {})
        if refused:
            log.error("execution.cancel_all_incomplete", refused=len(refused), reasons=refused)

        cancelled = tuple(str(order_id) for order_id in getattr(response, "canceled", ()))
        log.info("execution.cancel_all", cancelled=len(cancelled), refused=len(refused))
        return tuple(
            OrderRecord(
                client_key=ClientOrderKey(order_id),
                order_id=OrderId(order_id),
                status=OrderStatus.CANCELLED,
            )
            for order_id in cancelled
        )

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
        """Arm the venue-side switch that cancels our resting orders, and keep it armed.

        The venue cancels every open order owned by these CLOB credentials if a valid
        heartbeat does not arrive within 10 seconds (swept every 5s, so cancellation
        can lag by up to 5s more). Once the first heartbeat is accepted the venue
        *expects* the stream to continue.

        **This is the one safety mechanism that survives the process dying.** Every
        circuit breaker in this codebase assumes a running supervisor; a crashed or
        partitioned bot leaves resting orders exposed with nothing watching them, and
        this closes that hole.

        Idempotent: a second call while the loop is running is a no-op rather than a
        second competing beat, because each response returns the id the *next* request
        must carry and two loops would invalidate each other's ids.
        """
        self._assert_live("start the order heartbeat")
        if self._heartbeat is not None and not self._heartbeat.done():
            return
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())

    async def stop_order_heartbeat(self) -> None:
        """Stop beating, on a graceful shutdown.

        Stopping does **not** disarm the switch -- the venue will cancel our resting
        orders about 10 seconds from the last beat, which on a deliberate shutdown is
        the outcome we want. Cancel explicitly first if orders should survive.
        """
        task = self._heartbeat
        self._heartbeat = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _heartbeat_loop(self) -> None:
        """Beat every 5 seconds, carrying the id the last response returned.

        A failed beat is logged and retried on the next tick rather than raised: the
        loop has no caller to raise to, and the failure mode is already safe -- if the
        beats really have stopped, the venue cancels our orders, which is the whole
        point of the mechanism. Latching off on the first transient error would instead
        guarantee the cancellation it is trying to report.
        """
        heartbeat_id = ""
        while True:
            try:
                heartbeat_id = await self._beat(heartbeat_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # see docstring: a raised beat has no caller
                log.warning("execution.heartbeat_failed", error=str(exc))
            await asyncio.sleep(venue.ORDER_HEARTBEAT_SEND_INTERVAL_SECONDS)

    async def _beat(self, heartbeat_id: str) -> str:
        """One heartbeat round-trip. Returns the id the next one must carry.

        Posted through the SDK's authenticated CLOB transport rather than a client
        method, because **the SDK exposes none**: its only heartbeats are WebSocket
        keepalives, and the CLOB's order dead-man's switch (``POST /v1/heartbeats``)
        is unwrapped (finding 72). The transport is reached through a private
        attribute for that reason, and it is the right route rather than a shortcut:
        the L2 HMAC signature must cover the exact serialized body, and the
        transport's header resolver already signs ``(method, path, body)`` with the
        derived CLOB credentials. Hand-rolling that signing is how a heartbeat silently
        400s forever while the operator believes orders are protected.

        An invalid or expired id comes back as 400 *with the expected id in the body*,
        so that case recovers by adopting it instead of failing.
        """
        transport = self._clob_transport()
        try:
            body = await transport.post_json(
                venue.ORDER_HEARTBEAT_PATH, json={"heartbeat_id": heartbeat_id}
            )
        except Exception as exc:
            expected = _expected_heartbeat_id(exc)
            if expected is not None:
                log.info("execution.heartbeat_id_resynced")
                return expected
            raise self._translate_write(exc, "heartbeat") from exc
        return str((body or {}).get("heartbeat_id", ""))

    def _clob_transport(self) -> Any:
        """The authenticated CLOB transport behind the secure client.

        Private SDK surface (``_ctx.secure_clob``), and named in one place so an SDK
        upgrade that moves it fails here with this message rather than as an
        ``AttributeError`` inside the heartbeat loop, where the only symptom would be
        a warning every five seconds.
        """
        client = self._secure("post a heartbeat")
        context = getattr(client, "_ctx", None)
        transport = getattr(context, "secure_clob", None)
        if transport is None:
            raise ConfigurationError(
                "cannot post a heartbeat: the SDK no longer exposes _ctx.secure_clob. "
                "The CLOB order heartbeat has no client method, so this transport is "
                "the only authenticated route to POST /v1/heartbeats."
            )
        return transport

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

        Queried with the ``COLLATERAL`` asset type and the wallet's own signature type.
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
            result = await client.get_balance_allowance(asset_type=COLLATERAL)
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
            result = await client.get_balance_allowance(asset_type=COLLATERAL)
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
                if mapping.position_shares(position) != 0
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

    @staticmethod
    def _translate_write(exc: Exception, action: str) -> Exception:
        """Map a failed *write* onto this system's error vocabulary.

        Separate from :meth:`_translate` because the question a write asks is different
        and far more consequential: not "what went wrong" but **"do we know this did
        not happen?"** A read that fails can simply be repeated; a submission that
        fails ambiguously must never be.

        The ordering is deliberate:

        1. Restricted modes first -- they are waits, not faults, and the SDK already
           classifies them in ``RequestRejectedError.restriction``, which is more
           reliable than our own status sniffing (cancel-only carries no machine code,
           only message text).
        2. Rate limiting next; the request was well-formed.
        3. ``"context canceled"`` before any status test, because the CLOB rewrites
           that case to **400** and 400 otherwise means "definitively refused"
           (finding 71). A dropped request read as a clean refusal is exactly how a
           retry becomes a duplicate position.
        4. Only then the genuinely definitive statuses.
        5. Everything left over -- timeouts, transport errors, 5xx, an unrecognised
           exception -- is **uncertain**. That is the fail-closed default: the cost of
           being wrong here is a missed trade, and the cost of the opposite is two
           positions where one was intended.
        """
        restriction = getattr(exc, "restriction", None)
        if restriction == "restarting":
            return MatchingEngineRestartingError(f"{action}: matching engine restarting")
        if restriction == "post_only":
            return PostOnlyModeRequiredError(retry_after_seconds=getattr(exc, "retry_after", None))
        if restriction == "cancel_only":
            return CancelOnlyModeError(f"{action}: venue in cancel-only mode")

        status = _status_of(exc)
        if status == venue.STATUS_RATE_LIMITED or type(exc).__name__ == "RateLimitError":
            return RateLimitedError(f"{action}: rate limited")

        if _UNCERTAIN_TEXT in str(exc).lower():
            return ExecutionUncertainError(
                f"{action}: request context was cancelled; the venue reports this as 400 "
                "but it does not prove the order was refused"
            )

        if status is not None and venue.is_definitive_rejection(status):
            return OrderRejectedError(f"{action}: refused ({status}): {exc}")

        return ExecutionUncertainError(f"{action}: outcome unknown ({type(exc).__name__}: {exc})")
