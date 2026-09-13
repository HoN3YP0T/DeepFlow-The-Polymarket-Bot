"""Read-only half of the execution adapter.

These tests exist because of a bug that lint, mypy and 604 other tests all passed
over: `polymarket.models.clob.AssetType` is a `Literal` alias, not an enum, so
`AssetType.COLLATERAL` raises `AttributeError` at call time — and `polymarket.*` is
under `ignore_missing_imports`, which makes every SDK symbol `Any`, and `Any.ANYTHING`
type-checks (finding 67). The import path was wrong too, for the same reason.

So the assertions here are about *what is actually sent to the SDK*, not about types:
the asset-type argument is pinned as the string the venue's query string wants. A fake
that accepted any keyword would reproduce the original bug's invisibility.

The write tests below pin semantics, not liveness. **No order has been submitted to
this venue**, so every write path is written from the published spec and the installed
SDK models; these tests state what the code believes, and the first live submission is
what confirms it.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket import venue
from deepflow.adapters.polymarket.execution import COLLATERAL, PolymarketExecution
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent
from deepflow.core.enums import OrderSide, OrderStatus, OrderType, RunMode
from deepflow.core.errors import (
    CancelOnlyModeError,
    ConfigurationError,
    ExecutionUncertainError,
    MatchingEngineRestartingError,
    OrderRejectedError,
    PostOnlyModeRequiredError,
    RateLimitedError,
)
from deepflow.core.types import ClientOrderKey, ClobTokenId, ConditionId, OrderId

TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


def _intent(*, price: str = "0.95", size: str = "100") -> OrderIntent:
    return OrderIntent(
        client_key=ClientOrderKey("k1"),
        condition_id=ConditionId("0xcond"),
        token_id=ClobTokenId(TOKEN),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        size_shares=Decimal(size),
        limit_price=Decimal(price),
        max_slippage_bps=Decimal(50),
    )


class _Page:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


class _Query:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def first_page(self) -> _Page:
        return _Page(self._items)


class _Client:
    """A fake that is strict about the one argument mypy could not check."""

    def __init__(
        self,
        *,
        orders: list[Any] | None = None,
        positions: list[Any] | None = None,
        balance: int = 5_000_000,
        allowance: int = 5_000_000,
        closed_only: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self.orders = orders or []
        self.positions = positions or []
        self.balance = balance
        self.allowance = allowance
        self.closed_only = closed_only
        self.raises = raises
        self.asset_types: list[object] = []

    async def get_balance_allowance(self, *, asset_type: object) -> Any:
        if asset_type != "COLLATERAL":
            raise AttributeError(f"venue rejects asset_type={asset_type!r}")
        self.asset_types.append(asset_type)
        return SimpleNamespace(balance=self.balance, allowance=self.allowance)

    async def get_closed_only_mode(self) -> bool:
        return self.closed_only

    def list_open_orders(self, **_: object) -> _Query:
        if self.raises:
            raise self.raises
        return _Query(self.orders)

    def list_positions(self, **_: object) -> _Query:
        return _Query(self.positions)

    async def get_order(self, *, order_id: str) -> Any:
        if self.raises:
            raise self.raises
        for order in self.orders:
            if str(getattr(order, "id", "")) == order_id:
                return order
        raise RuntimeError("order not found")


def _sdk_order(
    *,
    order_id: str = "o1",
    price: str = "0.95",
    size: str = "100",
    side: str = "BUY",
    token: str = TOKEN,
    filled: str = "0",
    status: str = "LIVE",
) -> Any:
    return SimpleNamespace(
        id=order_id,
        asset_id=token,
        side=side,
        price=Decimal(price),
        original_size=Decimal(size),
        size_matched=Decimal(filled),
        status=status,
    )


def _adapter(client: _Client | None, *, mode: RunMode = RunMode.PAPER) -> PolymarketExecution:
    session = SimpleNamespace(secure=client)
    settings = Settings(mode=mode)
    return PolymarketExecution(session, settings)  # type: ignore[arg-type]


# --- Finding 67 -----------------------------------------------------------
def test_the_collateral_asset_type_is_the_string_the_venue_wants() -> None:
    """`AssetType` is a Literal alias; attribute access on it raises. The constant
    must stay a plain string or both collateral reads die on their first call."""
    assert COLLATERAL == "COLLATERAL"
    assert isinstance(COLLATERAL, str)


@pytest.mark.asyncio
async def test_balance_is_scaled_out_of_the_venues_integer_units() -> None:
    client = _Client(balance=1_234_560)
    balance = await _adapter(client).get_collateral_balance()
    assert balance == Decimal("1.23456")
    assert client.asset_types == ["COLLATERAL"]


@pytest.mark.asyncio
async def test_allowance_is_reported_separately_from_balance() -> None:
    """A funded account with a zero allowance rejects every order while plainly
    having funds, so the two figures cannot be collapsed."""
    result = await _adapter(_Client(balance=5_000_000, allowance=0)).get_allowances()
    assert result == {"balance": Decimal(5), "allowance": Decimal(0)}


# --- Reads need credentials, not LIVE mode -------------------------------
@pytest.mark.asyncio
async def test_reads_work_in_paper_mode() -> None:
    """The mode interlock guards submission. Gating reads on LIVE would make
    reconciliation impossible to verify before enabling trading."""
    assert await _adapter(_Client(), mode=RunMode.PAPER).get_closed_only_mode() is False


@pytest.mark.asyncio
async def test_a_missing_secure_client_names_the_settings_involved() -> None:
    with pytest.raises(ConfigurationError) as caught:
        await _adapter(None).get_collateral_balance()
    message = str(caught.value)
    assert "PRIVATE_KEY" in message and "WALLET_ADDRESS" in message


# --- find_by_intent -------------------------------------------------------
@pytest.mark.asyncio
async def test_an_order_is_found_by_its_fingerprint() -> None:
    client = _Client(orders=[_sdk_order()])
    found = await _adapter(client).find_by_intent(_intent())
    assert found is not None
    assert found.order_id == OrderId("o1")
    assert found.client_key == ClientOrderKey("k1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        _sdk_order(price="0.951"),
        _sdk_order(size="101"),
        _sdk_order(side="SELL"),
        _sdk_order(token="999"),
    ],
    ids=["adjacent tick", "different size", "opposite side", "other token"],
)
async def test_a_near_miss_is_not_our_order(order: Any) -> None:
    """Compared exactly. A tolerance here would claim a genuinely different order at
    an adjacent tick as ours, which suppresses a legitimate retry."""
    assert await _adapter(_Client(orders=[order])).find_by_intent(_intent()) is None


@pytest.mark.asyncio
async def test_absence_is_returned_not_raised() -> None:
    """Absent from the *open* set only. The reconciler pairs this with a position
    read, because a filled order is not an open one."""
    assert await _adapter(_Client(orders=[])).find_by_intent(_intent()) is None


@pytest.mark.asyncio
async def test_a_lookup_failure_is_translated_never_swallowed() -> None:
    """"We could not tell" must not reach the reconciler as "it is not there"."""
    client = _Client(raises=_http(429, "rate limited"))
    with pytest.raises(RateLimitedError):
        await _adapter(client).find_by_intent(_intent())


# --- get_order ------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_not_found_order_is_none_not_an_error() -> None:
    """`None` is the answer the reconciler turns into permission to re-intend, so
    collapsing it into an exception makes every absence unresolvable."""
    assert await _adapter(_Client(orders=[])).get_order(OrderId("missing")) is None


@pytest.mark.asyncio
async def test_a_working_order_maps_to_open() -> None:
    record = await _adapter(_Client(orders=[_sdk_order()])).get_order(OrderId("o1"))
    assert record is not None
    assert record.status is OrderStatus.OPEN


@pytest.mark.asyncio
async def test_an_unrecognised_status_is_unknown_not_a_guess() -> None:
    client = _Client(orders=[_sdk_order(status="SOMETHING_NEW")])
    record = await _adapter(client).get_order(OrderId("o1"))
    assert record is not None
    assert record.status is OrderStatus.UNKNOWN


# --- Positions ------------------------------------------------------------
@pytest.mark.asyncio
async def test_zero_share_positions_are_dropped() -> None:
    """The venue keeps settled positions in the list at zero size. Carrying them
    into exposure would bill the account for inventory it does not hold."""
    # Field names taken from `polymarket.models.data.portfolio.Position`: shares are
    # `current_size` and the token is `asset_id`. A fake using `size`/`asset` is a
    # payload the venue never sends, and is exactly what let finding 68 survive.
    live = SimpleNamespace(
        asset_id=TOKEN,
        condition_id="0xcond",
        current_size=Decimal(100),
        avg_price=Decimal("0.95"),
    )
    settled = SimpleNamespace(
        asset_id="other",
        condition_id="0xother",
        current_size=Decimal(0),
        avg_price=Decimal("0.5"),
    )
    positions = await _adapter(_Client(positions=[live, settled])).list_positions()
    assert len(positions) == 1
    assert positions[0].shares == Decimal(100)
    assert positions[0].token_id == TOKEN


@pytest.mark.asyncio
async def test_a_held_position_is_never_reported_as_flat() -> None:
    """The regression for finding 68: `to_position` and the zero-filter each read a
    field the model does not have, so a funded account reconciled as flat -- the one
    state the LIVE interlock exists to prevent acting on."""
    held = SimpleNamespace(
        asset_id=TOKEN,
        condition_id="0xcond",
        current_size=Decimal("37.5"),
        avg_price=Decimal("0.91"),
    )
    positions = await _adapter(_Client(positions=[held])).list_positions()
    assert [p.shares for p in positions] == [Decimal("37.5")]


def _http(status: int, message: str) -> Exception:
    error = RuntimeError(message)
    error.status_code = status  # type: ignore[attr-defined]
    return error


# --- Writes ---------------------------------------------------------------
# Gated on LIVE, so these adapters are built in LIVE mode. Nothing here touches a
# network: every venue call goes to a fake.
def _live(client: _Writes) -> PolymarketExecution:
    settings = Settings(
        _env_file=None,
        mode=RunMode.LIVE,
        live_trading_confirmed=True,
        live_trading_ack="I ACCEPT REAL CAPITAL RISK",
        api={"jwt_secret": "x" * 32},
        polymarket={
            "private_key": "0x" + "1" * 64,
            "wallet_address": "0xabc",
            "relayer_api_key": "k",
            "relayer_api_key_address": "0x43c3F958321EAE86333fE97993E58dCB5B9B7B49",
        },
    )
    return PolymarketExecution(SimpleNamespace(secure=client), settings)  # type: ignore[arg-type]


class _Writes:
    """Records what reached the venue, and returns whatever the test dictates."""

    def __init__(
        self,
        *,
        response: Any = None,
        raises: Exception | None = None,
        cancel: Any = None,
        orders: list[Any] | None = None,
    ) -> None:
        self.response = response
        self.raises = raises
        self.cancel_response = cancel
        self.orders = orders or []
        self.limit_calls: list[dict[str, Any]] = []
        self.market_calls: list[dict[str, Any]] = []
        self.posted: list[Any] = []

    async def create_limit_order(self, **kwargs: Any) -> Any:
        if self.raises:
            raise self.raises
        self.limit_calls.append(kwargs)
        return SimpleNamespace(kind="limit")

    async def create_market_order(self, **kwargs: Any) -> Any:
        if self.raises:
            raise self.raises
        self.market_calls.append(kwargs)
        return SimpleNamespace(kind="market")

    async def place_limit_order(self, **kwargs: Any) -> Any:
        raise AssertionError(
            "place_limit_order wraps post_order_with_allowance_recovery, which approves "
            "on chain and re-posts the order behind our back (finding 73)"
        )

    async def place_market_order(self, **kwargs: Any) -> Any:
        raise AssertionError("place_market_order hides an approval and a retry (finding 73)")

    async def post_order(self, signed: Any) -> Any:
        self.posted.append(signed)
        return self.response

    async def cancel_order(self, *, order_id: str) -> Any:
        if self.raises:
            raise self.raises
        return self.cancel_response

    async def cancel_all(self) -> Any:
        if self.raises:
            raise self.raises
        return self.cancel_response

    async def get_order(self, *, order_id: str) -> Any:
        for order in self.orders:
            if str(getattr(order, "id", "")) == order_id:
                return order
        raise RuntimeError("order not found")


def _accepted(*, status: str = "live", making: str = "0", taking: str = "0") -> Any:
    """An `AcceptedOrder`, with amounts in the venue's 6-decimal fixed math."""
    return SimpleNamespace(
        ok=True,
        order_id="o9",
        status=status,
        making_amount=Decimal(making),
        taking_amount=Decimal(taking),
        trade_ids=(),
        transactions_hashes=(),
    )


