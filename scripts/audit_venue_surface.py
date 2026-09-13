"""Diff what the venue sends against what our code can see.

    .venv/bin/python scripts/audit_venue_surface.py            # summary
    .venv/bin/python scripts/audit_venue_surface.py --verbose  # every unseen field

Written after three findings in a row were wrong the same way. Each time a field
existed and our reading of it did not, and each time the conclusion drawn was about
the *venue* rather than about our own view of it:

* "no market/live-game join" -- the id was on the event, not the market (§45)
* "no short-dated crypto markets" -- the window was measured from ``startDate``, which
  is when the market listed, not when the contest starts (§54)
* "cricket has no game id" -- it does, as a *string* in ``eventMetadata.gameId``,
  where a field typed ``int | None`` cannot hold it (§56)

None of those were discoverable by reading prose, and all three were invisible to a
passing test suite. What makes them visible is a mechanical three-way comparison:

    raw JSON keys  ->  SDK model fields  ->  our domain model fields

A key present in the raw payload and absent downstream is either a deliberate
omission or a blind spot, and the difference is not something to decide from memory.
So this prints every one of them with a sample value, because the *value* is what
carries the lesson: ``"1000169067LIVE2026"`` says "string id" at a glance in a way
``gameId: present`` never would.

It is a read-only audit against public endpoints. No credentials, no writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import httpx

from deepflow.core.domain import Market, OrderBook, PublicTrade

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

#: Raw probes: a name, a URL, and how to reach the interesting object inside the
#: response. Chosen to span the payload *shapes* rather than the endpoint list: a
#: sports event, a crypto event, a plain market, a book, a trade. A shape we never
#: fetch is a shape whose fields we cannot claim to know.
PROBES: tuple[tuple[str, str, str], ...] = (
    ("event.sports.live", f"{GAMMA}/events/keyset?live=true&closed=false&limit=5", "events[]"),
    (
        "event.sports.scheduled",
        f"{GAMMA}/events/keyset?closed=false&tag_id=100350&limit=5",
        "events[]",
    ),
    (
        "event.crypto.short_dated",
        f"{GAMMA}/events?closed=false&title_search=Up%20or%20Down&limit=5",
        "[]",
    ),
    ("event.plain", f"{GAMMA}/events?closed=false&limit=5", "[]"),
    ("market.plain", f"{GAMMA}/markets?closed=false&limit=5", "[]"),
    ("market.sports", f"{GAMMA}/markets?closed=false&sports_market_types=moneyline&limit=5", "[]"),
    ("team", f"{GAMMA}/teams?limit=5", "[]"),
    ("sport", f"{GAMMA}/sports", "[]"),
    ("tag", f"{GAMMA}/tags?limit=5", "[]"),
    ("series", f"{GAMMA}/series?limit=3", "[]"),
    ("clob.market", f"{CLOB}/sampling-markets", "data[]"),
    ("clob.simplified", f"{CLOB}/simplified-markets", "data[]"),
    ("data.trade", f"{DATA}/v2/trades?limit=5", "[]"),
    ("data.holders", f"{DATA}/v2/holders?limit=3", "[]"),
    ("data.status", f"{DATA}/v2/status", "."),
    ("gamma.status", f"{GAMMA}/status", "."),
    ("clob.time", f"{CLOB}/time", "."),
)

#: Raw key -> the SDK/domain field it becomes. Anything not listed and not modelled
#: is reported. Kept explicit so "we looked at this and chose to ignore it" is a
#: written decision rather than an assumption.
KNOWN_UNUSED: frozenset[str] = frozenset(
    {
        # Presentation only.
        "image",
        "icon",
        "imageOptimized",
        "iconOptimized",
        "featuredImage",
        "featuredImageOptimized",
        "color",
        "chartColor",
        "seriesColor",
        "logo",
        "twitterCardImage",
        "sponsorName",
        "sponsorImage",
        "showGmpSeries",
        "showGmpOutcome",
        "gmpChartMode",
        "wideFormat",
        "showAllOutcomes",
        "showMarketImages",
        "carouselMap",
        "featured",
        "featuredOrder",
        "sortBy",
        "curationOrder",
        "ordering",
        # Social / editorial.
        "commentCount",
        "commentsEnabled",
        "disqusThread",
        "tweetCount",
        "chats",
        "mailchimpTag",
        "requiresTranslation",
        "createdBy",
        "updatedBy",
        "creator",
        "creators",
        "eventCreators",
        "restricted",
        "new",
        "archived",
        # Features this bot does not use.
        "rewards",
        "clobRewards",
        "rewardsMinSize",
        "rewardsMaxSpread",
        "holdingRewardsEnabled",
        "estimateValue",
        "cantEstimate",
        "estimatedValue",
        "electionType",
        "countryName",
        "isTemplate",
        "templateVariables",
        "templates",
        "cyom",
        "partners",
        "externalPartners",
        "collections",
        "subEvents",
        "parentEvent",
        "parentEventId",
        "version",
        "publishedAt",
        "published_at",
    }
)


def walk(node: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield every ``dotted.path -> value`` in a payload, arrays collapsed to ``[]``."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, value
            yield from walk(value, path)
    elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for item in node[:1]:  # one element is enough to learn the shape
            yield from walk(item, f"{prefix}[]")


def select(payload: Any, selector: str) -> list[Any]:
    """Pull the objects to inspect out of a response body."""
    if selector == ".":
        return [payload]
    if selector == "[]":
        return list(payload)[:3] if isinstance(payload, list) else [payload]
    key = selector.removesuffix("[]")
    inner = payload.get(key) if isinstance(payload, Mapping) else None
    return list(inner)[:3] if isinstance(inner, list) else []


def model_fields(*models: type) -> set[str]:
    """Leaf field names our code models, across the SDK and our domain."""
    names: set[str] = set()
    for model in models:
        for name, field in getattr(model, "model_fields", {}).items():
            names.add(name)
            alias = getattr(field, "validation_alias", None)
            if isinstance(alias, str):
                names.add(alias)
    return names


def _sdk_fields() -> set[str]:
    """Every field name and alias the SDK's gamma/clob models know.

    Imported lazily and defensively: this audit must keep working when the SDK
    reorganises its modules, since noticing that is part of the point.
    """
    names: set[str] = set()
    try:
        from polymarket.models.gamma import event as gamma_event
        from polymarket.models.gamma import market as gamma_market

        for module in (gamma_event, gamma_market):
            for obj in vars(module).values():
                if isinstance(obj, type) and hasattr(obj, "model_fields"):
                    names |= model_fields(obj)
    except ImportError:  # pragma: no cover - shape of the SDK, not of our code
        pass
    return names


def normalise(name: str) -> str:
    return name.replace("_", "").lower()


async def main(verbose: bool) -> int:
    known = {normalise(n) for n in _sdk_fields() | model_fields(Market, OrderBook, PublicTrade)}
    ignored = {normalise(n) for n in KNOWN_UNUSED}

    unseen_total = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for name, url, selector in PROBES:
            try:
                response = await client.get(url)
                response.raise_for_status()
                objects = select(response.json(), selector)
            except Exception as error:
                print(f"\n### {name}\n  PROBE FAILED: {type(error).__name__}: {error}")
                continue

            if not objects:
                print(f"\n### {name}\n  empty response -- nothing to compare")
                continue

            fields: dict[str, Any] = {}
            for obj in objects:
                for path, value in walk(obj):
                    leaf = path.rsplit(".", 1)[-1].removesuffix("[]")
                    if leaf and leaf not in fields:
                        fields[leaf] = value

            unseen = {
                leaf: value
                for leaf, value in fields.items()
                if normalise(leaf) not in known and normalise(leaf) not in ignored
            }
            unseen_total += len(unseen)
            print(f"\n### {name}  ({len(fields)} leaf fields, {len(unseen)} unmodelled)")
            if not unseen:
                print("  all fields accounted for")
                continue
            shown = sorted(unseen)
            for leaf in shown if verbose else shown[:12]:
                sample = json.dumps(unseen[leaf], default=str)
                print(f"  {leaf:34s} = {sample[:96]}")
            if not verbose and len(shown) > 12:
                print(f"  ... {len(shown) - 12} more (--verbose)")

    print(f"\n{unseen_total} unmodelled fields across {len(PROBES)} payload shapes.")
    print(
        "Each is a field the venue sends that neither the SDK nor our domain names. "
        "That is not automatically a bug -- but it is where all three wrong findings "
        "lived, so it is worth reading rather than assuming."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="list every unmodelled field")
    raise SystemExit(asyncio.run(main(parser.parse_args().verbose)))
