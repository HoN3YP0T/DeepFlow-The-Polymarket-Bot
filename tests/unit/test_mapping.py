"""Mapping against payloads recorded live from the venue.

The fixture in ``tests/fixtures/polymarket_payloads.json`` is a real response
captured from ``polymarket-client`` 0.10.0, with the book trimmed to its three
worst and three best levels per side *in wire order*. Wire order is the point:
these tests exist because the venue's ordering is the opposite of ours, and the
mistake is invisible in the field names.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket import mapping
from deepflow.core.enums import OrderSide, OutcomeSide

FIXTURE = Path(__file__).parent.parent / "fixtures" / "polymarket_payloads.json"


def _obj(data: Any) -> Any:
    """Recursively turn recorded JSON into attribute-access objects.

    The mapping reads attributes, so the fixture has to present them that way.
    Deliberately not the real SDK classes: the point is to pin the *shape* we
    observed, so an SDK upgrade that renames a field fails here rather than
    being silently accommodated by a re-parse.
    """
    if isinstance(data, dict):
        return SimpleNamespace(**{k: _obj(v) for k, v in data.items()})
    if isinstance(data, list):
        return [_obj(v) for v in data]
    return data


@pytest.fixture(scope="module")
def payloads() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


# --- Book ordering: the finding this phase existed to catch ----------------
def test_fixture_really_is_in_wire_order(payloads: dict[str, Any]) -> None:
    """Guard the guard. If someone "tidies" the fixture into sorted order, the
    tests below would pass for the wrong reason."""
    bids = [Decimal(level["price"]) for level in payloads["book"]["bids"]]
    asks = [Decimal(level["price"]) for level in payloads["book"]["asks"]]
    assert bids == sorted(bids), "venue sends bids ascending (worst first)"
    assert asks == sorted(asks, reverse=True), "venue sends asks descending (worst first)"


def test_book_is_reversed_into_best_first_order(payloads: dict[str, Any]) -> None:
    book = mapping.to_order_book(_obj(payloads["book"]))
    assert book.best_bid == Decimal("0.04")
    assert book.best_ask == Decimal("0.041")
    assert book.spread == Decimal("0.001")


def test_passing_wire_order_through_would_have_given_a_nonsense_spread(
    payloads: dict[str, Any],
) -> None:
    """Documents the bug that was avoided, and why the crossed-book check missed it.

    Straight through, the best bid reads 0.001 and the best ask 0.999 -- a 99.8c
    spread, which is not crossed and so passed the original validator. Every
    market would have failed the 150 bps spread gate, and a book walked from that
    end would price a 4c contract's fill at 0.999.
    """
    wire_bid = Decimal(payloads["book"]["bids"][0]["price"])
    wire_ask = Decimal(payloads["book"]["asks"][0]["price"])
    assert wire_ask - wire_bid == Decimal("0.998")
    assert not wire_bid >= wire_ask  # the old validator's only check


def test_order_book_rejects_misordered_levels(payloads: dict[str, Any]) -> None:
    """The domain model now refuses the wire order outright."""
    from deepflow.core.domain import BookLevel, OrderBook

    raw = payloads["book"]
    with pytest.raises(ValueError, match="bids must be sorted descending"):
        OrderBook(
            token_id=raw["asset_id"],
            bids=tuple(
                BookLevel(price=Decimal(x["price"]), size=Decimal(x["size"])) for x in raw["bids"]
            ),
            asks=(),
            captured_at=mapping.to_order_book(_obj(raw)).captured_at,
        )


def test_book_preserves_full_depth(payloads: dict[str, Any]) -> None:
    book = mapping.to_order_book(_obj(payloads["book"]))
    assert len(book.bids) == len(payloads["book"]["bids"])
    assert len(book.asks) == len(payloads["book"]["asks"])


# --- Market ---------------------------------------------------------------
def test_market_identifiers_and_status(payloads: dict[str, Any]) -> None:
    market = mapping.to_market(_obj(payloads["market"]))
    assert market.condition_id.startswith("0x")
    assert market.question
    assert market.slug
    assert market.active and not market.closed and market.accepting_orders


def test_fee_schedule_is_carried_through(payloads: dict[str, Any]) -> None:
    """Dropping this makes every downstream EV figure optimistic by the taker fee."""
    market = mapping.to_market(_obj(payloads["market"]))
    assert market.fees_enabled is True
    assert market.fee_schedule is not None
    assert market.fee_schedule.rate == Decimal("0.04")
    assert market.fee_schedule.exponent == Decimal(1)
    assert market.fee_schedule.taker_only is True
    assert market.fee_schedule.rebate_rate == Decimal("0.25")


def test_trading_constraints_are_carried_through(payloads: dict[str, Any]) -> None:
    market = mapping.to_market(_obj(payloads["market"]))
    assert market.minimum_tick_size == Decimal("0.001")
    assert market.minimum_order_size == Decimal("5")


def test_resolution_text_comes_from_description(payloads: dict[str, Any]) -> None:
    """The rules live in ``description``; the ``resolution`` group is UMA plumbing
    and its ``source`` was empty on every market sampled."""
    market = mapping.to_market(_obj(payloads["market"]))
    assert market.resolution_text
    assert "resolve" in market.resolution_text.lower()
    assert market.resolution_source is None


def test_absent_seconds_delay_reads_as_no_delay(payloads: dict[str, Any]) -> None:
    """The venue sends ``None``, not 0, for an undelayed market."""
    assert payloads["market"]["trading"]["seconds_delay"] is None
    assert mapping.to_market(_obj(payloads["market"])).seconds_delay == 0


def test_outcomes_are_flattened_with_sides(payloads: dict[str, Any]) -> None:
    market = mapping.to_market(_obj(payloads["market"]))
    assert [o.side for o in market.outcomes] == [OutcomeSide.YES, OutcomeSide.NO]
    assert all(o.token_id for o in market.outcomes)
    assert market.outcome_for(market.outcomes[0].token_id) is market.outcomes[0]


def test_outcome_without_a_token_id_is_dropped(payloads: dict[str, Any]) -> None:
    """Token ids are ``None`` until a market's book opens. Carrying a placeholder
    would fail far from the cause, since everything downstream keys on it."""
    raw = json.loads(json.dumps(payloads["market"]))
    raw["outcomes"]["no"]["token_id"] = None
    market = mapping.to_market(_obj(raw))
    assert [o.side for o in market.outcomes] == [OutcomeSide.YES]


# --- Trade ----------------------------------------------------------------
def test_trade_maps_with_exact_price(payloads: dict[str, Any]) -> None:
    trade = mapping.to_public_trade(_obj(payloads["trade"]))
    assert trade.price == Decimal(payloads["trade"]["price"])
    assert trade.side in (OrderSide.BUY, OrderSide.SELL)
    assert trade.token_id == payloads["trade"]["asset_id"]