def _rejected(code: str, message: str = "no") -> Any:
    return SimpleNamespace(ok=False, code=code, message=message)


def _rejection(status: int, message: str, *, restriction: str | None = None) -> Exception:
    error = RuntimeError(message)
    error.status_code = status  # type: ignore[attr-defined]
    if restriction is not None:
        error.restriction = restriction  # type: ignore[attr-defined]
    return error


@pytest.mark.asyncio
async def test_writes_are_refused_outside_live_mode() -> None:
    """The last interlock before the network: no upstream bug — a mis-set flag, a
    stale config object, a test harness — can produce a live order from PAPER."""
    adapter = _adapter(_Client(), mode=RunMode.PAPER)
    for call in (
        adapter.submit(_intent()),
        adapter.cancel(OrderId("o1")),
        adapter.cancel_all(),
        adapter.start_order_heartbeat(),
    ):
        with pytest.raises(RuntimeError, match="not LIVE"):
            await call


# --- A rejection is a return value, not an exception ----------------------
@pytest.mark.asyncio
async def test_a_rejection_is_read_from_the_response_not_from_an_exception() -> None:
    """`place_*_order` returns AcceptedOrder | RejectedOrder, and the venue even sends
    `"success": true` with an errorMsg for a post-only refusal. Code that only catches
    exceptions books every rejection as a live order."""
    venue_client = _Writes(response=_rejected("not_enough_balance", "not enough balance"))
    with pytest.raises(OrderRejectedError, match="not_enough_balance"):
        await _live(venue_client).submit(_intent())


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["fok_not_filled", "fak_not_filled", "unmatched"])
async def test_an_unfillable_order_is_cancelled_not_rejected(code: str) -> None:
    """A FOK that cannot fill is killed — a legitimate outcome of a correct order.
    Raising would make it indistinguishable in the journal from a malformed one."""
    record = await _live(_Writes(response=_rejected(code))).submit(_intent())
    assert record.status is OrderStatus.CANCELLED
    assert record.filled_shares == Decimal(0)


