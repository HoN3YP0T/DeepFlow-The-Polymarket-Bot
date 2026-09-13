"""Regenerate ``tests/fixtures/sports_game_join.json`` from the live venue.

    .venv/bin/python scripts/capture_sports_fixtures.py

The fixture was originally trimmed by hand in a scratch script, which made the
test corpus unreproducible: nobody could refresh it after a venue change without
guessing at what had been kept. This script is that guess removed.

It captures three things the join tests need, and keeps each one small enough to
read in a diff:

* **live_events** -- one fixture per sport family currently in play, which is what
  proves the ``live=True`` sweep carries score and period.
* **one_fixture_many_events** -- every event family of a single fixture, which is
  what proves the fold collapses them into one game rather than nine.
* **suspended_but_live** -- a fixture flagged ``live`` that is not being played, so
  the ``is_in_play`` guard is tested against a real payload and not a fabricated one.

Reruns are not expected to reproduce the file byte for byte -- different fixtures
are in play at different times. What must survive a refresh is the *shape*: a
soccer fixture with many families, a fixture with a composite esports score, a
fixture carrying no ``game_id``, and one suspended fixture. The script warns when
any of those is missing rather than writing a corpus that quietly tests less than
it used to. **A capture that warns should not be committed** -- rerun it when the
missing sport is next in play. It exits non-zero to make that hard to miss.

The file is ~150 KiB of unedited payload. That is deliberate: the bulk is the
venue's own ``markets`` and ``sports`` blocks, and trimming fields to shrink it
would turn a captured corpus into a hand-built one, which is what the tests exist
to avoid depending on.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "sports_game_join.json"

#: Markets kept per event. Enough to exercise the type filter and the event-id
#: attachment; more would only inflate the file.
MARKETS_PER_EVENT = 2

#: A finished fixture with the most event families observed. Pinned rather than
#: discovered so the fold test keeps asserting against the same known-nine case.
REFERENCE_GAME_ID = 90112380


def _trim(event: Any, markets_per_event: int) -> dict[str, Any]:
    payload: dict[str, Any] = event.model_dump(mode="json")
    if payload.get("markets"):
        payload["markets"] = payload["markets"][:markets_per_event]
    return payload


def _one_per_family(events: list[Any]) -> list[Any]:
    """One fixture per league code, so the corpus spans sports without bulk."""
    seen: dict[str, Any] = {}
    for event in events:
        code = str(event.slug).split("-", 1)[0]
        seen.setdefault(code, event)
    return list(seen.values())


async def main() -> int:
    async with PolymarketSession(Settings()) as session:
        client = session.public

        live = list((await client.list_events(live=True, closed=False).first_page()).items)
        family = list(
            (await client.list_events(game_ids=[REFERENCE_GAME_ID], closed=True).first_page()).items
        )

    suspended = [e for e in live if (getattr(e.sports, "period", "") or "").upper() == "SUS"]
    sampled = _one_per_family([e for e in live if e not in suspended])

    captured = {
        "live_events": [_trim(e, MARKETS_PER_EVENT) for e in sampled],
        "one_fixture_many_events": [_trim(e, 1) for e in family],
        "suspended_but_live": [_trim(e, 1) for e in suspended[:1]],
    }

    FIXTURE.write_text(json.dumps(captured, indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size // 1024} KiB)")
    for key, items in captured.items():
        print(f"  {key}: {len(items)}")

    # What the tests actually depend on, checked rather than hoped for.
    warnings = []
    if not captured["one_fixture_many_events"]:
        warnings.append(
            f"game {REFERENCE_GAME_ID} returned nothing -- the fold test loses its case"
        )
    if len(captured["one_fixture_many_events"]) < 2:
        warnings.append("reference fixture has fewer than two event families")
    if not captured["suspended_but_live"]:
        warnings.append("no suspended fixture in play -- the is_in_play guard loses its payload")
    if not any("|" in (e.get("sports") or {}).get("score", "") for e in captured["live_events"]):
        warnings.append("no composite (esports) score captured")
    if not any((e.get("sports") or {}).get("game_id") is None for e in captured["live_events"]):
        warnings.append("no fixture without a game_id captured -- the cricket case is untested")

    for warning in warnings:
        print(f"  WARNING: {warning}")
    return 1 if warnings else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
