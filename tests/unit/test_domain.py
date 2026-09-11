"""Domain model invariant tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from deepflow.core.domain import BookLevel, OrderBook
from deepflow.core.types import ClobTokenId

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = ClobTokenId("0xtoken")


def _book(bid: str, ask: str) -> OrderBook:
    return OrderBook(
        token_id=TOKEN,
        bids=(BookLevel(price=D(bid), size=D(100)),),
        asks=(BookLevel(price=D(ask), size=D(100)),),
        captured_at=NOW,
    )


def test_best_prices_and_derived_values() -> None:
    book = _book("0.93", "0.95")
    assert book.best_bid == D("0.93")
    assert book.best_ask == D("0.95")
    assert book.mid == D("0.94")
    assert book.spread == D("0.02")


def test_crossed_book_is_rejected() -> None:
    """A crossed book is corrupt data, and quietly accepting it would produce a
    negative spread and a fictitious edge."""
    with pytest.raises(ValidationError, match="crossed book"):
        _book("0.96", "0.95")


def test_locked_book_is_rejected() -> None:
    with pytest.raises(ValidationError, match="crossed book"):
        _book("0.95", "0.95")


def test_empty_book_yields_none_not_zero() -> None:
    """Zero would read downstream as a real price of zero."""
    book = OrderBook(token_id=TOKEN, bids=(), asks=(), captured_at=NOW)
    assert book.best_bid is None
    assert book.mid is None
    assert book.spread is None


def test_one_sided_book_has_no_mid() -> None:
    book = OrderBook(
        token_id=TOKEN,
        bids=(BookLevel(price=D("0.93"), size=D(10)),),
        asks=(),
        captured_at=NOW,
    )
    assert book.best_bid == D("0.93")
    assert book.mid is None


def test_domain_models_are_immutable() -> None:
    book = _book("0.93", "0.95")
    with pytest.raises(ValidationError):
        book.token_id = ClobTokenId("0xother")  # type: ignore[misc]