@pytest.mark.asyncio
async def test_post_only_mode_is_a_wait_not_a_rejection() -> None:
    """Raising OrderRejectedError here would trip the API_FAILURE breaker over an
    announced two-minute window the supervisor should simply sit out."""
    with pytest.raises(PostOnlyModeRequiredError):
        await _live(_Writes(response=_rejected("post_only_mode"))).submit(_intent())


# --- Fill accounting ------------------------------------------------------
@pytest.mark.asyncio
async def test_a_buys_filled_shares_come_from_the_taking_amount() -> None:
    """For a BUY, makerAmount is price x size in pUSD and takerAmount is shares."""
    response = _accepted(status="matched", making="95000000", taking="100000000")
    record = await _live(_Writes(response=response)).submit(_intent())
    assert record.filled_shares == Decimal(100)
    assert record.status is OrderStatus.FILLED


@pytest.mark.asyncio
async def test_a_sells_filled_shares_come_from_the_making_amount() -> None:
    """The two swap by side. Reading `taking` unconditionally is off by 1/price —
    20x on a 0.05 contract."""
    intent = OrderIntent(
        client_key=ClientOrderKey("k2"),
        condition_id=ConditionId("0xcond"),
        token_id=ClobTokenId(TOKEN),
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        size_shares=Decimal(100),
        limit_price=Decimal("0.95"),
        max_slippage_bps=Decimal(50),
    )
    response = _accepted(status="matched", making="100000000", taking="95000000")
    record = await _live(_Writes(response=response)).submit(intent)
    assert record.filled_shares == Decimal(100)


