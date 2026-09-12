"""Resolution rule parsing.

The highest-value safety component in the system, so the tests lean on the rule it
exists to enforce: the payout condition comes from the resolution text, never from
the market title. Texts here are copied from live markets rather than invented,
including their typographic quotes.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from deepflow.core.domain import Market, Outcome, ResolutionCriteria
from deepflow.core.enums import OutcomeSide, ResolutionValidity
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.pipeline.resolution import ResolutionValidator

# Live text, curly quotes intact. This is the shape that breaks a naive parser.
BINARY_TEXT = (
    "This market will resolve to “Yes” if the named individual wins and "
    "accepts the 2028 nomination of the Democratic Party for U.S. president. "
    "Otherwise, this market will resolve to “No”.\n\n"
    "The resolution source for this market will be a consensus of official "
    "Democratic Party sources."
)

GROUP_TEXT = (
    "The 2028 US Presidential Election is scheduled to take place on November 7, "
    "2028.\n\nThis market will resolve to the person who wins the 2028 US "
    "Presidential Election.\n\nThe resolution source for this market is the "
    "Associated Press."
)

ACCORDING_TO_TEXT = (
    "This market will resolve according to the party that wins control of the "
    "United States Senate in the 2026 United States midterm election.\n\n"
    "The resolution source is official information from relevant state election "
    "authorities."
)


@pytest.fixture
def validator() -> ResolutionValidator:
    return ResolutionValidator()


def _market(text: str | None, *, group_item_title: str | None = None) -> Market:
    return Market(
        condition_id=ConditionId("0xabc"),
        question="Will the title mislead you?",
        outcomes=(Outcome(token_id=ClobTokenId("1"), label="Yes", side=OutcomeSide.YES),),
        active=True,
        closed=False,
        accepting_orders=True,
        resolution_text=text,
        group_item_title=group_item_title,
    )


# --- The curly-quote trap -------------------------------------------------
def test_typographic_quotes_are_normalised(validator: ResolutionValidator) -> None:
    """Matching ASCII quotes alone detects the payout clause on 7% of live markets
    instead of 46%. The cause is one invisible character, so this is pinned."""
    result = validator.validate(_market(BINARY_TEXT))
    assert result.yes_condition is not None
    assert "nomination of the Democratic Party" in result.yes_condition


def test_ascii_quotes_also_work(validator: ResolutionValidator) -> None:
    result = validator.validate(_market(BINARY_TEXT.replace("“", '"').replace("”", '"')))
    assert result.yes_condition is not None


# --- Shapes ---------------------------------------------------------------
def test_explicit_binary_shape(validator: ResolutionValidator) -> None:
    result = validator.validate(_market(BINARY_TEXT))
    assert "shape=explicit_binary" in result.notes
    assert result.no_condition


def test_group_winner_shape_uses_the_entity(validator: ResolutionValidator) -> None:
    """A group market's text names only the contest. The payout condition is *this*
    market's entity winning, and the only place that entity appears is
    group_item_title."""
    result = validator.validate(_market(GROUP_TEXT, group_item_title="Gavin Newsom"))
    assert "shape=group_winner" in result.notes
    assert result.yes_condition is not None
    assert result.yes_condition.startswith("Gavin Newsom is the person who")


def test_group_market_without_an_entity_is_unparseable(
    validator: ResolutionValidator,
) -> None:
    """Inferring the entity from the question title is the one thing this module
    exists to forbid, so a group market missing it must refuse rather than guess."""
    result = validator.validate(_market(GROUP_TEXT, group_item_title=None))
    assert result.validity is ResolutionValidity.UNPARSEABLE


def test_according_to_variant(validator: ResolutionValidator) -> None:
    """This phrasing alone accounted for a large share of the first calibration
    run's unparseable markets."""
    result = validator.validate(_market(ACCORDING_TO_TEXT, group_item_title="Democratic Party"))
    assert result.yes_condition is not None
    assert result.yes_condition.startswith("Democratic Party is the party")


def test_winner_of_variant(validator: ResolutionValidator) -> None:
    text = (
        "This market will resolve according to the winner of the 2026 Ballon d'Or.\n\n"
        "The resolution source is official France Football announcements."
    )
    result = validator.validate(_market(text, group_item_title="Kylian Mbappé"))
    assert result.yes_condition is not None
    assert "Kylian Mbappé" in result.yes_condition


# --- Verdicts -------------------------------------------------------------
def test_no_text_is_unparseable(validator: ResolutionValidator) -> None:
    assert validator.validate(_market(None)).validity is ResolutionValidity.UNPARSEABLE
    assert validator.validate(_market("   ")).validity is ResolutionValidity.UNPARSEABLE


