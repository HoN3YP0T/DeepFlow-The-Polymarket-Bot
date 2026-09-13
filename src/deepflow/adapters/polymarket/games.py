"""The market <-> live-game join, which does exist.

This module corrects a documented conclusion that was wrong. ``docs/STATUS.md``
listed "no market <-> live-game join key" as a blocker code could not solve,
on the strength of three probes that each failed for a different avoidable
reason:

1. The join was looked for on the **market**, where the field exists but means
   something else. ``Market.sports.game_id`` is ``None`` on every fixture-level
   market sampled, and on a ``child_moneyline`` it holds the id of the *child*
   contest -- ``283508`` for game 1 of a LoL series whose fixture id is
   ``1642158``. Two id spaces sharing one field name, so
   ``list_markets(game_id=<fixture id>)`` returns nothing while looking
   completely correct.
2. ``streams.py`` told callers to "match on ``game_id`` against
   ``Market.game_id``", so the wrong place to look was written down as the
   design.
3. Fixtures were filtered on ``start_date_*``, which is when the *market*
   opened, rather than ``start_time_*``, which is kickoff. A window of
   "kickoff within -3h..+24h" expressed against ``start_date`` excludes almost
   every live fixture, because sports markets open weeks early.

The join is on the **event**: ``list_events(game_ids=...)`` resolves a sports
feed ``gameId`` to the event families built on that fixture. Verified live on
2026-09-13 against seven games streaming at the time -- 7 of 7 resolved, 119
open tradeable markets -- and again against a finished Ligue 1 fixture, where
feed ``gameId`` 90112380 returned ``fl1-str-asm-2026-09-12`` with the same two
teams the feed named.

There is also a cheaper path that needs no socket at all:
``list_events(live=True, closed=False)`` returns every in-play fixture with its
score, period and open markets in one request. At the time of writing that was
15 events and 278 open, order-accepting markets -- including a cricket fixture,
which ``sports_feed`` describes as having no venue-native state source.

Two traps live in here, both observed rather than reasoned about:

* **``live=True`` does not mean in play.** A suspended Chile Primera fixture
  reported ``live=True`` with ``period="SUS"`` and a ``start_time`` three days
  in the future. Trading that as an in-play market means pricing a game that is
  not being played. :meth:`GameLink.is_in_play` is the guard.
* **Not every fixture has a game id.** A live international cricket match
  carried ``score`` and ``period`` but no ``game_id`` at all. It is reachable
  through the ``live=True`` sweep and unreachable through a socket join, because
  the socket keys on the id it does not have. Such fixtures are kept, keyed by
  their event id, rather than dropped for lacking a field.
* **One fixture is many events.** That Ligue 1 game had nine: moneyline,
  halftime result, second half result, exact score, first to score, spreads and
  three first/second-half families. Each carries the same in-play state, so a
  naive per-event loop multiplies one game into nine identical game states.
  :class:`GameLink` aggregates by fixture instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import ValidationError

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.core.domain import Market
from deepflow.core.logging import get_logger
from deepflow.core.types import EventId

log = get_logger(__name__)

TRADEABLE_SPORTS_MARKET_TYPES: Final = frozenset({"moneyline", "child_moneyline"})
"""The only sports market types this system will price.

The venue publishes 240 (``get_sports_market_types``), from ``anytime_touchdowns``
to ``lol_penta_kill``. Two are modellable from score and clock alone, which is
all the feed supplies: ``moneyline`` (who wins the fixture) and
``child_moneyline`` (who wins one map or game of a series). Everything else needs
inputs no free feed carries -- a player prop needs per-player state, a total
needs a scoring-rate model -- and would be priced by guesswork wearing a model's
clothes.