@pytest.mark.asyncio
async def test_a_fill_larger_than_the_order_is_uncertain_not_recorded() -> None:
    """The 6-decimal scaling is documented but unconfirmed against a live response, so
    it is checked: a fill cannot exceed the order. A violation must reach
    reconciliation, which reads size_matched — an unambiguous share count."""
    response = _accepted(status="matched", making="95000000", taking="100000000000")
    with pytest.raises(ExecutionUncertainError, match="do not scale"):
        await _live(_Writes(response=response)).submit(_intent())


@pytest.mark.asyncio
async def test_delayed_is_neither_a_fill_nor_a_rejection() -> None:
    """On a market with seconds_delay the order is accepted with zero filled amounts
    and matches later. Read as a fill it books a position that does not exist."""
    record = await _live(_Writes(response=_accepted(status="delayed"))).submit(_intent())
    assert record.status is OrderStatus.DELAYED
    assert record.filled_shares == Decimal(0)


@pytest.mark.asyncio
async def test_an_unknown_post_status_is_not_guessed() -> None:
    record = await _live(_Writes(response=_accepted(status="something_new"))).submit(_intent())
    assert record.status is OrderStatus.UNKNOWN


# --- What actually reaches the venue -------------------------------------
@pytest.mark.asyncio
async def test_a_market_buy_is_denominated_in_collateral() -> None:
    """`amount` is pUSD spent, not shares. Passing a share count sizes the trade by
    1/price — a 5% error at 0.95 and twentyfold at 0.05."""
    client = _Writes(response=_accepted())
    intent = _intent().model_copy(
        update={"order_type": OrderType.MARKET, "max_spend": Decimal(100)}
    )
    await _live(client).submit(intent)
    call = client.market_calls[0]
    assert call["side"] == "BUY"
    assert call["amount"] == Decimal(100) * Decimal("0.95")
    assert call["max_spend"] == Decimal(100)
    assert call["max_price"] == Decimal("0.95")
    assert "shares" not in call


