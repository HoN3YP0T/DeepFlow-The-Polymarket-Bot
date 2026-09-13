"""Read-only half of the execution adapter.

These tests exist because of a bug that lint, mypy and 604 other tests all passed
over: `polymarket.models.clob.AssetType` is a `Literal` alias, not an enum, so
`AssetType.COLLATERAL` raises `AttributeError` at call time — and `polymarket.*` is
under `ignore_missing_imports`, which makes every SDK symbol `Any`, and `Any.ANYTHING`
type-checks (finding 67). The import path was wrong too, for the same reason.

So the assertions here are about *what is actually sent to the SDK*, not about types:
the asset-type argument is pinned as the string the venue's query string wants. A fake
that accepted any keyword would reproduce the original bug's invisibility.

The write methods are not covered. They are unimplemented and unverified — no order
has been submitted to this venue — and a passing test would misrepresent that.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket.execution import COLLATERAL, PolymarketExecution
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent
from deepflow.core.enums import OrderSide, OrderStatus, OrderType, RunMode
from deepflow.core.errors import ConfigurationError, RateLimitedError
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