Widening this set is a code change with a test attached, deliberately: each new
type is a new claim that the engines can price it.
"""

#: Period strings that mean "not being played right now" despite ``live=True``.
#: Observed: ``SUS`` on a suspended fixture whose kickoff was three days out.
NOT_IN_PLAY_PERIODS: Final = frozenset({"SUS", "POST", "CAN", "INT", "AB", "DELAYED"})

#: Events per ``game_ids`` request. The venue caps a page at 100 and one fixture
#: can carry five event families, so twenty games is the most that reliably fits
#: in a single page.
GAME_ID_BATCH: Final = 20


@dataclass(frozen=True, slots=True)
class GameLink:
    """One fixture, with every tradeable market the venue built on it.

    Aggregated across the fixture's event families -- see the module docstring on
    why per-event iteration double-counts.
    """

    fixture_key: str
    """Identity for folding. The ``game_id`` when the venue gives one, otherwise
    the first event's id -- see the module docstring on cricket."""

    game_id: int | None
    event_ids: tuple[EventId, ...]
    slug: str
    title: str
    league_tags: tuple[str, ...]
    markets: tuple[Market, ...]
    live: bool = False
    ended: bool = False
    score: str | None = None
    period: str | None = None
    elapsed: str | None = None
    game_status: str | None = None
    start_time: datetime | None = None

    @property
    def league_abbreviation(self) -> str:
        """The league code, taken from the slug's first segment.

        Slugs are ``{league}-{home}-{away}-{date}`` (``fl1-str-asm-2026-09-12``,
        ``cfb-nmxst-hawaii-2026-09-13``), so the league code the sport registry
        resolves on is already here. Exposing it under the name the registry reads
        lets a :class:`GameLink` be parsed by
        :class:`deepflow.engines.sports.rules.SportRegistry` directly, rather than
        needing a second translation layer between the REST sweep and the rules
        that were written against the socket payload.
        """
        return self.slug.split("-", 1)[0]

    @property
    def is_in_play(self) -> bool:
        """Whether the fixture is actually being played.

        ``live`` alone is not enough (see the module docstring), and neither is
        the period alone: a fixture can sit at ``live=True`` with a blank period
        before the first update lands. Both must agree, and kickoff must have
        passed -- a ``start_time`` in the future contradicts any claim of live
        play no matter what the flag says.
        """
        if not self.live or self.ended:
            return False
        if (self.period or "").upper() in NOT_IN_PLAY_PERIODS:
            return False
        return self.start_time is None or self.start_time <= datetime.now(UTC)

    @property
    def tradeable_markets(self) -> tuple[Market, ...]:
        """Markets this system will price: open, order-accepting, right type."""
        return tuple(
            market
            for market in self.markets
            if market.sports_market_type in TRADEABLE_SPORTS_MARKET_TYPES
            and not market.closed
            and market.accepting_orders
            and market.enable_order_book
        )


def _sports(event: Any) -> Any:
    return getattr(event, "sports", None)


def _league_tags(event: Any) -> tuple[str, ...]:
    return tuple(
        str(getattr(tag, "slug", "") or "") for tag in (getattr(event, "tags", None) or ())
    )


def _fixture_key(event: Any) -> str:
    """Fold key for one event.

    Prefers the fixture's ``game_id`` so sibling market families collapse into
    one link. Falls back to the event's own id for fixtures the venue publishes
    without a game id, which keeps them tradeable through the ``live=True`` sweep
    at the cost of not being joinable from the socket.
    """
    game_id = getattr(_sports(event), "game_id", None)
    return str(game_id) if game_id is not None else f"event:{event.id}"


def _merge(existing: GameLink | None, event: Any, markets: tuple[Market, ...]) -> GameLink:
    """Fold one event into the link for its fixture.

    In-play state is taken from the first event that carries it and not
    overwritten: sibling events report the same score, and letting a later
    sibling win would make the resulting state depend on page order.
    """
    sports = _sports(event)
    state = getattr(event, "state", None)
    schedule = getattr(event, "schedule", None)
    event_id = EventId(str(event.id))

    if existing is None:
        game_id = getattr(sports, "game_id", None)
        return GameLink(
            fixture_key=_fixture_key(event),
            game_id=int(game_id) if game_id is not None else None,
            event_ids=(event_id,),
            slug=str(event.slug),
            title=str(event.title),
            league_tags=_league_tags(event),
            markets=markets,
            live=bool(getattr(state, "live", False)),
            ended=bool(getattr(state, "ended", False)),
            score=getattr(sports, "score", None) or None,
            period=getattr(sports, "period", None) or None,
            elapsed=getattr(sports, "elapsed", None) or None,
            game_status=getattr(sports, "game_status", None) or None,
            start_time=getattr(schedule, "start_time", None),
        )

    return GameLink(
        fixture_key=existing.fixture_key,
        game_id=existing.game_id,
        event_ids=(*existing.event_ids, event_id),
        # The shortest slug is the fixture itself; the others suffix a market
        # family onto it (``...-halftime-result``), so the fixture's own slug is
        # the one worth keeping as the link's identity.
        slug=min(existing.slug, str(event.slug), key=len),
        title=existing.title,
        league_tags=existing.league_tags or _league_tags(event),
        markets=(*existing.markets, *markets),
        live=existing.live or bool(getattr(state, "live", False)),
        ended=existing.ended and bool(getattr(state, "ended", False)),
        score=existing.score or (getattr(sports, "score", None) or None),
        period=existing.period or (getattr(sports, "period", None) or None),
        elapsed=existing.elapsed or (getattr(sports, "elapsed", None) or None),
        game_status=existing.game_status or (getattr(sports, "game_status", None) or None),
        start_time=existing.start_time or getattr(schedule, "start_time", None),
    )