@pytest.mark.asyncio
async def test_submission_signs_and_posts_rather_than_using_the_one_call_helper() -> None:
    """`place_*_order` wraps post_order_with_allowance_recovery, which on a 400
    allowance rejection submits an on-chain approval and re-posts the order. Approvals
    belong to the relayer at startup, and retry policy to the order manager — so the
    fake raises if either helper is touched."""
    client = _Writes(response=_accepted())
    await _live(client).submit(_intent())
    assert client.limit_calls and [s.kind for s in client.posted] == ["limit"]


@pytest.mark.asyncio
async def test_a_marketable_limit_stays_a_limit_order() -> None:
    """It crosses the spread but keeps a worst-price bound, which a venue market
    order does not have. Only MARKET may lose that bound."""
    client = _Writes(response=_accepted())
    intent = _intent().model_copy(update={"order_type": OrderType.MARKETABLE_LIMIT})
    await _live(client).submit(intent)
    assert client.limit_calls and not client.market_calls


@pytest.mark.asyncio
async def test_a_gtc_order_sends_no_expiration() -> None:
    """A GTD expiry under ~2 minutes is not expressible, so short working orders are
    GTC plus a client-side cancel. Sending an expiry here would rest the order far
    longer than intended."""
    client = _Writes(response=_accepted())
    await _live(client).submit(_intent())
    assert client.limit_calls[0]["expiration"] is None


# --- Error translation ----------------------------------------------------
@pytest.mark.asyncio
async def test_a_cancelled_context_is_uncertain_even_though_it_arrives_as_400() -> None:
    """The CLOB rewrites any message containing "context canceled" to 400, and 400
    otherwise means definitively refused. Read as a refusal, the retry duplicates
    the position."""
    client = _Writes(raises=_rejection(400, "context canceled"))
    with pytest.raises(ExecutionUncertainError):
        await _live(client).submit(_intent())


@pytest.mark.asyncio
async def test_a_genuine_400_is_a_definitive_rejection() -> None:
    client = _Writes(raises=_rejection(400, "invalid price"))
    with pytest.raises(OrderRejectedError):
        await _live(client).submit(_intent())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restriction", "expected"),
    [
        ("restarting", MatchingEngineRestartingError),
        ("post_only", PostOnlyModeRequiredError),
        ("cancel_only", CancelOnlyModeError),
    ],
)
async def test_restricted_modes_are_waits_with_their_own_types(
    restriction: str, expected: type[Exception]
) -> None:
    """Separated from PolymarketApiError so an announced restart is waited out
    instead of tripping the API_FAILURE breaker."""
    client = _Writes(raises=_rejection(503, "restricted", restriction=restriction))
    with pytest.raises(expected):
        await _live(client).submit(_intent())


@pytest.mark.asyncio
async def test_a_timeout_is_uncertain_never_a_rejection() -> None:
    """The fail-closed default for a write: being wrong this way costs a missed
    trade, the other way two positions where one was intended."""
    client = _Writes(raises=TimeoutError("read timed out"))
    with pytest.raises(ExecutionUncertainError):
        await _live(client).submit(_intent())


@pytest.mark.asyncio
async def test_rate_limiting_is_its_own_error() -> None:
    client = _Writes(raises=_rejection(429, "slow down"))
    with pytest.raises(RateLimitedError):
        await _live(client).submit(_intent())


