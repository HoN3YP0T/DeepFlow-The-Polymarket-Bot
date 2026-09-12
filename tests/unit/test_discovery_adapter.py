"""Discovery adapter: tradeability filtering, paging, and condition-id lookup."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket.discovery import MAX_PAGE_SIZE, SdkMarketDiscovery
from deepflow.config.settings import Settings
from deepflow.core.types import ConditionId

FIXTURE = Path(__file__).parent.parent / "fixtures" / "polymarket_payloads.json"


def _obj(data: Any) -> Any:
    if isinstance(data, dict):
        return SimpleNamespace(**{k: _obj(v) for k, v in data.items()})
    if isinstance(data, list):
        return [_obj(v) for v in data]
    return data


@pytest.fixture(scope="module")
def raw_market() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["market"]


def _variant(raw: dict[str, Any], **state: Any) -> Any:
    """A copy of the real market with its state flags overridden."""
    data = json.loads(json.dumps(raw))
    data["state"].update(state)
    return _obj(data)


class _FakePaginator:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def iter_items(self) -> AsyncIterator[Any]:
        for item in self._items:
            yield item

    async def first_page(self) -> Any:
        return SimpleNamespace(items=tuple(self._items), next_cursor=None)


class _FakePublic:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.kwargs: dict[str, Any] = {}

    def list_markets(self, **kwargs: Any) -> _FakePaginator:
        self.kwargs = kwargs
        condition_ids = kwargs.get("condition_ids")
        if condition_ids is not None:
            return _FakePaginator(
                [m for m in self._items if str(m.condition_id) == str(condition_ids)]
            )
        return _FakePaginator(self._items)


class _FakeSession:
    def __init__(self, public: Any) -> None:
        self.public = public


def _discovery(items: list[Any]) -> tuple[SdkMarketDiscovery, _FakePublic]:
    public = _FakePublic(items)
    return SdkMarketDiscovery(_FakeSession(public), Settings()), public


# --- Tradeability ---------------------------------------------------------
async def test_a_fully_open_market_is_returned(raw_market: dict[str, Any]) -> None:
    discovery, _ = _discovery([_obj(raw_market)])
    assert len(await discovery.list_active_markets()) == 1


@pytest.mark.parametrize(
    ("flags", "why"),
    [
        ({"active": False}, "not active"),
        ({"closed": True}, "already settled"),
        ({"accepting_orders": False}, "orders refused"),
        ({"enable_order_book": False}, "book never opened"),
    ],
)
async def test_every_status_flag_is_required(
    raw_market: dict[str, Any], flags: dict[str, Any], why: str
) -> None:
    """All four flags matter, and ``enable_order_book`` is the one that is easy to
    forget: a market can be active, open and accepting orders while having no book
    to quote against, and pricing it means quoting off nothing."""
    discovery, _ = _discovery([_variant(raw_market, **flags)])
    assert await discovery.list_active_markets() == (), why


async def test_market_without_token_ids_is_skipped(raw_market: dict[str, Any]) -> None:
    """Token ids are ``None`` until the book opens. Everything downstream keys on
    them, so such a market cannot be traded and must not be returned."""
    data = json.loads(json.dumps(raw_market))
    data["outcomes"]["yes"]["token_id"] = None
    data["outcomes"]["no"]["token_id"] = None
    discovery, _ = _discovery([_obj(data)])
    assert await discovery.list_active_markets() == ()


# --- Paging ---------------------------------------------------------------
async def test_page_size_is_capped_at_the_venue_maximum(
    raw_market: dict[str, Any],
) -> None:
    """The venue silently returns 100 for any larger request, so asking for 500
    and trusting one page would show a fifth of the universe with no error."""
    discovery, public = _discovery([_obj(raw_market)])
    await discovery.list_active_markets(limit=500)
    assert public.kwargs["page_size"] == MAX_PAGE_SIZE


async def test_limit_is_honoured_across_pages(raw_market: dict[str, Any]) -> None:
    discovery, _ = _discovery([_obj(raw_market) for _ in range(10)])
    assert len(await discovery.list_active_markets(limit=4)) == 4


async def test_liquidity_floor_is_pushed_to_the_venue(
    raw_market: dict[str, Any],
) -> None:
    """With no strategy enabled there is no floor to push, so the filter is
    omitted rather than sent as zero."""
    discovery, public = _discovery([_obj(raw_market)])
    await discovery.list_active_markets()
    assert public.kwargs["closed"] is False
    assert public.kwargs["liquidity_num_min"] is None


async def test_enabled_strategy_sets_the_liquidity_floor(
    raw_market: dict[str, Any],
) -> None:
    settings = Settings()
    tennis = settings.thresholds.sports.tennis.model_copy(
        update={"enabled": True, "min_liquidity_usdc": Decimal(7500)}
    )
    sports = settings.thresholds.sports.model_copy(update={"tennis": tennis})
    thresholds = settings.thresholds.model_copy(update={"sports": sports})
    settings = settings.model_copy(update={"thresholds": thresholds})

    public = _FakePublic([_obj(raw_market)])
    await SdkMarketDiscovery(_FakeSession(public), settings).list_active_markets()
    assert public.kwargs["liquidity_num_min"] == 7500.0


# --- Lookup by condition id ----------------------------------------------
async def test_get_market_by_condition_id(raw_market: dict[str, Any]) -> None:
    """``get_market`` takes only id/slug/url, so this routes through
    ``list_markets(condition_ids=...)``. The condition id is what the database and
    the venue's own position endpoints key on, so the translation belongs here
    rather than leaking Gamma's numeric id upward."""
    discovery, public = _discovery([_obj(raw_market)])
    condition_id = ConditionId(str(raw_market["condition_id"]))

    found = await discovery.get_market(condition_id)
    assert found is not None and found.condition_id == condition_id
    assert public.kwargs["condition_ids"] == condition_id


async def test_get_market_returns_none_when_absent(raw_market: dict[str, Any]) -> None:
    discovery, _ = _discovery([_obj(raw_market)])
    assert await discovery.get_market(ConditionId("0xdoesnotexist")) is None
