"""Prove the football chain against the live venue.

    .venv/bin/python scripts/verify_football.py

Read-only, places nothing, writes nothing. Walks the exact path the running process
takes: in-play fixtures -> soccer rules -> the state store -> ``FootballEngine``, and
prints what it priced or why it abstained.

**Soccer is not always on.** European kick-offs cluster in the afternoon and evening
UTC, and at some hours the venue has no live soccer at all. That is reported as
"nothing to check", not as a pass: a verification that cannot see its subject has not
verified anything, and the distinction is the point of running it.

What the output shows for every in-play soccer fixture: the parsed state, the three
result probabilities, and for each market in the result group whether the engine
matched it to home, draw or away. The three must sum to one -- they are one
distribution split across three binary markets, and if they do not, the entity match
is wrong somewhere.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from deepflow.adapters.polymarket.games import GammaGameLinks
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import MarketSnapshot
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ConditionId
from deepflow.engines.sports.football import FootballEngine
from deepflow.engines.sports.live_state import MatchStateStore
from deepflow.engines.sports.rules import SportRegistry, category_for
from deepflow.engines.sports.rules.base import SportKind


async def main() -> int:
    failures = 0
    settings = Settings()

    async with PolymarketSession(settings) as session:
        links = await GammaGameLinks(session).in_play()

        # Loaded from the venue, exactly as the orchestrator does. A bare registry
        # knows only the explicitly-named leagues and calls epl, lal, ukr1 and the rest
        # UNKNOWN -- so a verification built on one would report "no soccer in play"
        # while the venue was running eleven soccer leagues.
        registry = SportRegistry()
        added = await registry.load_from_venue(session.public)
        print(f"league map: {registry.leagues_known} leagues ({added} from the venue)")

        soccer = []
        for link in links:
            kind = registry.sport_for(link)
            if kind is SportKind.SOCCER:
                soccer.append((link, registry.parse(link)))

        print(f"in-play fixtures: {len(links)}   soccer among them: {len(soccer)}")
        if not soccer:
            print("\nnothing to check: no soccer in play right now.")
            print("  This is not a pass. Re-run during European afternoon/evening UTC.")
            print("  Other sports in play:")
            for link in links[:8]:
                print(f"    {link.slug:44} {registry.sport_for(link)}")
            return 0

        store = MatchStateStore()
        engine = FootballEngine(settings.thresholds.sports.football, states=store)

        for link, state in soccer:
            print("=" * 72)
            print(f"{link.slug}   {link.home_team} vs {link.away_team}")
            if state is None:
                print("  FAIL: fixture did not parse")
                failures += 1
                continue
            print(
                f"  score={state.home_score}-{state.away_score} half={state.period_index} "
                f"elapsed={state.seconds_elapsed} remaining={state.seconds_remaining} "
                f"modellable={state.is_modellable}"
            )
            if category_for(SportKind.SOCCER) is not MarketCategory.FOOTBALL:
                print("  FAIL: soccer does not map to the FOOTBALL category")
                failures += 1

            if not link.home_team or not link.away_team:
                print("  FAIL: fixture named no sides, so no market can be matched")
                failures += 1
                continue

            markets = link.tradeable_markets
            store.observe(
                tuple(m.condition_id for m in markets),
                state,
                home_team=link.home_team,
                away_team=link.away_team,
            )

            total = Decimal(0)
            priced = 0
            for market in markets:
                snapshot = MarketSnapshot(
                    condition_id=ConditionId(str(market.condition_id)),
                    books=(),
                    captured_at=state
                    and __import__("datetime").datetime.now(__import__("datetime").UTC),
                )
                estimate = await engine.estimate(
                    market=market,
                    snapshot=snapshot,
                    token_id=market.outcomes[0].token_id,
                )
                title = market.group_item_title or market.question[:40]
                if estimate is None:
                    print(f"    abstained  {title!r} (type={market.sports_market_type})")
                    continue
                priced += 1
                total += estimate.model_probability
                print(
                    f"    {estimate.inputs['result']:5} {title!r}: "
                    f"p={float(estimate.model_probability):.4f} "
                    f"+/-{float(estimate.uncertainty):.3f}"
                )

            if priced == 3:
                # The three markets of a result group are one distribution. A sum that
                # is not one means an entity was matched to the wrong result, which is
                # the error that prices the complement of the intended trade.
                print(f"    result group sums to {float(total):.6f}")
                if abs(total - Decimal(1)) > Decimal("0.0001"):
                    print("    FAIL: the three results do not sum to one")
                    failures += 1
            elif priced:
                print(f"    note: priced {priced} of the group, not all three")

    print("\nOK" if not failures else f"\n{failures} failure(s)")
    return failures


raise SystemExit(asyncio.run(main()))