# --- Cancels --------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_confirmed_cancel_is_cancelled() -> None:
    client = _Writes(cancel=SimpleNamespace(canceled=("o1",), not_canceled={}))
    record = await _live(client).cancel(OrderId("o1"))
    assert record.status is OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_a_refused_cancel_reports_the_venues_state_not_cancelled() -> None:
    """An order in `not_canceled` may still be live and able to fill. Claiming
    CANCELLED books a state the venue disagrees with, and the caller stops watching
    an order that is still working."""
    client = _Writes(
        cancel=SimpleNamespace(canceled=(), not_canceled={"o1": "order is being matched"}),
        orders=[_sdk_order(order_id="o1", filled="40")],
    )
    record = await _live(client).cancel(OrderId("o1"))
    assert record.status is OrderStatus.PARTIALLY_FILLED
    assert record.filled_shares == Decimal(40)


@pytest.mark.asyncio
async def test_a_refused_cancel_with_no_record_is_uncertain() -> None:
    client = _Writes(cancel=SimpleNamespace(canceled=(), not_canceled={"o1": "unknown"}))
    with pytest.raises(ExecutionUncertainError):
        await _live(client).cancel(OrderId("o1"))


@pytest.mark.asyncio
async def test_cancel_all_returns_what_was_confirmed_and_does_not_raise_on_a_partial() -> None:
    """A partial cancel during a panic is the state most worth seeing, and failing the
    whole call would hide the orders that did cancel."""
    client = _Writes(
        cancel=SimpleNamespace(canceled=("o1", "o2"), not_canceled={"o3": "being matched"})
    )
    records = await _live(client).cancel_all()
    assert [r.order_id for r in records] == [OrderId("o1"), OrderId("o2")]
    assert all(r.status is OrderStatus.CANCELLED for r in records)


# --- Heartbeat ------------------------------------------------------------
def test_the_heartbeat_route_carries_the_version_prefix() -> None:
    """Most CLOB routes in this SDK are unversioned; this one is not, and the SDK
    wraps no heartbeat at all — its only heartbeats are WebSocket keepalives."""
    assert venue.ORDER_HEARTBEAT_PATH == "/v1/heartbeats"
    assert venue.ORDER_HEARTBEAT_SEND_INTERVAL_SECONDS < venue.ORDER_HEARTBEAT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_a_heartbeat_posts_the_returned_id_back() -> None:
    """Each response returns the id the *next* request must carry."""
    posts: list[Any] = []

    class _Transport:
        async def post_json(self, path: str, *, json: Any) -> Any:
            posts.append((path, json))
            return {"heartbeat_id": "hb-2"}

    client = SimpleNamespace(_ctx=SimpleNamespace(secure_clob=_Transport()))
    adapter = _live(client)  # type: ignore[arg-type]
    assert await adapter._beat("") == "hb-2"
    assert await adapter._beat("hb-2") == "hb-2"
    assert posts == [
        (venue.ORDER_HEARTBEAT_PATH, {"heartbeat_id": ""}),
        (venue.ORDER_HEARTBEAT_PATH, {"heartbeat_id": "hb-2"}),
    ]


@pytest.mark.asyncio
async def test_an_expired_heartbeat_id_resyncs_from_the_error() -> None:
    """400 comes back with the expected id in the body. Without adopting it, one
    dropped response ends order protection permanently."""

    class _Transport:
        async def post_json(self, path: str, *, json: Any) -> Any:
            error = RuntimeError('{"error_msg":"Invalid Heartbeat ID","heartbeat_id":"hb-9"}')
            error.status_code = 400  # type: ignore[attr-defined]
            raise error

    client = SimpleNamespace(_ctx=SimpleNamespace(secure_clob=_Transport()))
    assert await _live(client)._beat("stale") == "hb-9"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_missing_transport_fails_with_a_message_naming_the_reason() -> None:
    """`_ctx.secure_clob` is private SDK surface. An upgrade that moves it must fail
    here, not as an AttributeError inside the loop where the only symptom is a
    warning every five seconds."""
    with pytest.raises(ConfigurationError, match="secure_clob"):
        await _live(SimpleNamespace())._beat("")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_starting_the_heartbeat_twice_does_not_start_two_loops() -> None:
    """Two loops would invalidate each other's ids, since each response returns the
    id the next request must carry."""

    class _Transport:
        async def post_json(self, path: str, *, json: Any) -> Any:
            return {"heartbeat_id": "hb"}

    client = SimpleNamespace(_ctx=SimpleNamespace(secure_clob=_Transport()))
    adapter = _live(client)  # type: ignore[arg-type]
    await adapter.start_order_heartbeat()
    first = adapter._heartbeat
    await adapter.start_order_heartbeat()
    assert adapter._heartbeat is first
    await adapter.stop_order_heartbeat()
    assert adapter._heartbeat is None