def links_from_events(sdk_events: Sequence[Any]) -> dict[str, GameLink]:
    """Fold SDK events into one :class:`GameLink` per fixture, keyed by fixture.

    Pure, so the folding rules above are testable without a network. Events with
    no sports block at all are skipped rather than grouped under a placeholder: a
    non-sports event reaching this function is a filter mistake upstream --
    ``sports_market_types`` is silently ignored by this endpoint, so that mistake
    is easy to make -- and inventing a fixture for it would hide it.

    A missing ``game_id`` is *not* such a case. Cricket fixtures arrive with live
    state and no game id, and dropping them would silently remove a whole sport.
    """
    links: dict[str, GameLink] = {}
    for event in sdk_events:
        if _sports(event) is None:
            continue
        key = _fixture_key(event)
        links[key] = _merge(links.get(key), event, _markets_of(event))
    return links


def _markets_of(event: Any) -> tuple[Market, ...]:
    """Map an event's markets, attaching the event id the nested payload omits.

    ``mapping.to_market`` reads ``event_id`` from the market's own ``events``
    list, which is empty on a market reached *through* an event -- the venue does
    not repeat the parent inside the child. Left alone, every market mapped here
    would lose the one identifier that ties it back to its fixture.
    """
    mapped: list[Market] = []
    event_id = EventId(str(event.id))
    for sdk_market in getattr(event, "markets", None) or ():
        try:
            market = mapping.to_market(sdk_market)
        except (AttributeError, TypeError, ValueError, ValidationError):
            # Narrow on purpose, and still broad: mapping reads roughly thirty
            # fields off a payload the venue can change under us, so the failures
            # worth surviving are a missing attribute, a None where a value was
            # expected, and a domain validator rejecting the result. Anything else
            # is a bug in us and should not be swallowed into a log line -- one
            # unmappable market must not cost the whole fixture, but a broken
            # invariant must not be downgraded to a warning either.
            log.warning("games.market_unmappable", event_id=str(event_id), exc_info=True)
            continue
        if not market.outcomes:
            continue
        mapped.append(market.model_copy(update={"event_id": event_id}))
    return tuple(mapped)


class GammaGameLinks:
    """Resolves fixtures to markets through Gamma's event index."""

    def __init__(self, session: PolymarketSession) -> None:
        self._session = session

    async def in_play(self) -> tuple[GameLink, ...]:
        """Every fixture the venue reports as in play, with its markets.

        One request, no socket. This is the cheaper of the two discovery paths
        and the one to prefer for a cold start: the sports stream only says what
        *changed* since you connected, so a bot that has just started knows
        nothing about a game already at half time until something happens in it.

        Filtered through :attr:`GameLink.is_in_play`, so a suspended fixture
        flagged ``live`` does not come back.
        """
        events = await self._page(self._session.public.list_events(live=True, closed=False))
        links = links_from_events(events)
        in_play = tuple(link for link in links.values() if link.is_in_play)
        log.info(
            "games.in_play",
            events=len(events),
            fixtures=len(links),
            in_play=len(in_play),
            tradeable=sum(len(link.tradeable_markets) for link in in_play),
        )
        return in_play

    async def for_game_ids(self, game_ids: Sequence[int]) -> dict[int, GameLink]:
        """Resolve sports-feed ``gameId`` values to their fixtures.

        Batched: ``game_id`` is a repeatable query parameter, so twenty ids cost
        one request rather than twenty. A feed update for a game with no markets
        is normal -- the venue streams every fixture it tracks, not only the ones
        it lists -- so a missing id is an absence, not an error.
        """
        found: dict[int, GameLink] = {}
        for start in range(0, len(game_ids), GAME_ID_BATCH):
            batch = list(game_ids[start : start + GAME_ID_BATCH])
            events = await self._page(
                self._session.public.list_events(game_ids=batch, closed=False)
            )
            for link in links_from_events(events).values():
                if link.game_id is not None:
                    found[link.game_id] = link
        missing = [game_id for game_id in game_ids if game_id not in found]
        if missing:
            log.info("games.unlisted_fixtures", count=len(missing), game_ids=missing[:10])
        return found

    @staticmethod
    async def _page(paginator: Any) -> tuple[Any, ...]:
        page = await paginator.first_page()
        return tuple(page.items)
