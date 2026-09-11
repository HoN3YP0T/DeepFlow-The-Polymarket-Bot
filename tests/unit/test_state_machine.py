"""State machine tests. Section 28: illegal transitions must be impossible."""

from __future__ import annotations

import pytest

from deepflow.core.errors import IllegalTransitionError
from deepflow.core.state_machine import (
    LEGAL_TRANSITIONS,
    MarketLifecycle,
    MarketState,
    can_transition,
)

S = MarketState

HAPPY_PATH = [
    S.DISCOVERED,
    S.CLASSIFIED,
    S.VALIDATED,
    S.MONITORED,
    S.CANDIDATE,
    S.SIGNAL,
    S.RISK_APPROVED,
    S.ORDER_PENDING,
    S.FILLED,
    S.MANAGED,
    S.EXIT_PENDING,
    S.CLOSED,
]


def test_happy_path_is_walkable() -> None:
    lifecycle = MarketLifecycle()
    for target in HAPPY_PATH[1:]:
        lifecycle.transition(target, reason="test")
    assert lifecycle.state is S.CLOSED
    assert lifecycle.is_terminal


def test_every_state_has_a_transition_entry() -> None:
    """A state missing from the table would raise KeyError at runtime, in the
    middle of a trade rather than here."""
    assert set(LEGAL_TRANSITIONS) == set(MarketState)


def test_illegal_transition_raises() -> None:
    lifecycle = MarketLifecycle()
    with pytest.raises(IllegalTransitionError):
        lifecycle.transition(S.FILLED, reason="skipping validation")


def test_illegal_transition_does_not_mutate_state() -> None:
    """A refused transition must leave the lifecycle untouched -- a partially
    applied move is worse than no move."""
    lifecycle = MarketLifecycle()
    with pytest.raises(IllegalTransitionError):
        lifecycle.transition(S.CLOSED, reason="nope")
    assert lifecycle.state is S.DISCOVERED
    assert lifecycle.history == []


def test_cannot_skip_resolution_validation() -> None:
    """CLASSIFIED must pass through VALIDATED. This is the edge that enforces
    'never trade solely from the market title'."""
    assert not can_transition(S.CLASSIFIED, S.MONITORED)
    assert can_transition(S.CLASSIFIED, S.VALIDATED)


def test_uncertain_execution_cannot_reach_closed_directly() -> None:
    """An indeterminate order must be reconciled, never assumed away."""
    assert not can_transition(S.EXECUTION_UNKNOWN, S.CLOSED)
    assert can_transition(S.EXECUTION_UNKNOWN, S.RECONCILIATION_REQUIRED)


def test_terminal_states_have_no_outgoing_edges() -> None:
    assert LEGAL_TRANSITIONS[S.CLOSED] == frozenset()
    assert LEGAL_TRANSITIONS[S.MARKET_INVALID] == frozenset()


def test_exposed_states_can_be_halted_but_not_invalidated() -> None:
    """A market holding exposure cannot be dropped as MARKET_INVALID -- that
    would abandon a position that still exists on the venue."""
    assert can_transition(S.MANAGED, S.HALTED)
    assert not can_transition(S.MANAGED, S.MARKET_INVALID)


def test_holds_exposure_flag() -> None:
    lifecycle = MarketLifecycle()
    assert not lifecycle.holds_exposure
    for target in HAPPY_PATH[1 : HAPPY_PATH.index(S.ORDER_PENDING) + 1]:
        lifecycle.transition(target, reason="test")
    assert lifecycle.holds_exposure


def test_history_records_reasons() -> None:
    lifecycle = MarketLifecycle()
    lifecycle.transition(S.CLASSIFIED, reason="matched football tags")
    assert lifecycle.history[0].reason == "matched football tags"
    assert lifecycle.history[0].source is S.DISCOVERED
