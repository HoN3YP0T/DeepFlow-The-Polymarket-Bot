"""CLOB adapter, with the venue's batch-ordering behaviour reproduced.

The fakes here are deliberately hostile in one specific way: ``get_order_books``
returns results in an order unrelated to the request, because that is what the
live venue does (observed differing on 5 of 5 trials with 12 tokens, at varying
positions). A fake that echoed the request order would let a positional zip pass
its tests and fail in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket.clob import ClobMarketData
from deepflow.core.errors import PolymarketApiError

CAPTURED = datetime(2026, 9, 12, 5, 0, tzinfo=UTC)


def _sdk_book(token_id: str, *, best_bid: str, best_ask: str, depth: int = 3) -> Any:
    """A book in the venue's wire order: bids ascending, asks descending."""
    bid = Decimal(best_bid)
    ask = Decimal(best_ask)
    step = Decimal("0.001")
    return SimpleNamespace(
        asset_id=token_id,
        condition_id="0xcond",
        timestamp=CAPTURED,
        # worst first, as the venue sends them
        bids=[
            SimpleNamespace(price=bid - step * i, size=Decimal(100)) for i in reversed(range(depth))
        ],
        asks=[
            SimpleNamespace(price=ask + step * i, size=Decimal(100)) for i in reversed(range(depth))
        ],
    )


class _FakePublic:
    def __init__(self, books: dict[str, Any], *, drop: set[str] | None = None) -> None:
        self._books = books
        self._drop = drop or set()
        self.calls = 0

    async def get_order_books(self, *, token_ids: list[str]) -> tuple[Any, ...]:
        self.calls += 1
        returned = [self._books[t] for t in token_ids if t not in self._drop]
        # The venue's order is unrelated to the request. Reversing is a stand-in
        # for "arbitrary", and is enough to catch positional zipping.
        return tuple(reversed(returned))

    async def get_order_book(self, *, token_id: str) -> Any:
        return self._books[token_id]


class _FakeSession:
    def __init__(self, public: Any) -> None:
        self.public = public


@pytest.fixture
def tokens() -> list[str]:
    return ["tok_yes", "tok_no", "tok_third"]


@pytest.fixture
def books(tokens: list[str]) -> dict[str, Any]:
    # Prices chosen so a misattribution is unmistakable: a YES/NO pair that are
    # near mirror images, exactly the case where a positional zip is plausible
    # and catastrophic.
    return {
        "tok_yes": _sdk_book("tok_yes", best_bid="0.040", best_ask="0.041"),
        "tok_no": _sdk_book("tok_no", best_bid="0.959", best_ask="0.960"),
        "tok_third": _sdk_book("tok_third", best_bid="0.500", best_ask="0.501"),
    }


async def test_batch_books_are_reprojected_onto_request_order(
    tokens: list[str], books: dict[str, Any]
) -> None:
    clob = ClobMarketData(_FakeSession(_FakePublic(books)))
    result = await clob.get_order_books(tokens)

    assert [b.token_id for b in result] == tokens


async def test_each_book_keeps_its_own_prices(tokens: list[str], books: dict[str, Any]) -> None:
    """The failure this guards: attributing the NO book (0.96) to the YES token
    (0.04). Both are valid prices, the complement of each other, and every gate
    downstream would accept the wrong one."""
    clob = ClobMarketData(_FakeSession(_FakePublic(books)))
    by_token = {b.token_id: b for b in await clob.get_order_books(tokens)}

    assert by_token["tok_yes"].best_ask == Decimal("0.041")
    assert by_token["tok_no"].best_ask == Decimal("0.960")


async def test_binary_complement_invariant(tokens: list[str], books: dict[str, Any]) -> None:
    """A binary market's two best asks sum to roughly 1. This is the cheapest
    live check that books are correctly attributed -- a swap breaks it."""
    clob = ClobMarketData(_FakeSession(_FakePublic(books)))
    yes, no = await clob.get_order_books(["tok_yes", "tok_no"])
    total = yes.best_ask + no.best_ask
    assert Decimal("0.99") < total < Decimal("1.02")


async def test_missing_book_raises_rather_than_shortening_the_sequence(
    tokens: list[str], books: dict[str, Any]
) -> None:
    """Returning fewer books than requested, silently, is how a caller ends up
    reasoning positionally about a sequence that no longer lines up."""
    clob = ClobMarketData(_FakeSession(_FakePublic(books, drop={"tok_no"})))
    with pytest.raises(PolymarketApiError, match="no book for 1 of 3"):
        await clob.get_order_books(tokens)


async def test_empty_request_makes_no_call(books: dict[str, Any]) -> None:
    public = _FakePublic(books)
    clob = ClobMarketData(_FakeSession(public))
    assert await clob.get_order_books([]) == ()
    assert public.calls == 0


async def test_single_book_is_normalized_to_best_first(books: dict[str, Any]) -> None:
    clob = ClobMarketData(_FakeSession(_FakePublic(books)))
    book = await clob.get_order_book("tok_yes")
    assert book.best_bid == Decimal("0.040")
    assert book.best_ask == Decimal("0.041")
    assert book.bids[0].price > book.bids[-1].price  # descending
    assert book.asks[0].price < book.asks[-1].price  # ascending


# --- Slippage walk --------------------------------------------------------
async def test_fill_walks_from_the_best_ask_outward(books: dict[str, Any]) -> None:
    """100 shares fills at the touch; 250 must reach into worse levels."""
    clob = ClobMarketData(_FakeSession(_FakePublic(books)))
    at_touch = await clob.estimate_fill_price("tok_yes", size_shares=Decimal(100))
    deeper = await clob.estimate_fill_price("tok_yes", size_shares=Decimal(250))
    assert at_touch == Decimal("0.041")
    assert deeper is not None and deeper > at_touch


async def test_thin_book_returns_none_not_a_partial_estimate(
    books: dict[str, Any],
) -> None:
    """A book that cannot fill the size has no fill price. Returning the average
    of what it *could* fill would understate slippage precisely when the book is
    too thin to trade -- the case the slippage term exists to catch."""
    clob = ClobMarketData(_FakeSession(_FakePublic(books)))
    assert await clob.estimate_fill_price("tok_yes", size_shares=Decimal(10_000)) is None
