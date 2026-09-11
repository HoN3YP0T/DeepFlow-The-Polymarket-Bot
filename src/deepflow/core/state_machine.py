"""Explicit lifecycle state machine for a tracked market.

Section 28 of the specification: illegal transitions must be impossible, not
merely discouraged. The transition table is the single source of truth; the
guard is enforced in one place so no call site can bypass it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from deepflow.core.errors import IllegalTransitionError


class MarketState(StrEnum):
    """Happy-path lifecycle plus terminal/failure states."""

    DISCOVERED = "DISCOVERED"
    CLASSIFIED = "CLASSIFIED"
    VALIDATED = "VALIDATED"
    MONITORED = "MONITORED"
    CANDIDATE = "CANDIDATE"
    SIGNAL = "SIGNAL"
    RISK_APPROVED = "RISK_APPROVED"
    ORDER_PENDING = "ORDER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    MANAGED = "MANAGED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"

    # --- Failure states ---------------------------------------------------
    DATA_STALE = "DATA_STALE"
    MARKET_INVALID = "MARKET_INVALID"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    HALTED = "HALTED"


S = MarketState

#: States from which a market holds no exposure, so abandoning it is free.
_PRE_EXPOSURE: Final[frozenset[MarketState]] = frozenset(
    {S.DISCOVERED, S.CLASSIFIED, S.VALIDATED, S.MONITORED, S.CANDIDATE, S.SIGNAL, S.RISK_APPROVED}
)

#: States where an order may exist or capital is committed. These may never
#: jump straight to CLOSED -- they must pass through reconciliation or exit.
_EXPOSED: Final[frozenset[MarketState]] = frozenset(
    {S.ORDER_PENDING, S.PARTIALLY_FILLED, S.FILLED, S.MANAGED, S.EXIT_PENDING}
)

TERMINAL: Final[frozenset[MarketState]] = frozenset({S.CLOSED, S.MARKET_INVALID})


def _legal_transitions() -> dict[MarketState, frozenset[MarketState]]:
    """Build the adjacency table.

    Kept as a function so the derived edges (every pre-exposure state may be
    abandoned; every live state may be halted) are expressed as rules rather
    than copy-pasted into thirteen rows.
    """
    table: dict[MarketState, set[MarketState]] = {
        S.DISCOVERED: {S.CLASSIFIED},
        S.CLASSIFIED: {S.VALIDATED},
        S.VALIDATED: {S.MONITORED},
        S.MONITORED: {S.CANDIDATE},
        S.CANDIDATE: {S.SIGNAL, S.MONITORED},
        S.SIGNAL: {S.RISK_APPROVED, S.MONITORED},
        S.RISK_APPROVED: {S.ORDER_PENDING, S.MONITORED},
        # Submission may resolve to a fill, a partial, or nothing at all.
        # EXECUTION_UNKNOWN is the honest outcome of a timeout.
        S.ORDER_PENDING: {S.PARTIALLY_FILLED, S.FILLED, S.MONITORED, S.EXECUTION_UNKNOWN},
        S.PARTIALLY_FILLED: {S.FILLED, S.MANAGED, S.EXIT_PENDING, S.EXECUTION_UNKNOWN},
        S.FILLED: {S.MANAGED},
        # MANAGED -> ORDER_PENDING covers adding to an existing position.
        S.MANAGED: {S.EXIT_PENDING, S.ORDER_PENDING, S.CLOSED},
        S.EXIT_PENDING: {S.MANAGED, S.CLOSED, S.EXECUTION_UNKNOWN},
        S.CLOSED: set(),
        # --- Failure states ----------------------------------------------
        # Stale data suspends, it does not destroy: recovery returns to
        # monitoring, or to management if a position is open.
        S.DATA_STALE: {S.MONITORED, S.MANAGED, S.HALTED},
        S.MARKET_INVALID: set(),
        # An indeterminate execution is only resolvable by reconciliation.
        S.EXECUTION_UNKNOWN: {S.RECONCILIATION_REQUIRED, S.HALTED},
        S.RECONCILIATION_REQUIRED: {S.MANAGED, S.MONITORED, S.CLOSED, S.HALTED},
        S.HALTED: {S.MONITORED, S.MANAGED, S.RECONCILIATION_REQUIRED, S.CLOSED},
    }

    for state in _PRE_EXPOSURE:
        table[state] |= {S.MARKET_INVALID, S.DATA_STALE, S.HALTED}
    for state in _EXPOSED:
        table[state] |= {S.DATA_STALE, S.HALTED}

    return {state: frozenset(targets) for state, targets in table.items()}


LEGAL_TRANSITIONS: Final[dict[MarketState, frozenset[MarketState]]] = _legal_transitions()


def can_transition(source: MarketState, target: MarketState) -> bool:
    """Whether ``source -> target`` is a legal edge."""
    return target in LEGAL_TRANSITIONS[source]


@dataclass(frozen=True, slots=True)
class Transition:
    """One recorded move. Immutable, so the history is an audit trail."""

    source: MarketState
    target: MarketState
    reason: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class MarketLifecycle:
    """Guarded state holder for a single market.

    The only way to change ``state`` is :meth:`transition`, which refuses
    illegal edges. Every move carries a human-readable reason so the journal
    can answer "why did this market stop being tradeable?".
    """

    state: MarketState = S.DISCOVERED
    history: list[Transition] = field(default_factory=list)

    def transition(self, target: MarketState, reason: str) -> Transition:
        """Move to ``target``.

        Raises:
            IllegalTransitionError: if the edge is not in the table. Callers
                that legitimately might race should check :func:`can_transition`
                first rather than catching this.
        """
        if not can_transition(self.state, target):
            raise IllegalTransitionError(source=self.state, target=target)
        move = Transition(source=self.state, target=target, reason=reason)
        self.state = target
        self.history.append(move)
        return move

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def holds_exposure(self) -> bool:
        """True when capital may be committed, so shutdown cannot just drop it."""
        return self.state in _EXPOSED