def test_unrecognised_text_is_unparseable_not_valid(
    validator: ResolutionValidator,
) -> None:
    """Failing open here would trade a market whose payout nobody has read."""
    result = validator.validate(_market("Some prose that never says how it settles."))
    assert result.validity is ResolutionValidity.UNPARSEABLE
    assert result.yes_condition is None


def test_judgement_call_marker_is_ambiguous(validator: ResolutionValidator) -> None:
    text = BINARY_TEXT + " Resolution will follow a consensus of credible reporting."
    result = validator.validate(_market(text))
    assert result.validity is ResolutionValidity.AMBIGUOUS
    assert result.ambiguity_rules == "credible reporting"


def test_consensus_of_named_sources_is_not_ambiguous(
    validator: ResolutionValidator,
) -> None:
    """ "consensus of" appears in 58% of live markets, almost always as "a consensus of
    official <named> sources" — a checkable authority. Treating that as ambiguous
    would refuse most of the catalogue over a turn of phrase."""
    result = validator.validate(_market(BINARY_TEXT))
    assert result.validity is ResolutionValidity.VALID


def test_unobservable_source_outranks_ambiguity(validator: ResolutionValidator) -> None:
    """The more specific complaint wins: a market we cannot observe stays unusable
    however crisply worded, and calling it merely ambiguous invites someone to widen
    a margin and trade it."""
    text = BINARY_TEXT + " Settled by internal records and widely reported accounts."
    result = validator.validate(_market(text))
    assert result.validity is ResolutionValidity.UNSUPPORTED_SOURCE


def test_deadline_without_a_timezone_is_ambiguous(
    validator: ResolutionValidator,
) -> None:
    """A date with no zone is ambiguous by up to a day, and on a market settling at a
    date boundary that is the entire question."""
    text = (
        'This market will resolve to "Yes" if the bill passes before December 31, '
        '2026. Otherwise it will resolve to "No". Resolution source: official '
        "congressional records."
    )
    result = validator.validate(_market(text))
    assert result.validity is ResolutionValidity.AMBIGUOUS
    assert result.deadline == datetime(2026, 12, 31)
    assert result.timezone_name is None


def test_deadline_with_a_timezone_is_valid(validator: ResolutionValidator) -> None:
    text = (
        'This market will resolve to "Yes" if the bill passes before December 31, '
        '2026, 11:59 PM ET. Otherwise it will resolve to "No". Resolution source: '
        "official congressional records."
    )
    result = validator.validate(_market(text))
    assert result.validity is ResolutionValidity.VALID
    assert result.timezone_name == "ET"


def test_missing_source_is_ambiguous(validator: ResolutionValidator) -> None:
    text = 'This market will resolve to "Yes" if it rains tomorrow. Otherwise "No".'
    result = validator.validate(_market(text))
    assert result.validity is ResolutionValidity.AMBIGUOUS
    assert "no named resolution source" in " ".join(result.notes)


# --- The structural guard ------------------------------------------------
@pytest.mark.parametrize(
    "validity",
    [
        ResolutionValidity.AMBIGUOUS,
        ResolutionValidity.UNPARSEABLE,
        ResolutionValidity.UNSUPPORTED_SOURCE,
        ResolutionValidity.NOT_CHECKED,
    ],
)
def test_only_valid_is_tradeable(validity: ResolutionValidity) -> None:
    """The risk of grading rather than pass/fail is that "ambiguous" starts being
    read as "tradeable with care". Naming the predicate means any future widening has
    to edit this property, where it is visible and tested."""
    criteria = ResolutionCriteria(validity=validity, yes_condition="something")
    assert not criteria.is_tradeable


def test_valid_is_tradeable() -> None:
    assert ResolutionCriteria(validity=ResolutionValidity.VALID).is_tradeable


def test_not_checked_is_the_default_for_a_new_market(
    validator: ResolutionValidator,
) -> None:
    criteria = ResolutionValidator.not_checked()
    assert criteria.validity is ResolutionValidity.NOT_CHECKED
    assert not criteria.is_tradeable


# --- Extraction quality --------------------------------------------------
def test_extracted_conditions_read_on_one_line(validator: ResolutionValidator) -> None:
    """Journal entries are read by humans; a clause with embedded newlines is not."""
    result = validator.validate(_market(GROUP_TEXT, group_item_title="Gavin Newsom"))
    assert result.yes_condition is not None
    assert "\n" not in result.yes_condition


def test_source_is_captured_for_a_group_market(validator: ResolutionValidator) -> None:
    result = validator.validate(_market(GROUP_TEXT, group_item_title="Gavin Newsom"))
    assert result.primary_source is not None
    assert "Associated Press" in result.primary_source
