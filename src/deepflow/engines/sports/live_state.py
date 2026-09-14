"""Live fixture state, held where a probability engine can reach it.

An engine is handed a market, a book snapshot and a token id. None of those carry a
score, so a sports model needs the live state kept for it -- the same shape as
:class:`~deepflow.engines.crypto.reference.TwapReference`, which holds the Chainlink
series the crypto model prices against.

**Observation time is stored with the state, not inferred.** A current order book
beside a two-minute-old score is the single most dangerous combination in live sports
trading: the book has already moved on the goal the model cannot see, so the model
reads a stale edge as a large one and buys into it. Every observation here is
timestamped and :meth:`MatchStateStore.get` refuses to return a stale one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from deepflow.core.clock import Clock, SystemClock
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId
from deepflow.engines.sports.rules.base import MatchState

log = get_logger(__name__)

#: How old a fixture observation may be before it is refused.
#:
#: The REST sweep runs on ``live_game_interval_seconds`` (20 s by default), so a
#: healthy observation is never older than that plus one request. 90 seconds allows
#: two missed sweeps and refuses a third: by then the state is old enough that a goal
#: could have been scored, priced by the market, and still be invisible here.
MAX_STATE_AGE: Final = timedelta(seconds=90)


@dataclass(frozen=True)
class FixtureObservation:
    """One fixture's state as of one moment, with the sides named."""

    state: MatchState
    home_team: str
    away_team: str
    observed_at: datetime

    def age(self, now: datetime) -> timedelta:
        return now - self.observed_at


class MatchStateStore:
    """Latest observation per market, refusing stale reads.

    Keyed by condition id rather than by fixture: the engine is asked about a market
    and has nothing else to look up with, and one fixture fans out to several markets
    (home, draw, away, and the half-time and second-half families besides).
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._by_market: dict[ConditionId, FixtureObservation] = {}

    def observe(
        self,
        condition_ids: tuple[ConditionId, ...],
        state: MatchState,
        *,
        home_team: str,
        away_team: str,
    ) -> None:
        """Record one fixture's state against every market built on it."""
        observation = FixtureObservation(
            state=state,
            home_team=home_team,
            away_team=away_team,
            observed_at=self._clock.now(),
        )
        for condition_id in condition_ids:
            self._by_market[condition_id] = observation

    def get(self, condition_id: ConditionId) -> FixtureObservation | None:
        """The current observation for this market, or ``None``.

        ``None`` covers both "never seen" and "too old to use", and the caller wants
        the same thing in either case: abstain. The two are distinguished in the log,
        because a fixture that was never seen is a join problem and one that has gone
        stale is a feed problem.
        """
        observation = self._by_market.get(condition_id)
        if observation is None:
            return None

        age = observation.age(self._clock.now())
        if age > MAX_STATE_AGE:
            log.info(
                "live_state.stale",
                condition_id=str(condition_id),
                age_seconds=round(age.total_seconds(), 1),
            )
            return None
        return observation

    def forget(self, condition_id: ConditionId) -> None:
        """Drop a market, e.g. once its fixture has ended."""
        self._by_market.pop(condition_id, None)

    def tracked(self) -> int:
        """How many markets currently have state. Reported on the health line: zero
        while fixtures are in play means the join is broken, not that nothing is on."""
        return len(self._by_market)
