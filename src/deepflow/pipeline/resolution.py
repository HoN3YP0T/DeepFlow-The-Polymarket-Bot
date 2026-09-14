"""Resolution rule parsing and validation. Section 4.

The single highest-value safety component in the system. A market can be
liquid, fresh, and mispriced, and still lose the full stake because the payout
condition was not what the title implied -- a "will X happen by date D" market
that resolves NO on a technicality, a source hierarchy that defers to an
authority which never publishes, an ambiguity clause that voids the market.

Rule: the bot never infers the payout condition from the market title. The
YES/NO definitions must be extracted from the resolution text, and a market
whose text cannot be parsed is ``UNPARSEABLE`` -- which is not tradeable.

Two resolution *shapes* exist on Polymarket, measured over 250 live markets:

* **Explicit binary** (46%) -- ``resolve to "Yes" if <condition>. Otherwise ...
  "No".`` The conditions are in the text.
* **Group winner** (54%) -- ``This market will resolve to the person who wins
  <event>.`` No YES clause at all, because the payout is winner-takes-all across a
  negative-risk group. The condition is *this market's entity* winning, and the
  only place that entity appears is ``group_item_title`` -- populated on 100% of
  group markets sampled.

A validator that demanded an explicit YES clause would mark that 54% UNPARSEABLE
and refuse over half the catalogue, for markets that are not ambiguous at all.

**The curly-quote trap.** Polymarket writes the payout clause with typographic
quotes (U+201C / U+201D) on most markets, not ASCII ones. Matching ASCII quotes alone detects the
clause on 7% of markets instead of 46% -- so a validator would reject nearly
everything, and the cause would be one invisible character. Every text is
normalised through :data:`_PUNCTUATION` before any pattern runs.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final

from deepflow.core.domain import Market, ResolutionCriteria
from deepflow.core.enums import ResolutionValidity
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: Typographic punctuation the venue actually uses, mapped to ASCII. See the
#: module docstring: this single substitution is the difference between detecting
#: the payout clause on 7% of markets and on 46%.
_PUNCTUATION: Final = str.maketrans(
    {
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
    }
)

#: Phrases that make a market's payout conditional on a judgement call rather
#: than an observable fact. Presence forces AMBIGUOUS pending manual review.
#:
#: Note what is *not* here. "consensus of" appears in 58% of markets, almost always
#: as "a consensus of official <named> sources" -- a specific, checkable authority.
#: Treating that as ambiguous would refuse most of the catalogue over a turn of
#: phrase. The vague cousin, "consensus of credible reporting", is caught by
#: ``credible reporting`` below.
AMBIGUITY_MARKERS: Final[tuple[str, ...]] = (
    "at the discretion",
    "generally accepted",
    "widely reported",
    "credible sources",
    "credible reporting",
    "may be resolved",
    "in the sole judgement",
    "polymarket reserves the right",
)

#: Sources whose publications we cannot observe programmatically. A market that
#: settles against one is not wrong, it is simply not checkable by this system, and
#: pretending otherwise means trusting a number nobody verified.
UNSUPPORTED_SOURCES: Final[tuple[str, ...]] = (
    "private communication",
    "internal records",
    "unspecified source",
    "to be determined",
)

#: Named authorities we can follow. Presence raises confidence in the source.
KNOWN_SOURCE_MARKERS: Final[tuple[str, ...]] = (
    "associated press",
    "reuters",
    "official",
    "resolution source",
    "according to",
    "as reported by",
)

#: Explicit-binary payout clause, after punctuation normalisation.
_YES_CLAUSE = re.compile(
    r'resolve[sd]?\s+(?:to|as)\s+"?yes"?\s*(?:if|when|once)\s+(.{10,400}?)(?:\.\s|\.$|$)',
    re.I | re.S,
)
_NO_CLAUSE = re.compile(
    r'(?:otherwise[^.]{0,80}resolve[sd]?\s+(?:to|as)\s+"?no"?|resolve[sd]?\s+(?:to|as)\s+"?no"?\s*(?:if|when)\s+(.{10,300}?)(?:\.\s|\.$|$))',
    re.I | re.S,
)

#: Group-winner shape. Deliberately permissive, because the venue writes this at
#: least six ways across 23 observed templates:
#:
#:   resolve to the person who wins ...
#:   resolve according to the party that wins ...
#:   resolve according to the listed candidate that wins ...   (multi-word noun)
#:   resolve according to the winner of the 2026 Ballon d'Or   ("of", not "who")
#:   resolve based on OpenAI's market capitalization ...
#:   resolve to according to the candidate who wins ...        (the venue's own typo)
#:
#: Each variant it does not cover is a market marked UNPARSEABLE and never traded,
#: so the cost of being too narrow is silent lost coverage. The cost of being too
#: broad is a garbled ``yes_condition``, which is visible in the journal -- an
#: asymmetry that argues for permissiveness here, with the noun phrase bounded to
#: three words so it cannot swallow the whole sentence.
_GROUP_CLAUSE = re.compile(
    r"resolve[sd]?\s+(?:to\s+)?(?:according\s+to\s+|based\s+on\s+)?"
    r"the\s+([\w'-]+(?:\s+[\w'-]+){0,2}?)\s+(?:who|that|which|of)\s+"
    r"(.{5,200}?)(?:\.\s|\.$|$)",
    re.I | re.S,
)

#: A deadline's timezone. Without one, "by December 31" is ambiguous by up to a day,
#: which on a market settling at a date boundary is the whole question.
_TIMEZONE = re.compile(r"\b(ET|EST|EDT|UTC|GMT|CET|JST)\b")

#: Dates the venue writes, e.g. "December 31, 2026, 11:59 PM ET".
_DEADLINE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})",
    re.I,
)
_MONTHS: Final = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


class ResolutionValidator:
    """Parses resolution rules into a structured, checkable form."""

    def validate(self, market: Market) -> ResolutionCriteria:
        """Parse and classify ``market``'s resolution rules.

        Verdicts, in the order they are decided:

        * ``UNPARSEABLE``        -- no usable text, or no payout condition found
        * ``UNSUPPORTED_SOURCE`` -- settles against something we cannot observe
        * ``AMBIGUOUS``          -- a judgement-call marker, or a deadline with no
                                    timezone
        * ``VALID``              -- a payout condition and a checkable source

        ``UNSUPPORTED_SOURCE`` is checked before ``AMBIGUOUS`` because it is the
        more specific complaint: a market we cannot observe stays unusable however
        crisply it is worded, and reporting it as merely ambiguous would invite
        someone to widen a margin and trade it anyway.
        """
        raw = (market.resolution_text or "").strip()
        if not raw:
            return ResolutionCriteria(
                validity=ResolutionValidity.UNPARSEABLE,
                notes=("no resolution text",),
            )

        text = raw.translate(_PUNCTUATION)
        low = text.lower()
        notes: list[str] = []

        yes_condition, no_condition, shape = self._payout(text, market)
        if yes_condition is None:
            return ResolutionCriteria(
                validity=ResolutionValidity.UNPARSEABLE,
                notes=("no payout condition found in resolution text",),
                primary_source=self._source(text),
            )
        notes.append(f"shape={shape}")

        unsupported = next((s for s in UNSUPPORTED_SOURCES if s in low), None)
        if unsupported is not None:
            return ResolutionCriteria(
                validity=ResolutionValidity.UNSUPPORTED_SOURCE,
                yes_condition=yes_condition,
                no_condition=no_condition,
                notes=(*notes, f"source not observable: {unsupported!r}"),
            )

        deadline = self._deadline(text)
        timezone_name = self._timezone(text)
        source = self._source(text)

        marker = next((m for m in AMBIGUITY_MARKERS if m in low), None)
        if marker is not None:
            return ResolutionCriteria(
                validity=ResolutionValidity.AMBIGUOUS,
                yes_condition=yes_condition,
                no_condition=no_condition,
                deadline=deadline,
                timezone_name=timezone_name,
                primary_source=source,
                ambiguity_rules=marker,
                notes=(*notes, f"judgement-call marker: {marker!r}"),
            )

        if deadline is not None and timezone_name is None:
            # A date without a zone is ambiguous by up to a day, and on a market
            # that settles at a date boundary that is the entire question.
            return ResolutionCriteria(
                validity=ResolutionValidity.AMBIGUOUS,
                yes_condition=yes_condition,
                no_condition=no_condition,
                deadline=deadline,
                primary_source=source,
                notes=(*notes, "deadline has no explicit timezone"),
            )

        if source is None:
            return ResolutionCriteria(
                validity=ResolutionValidity.AMBIGUOUS,
                yes_condition=yes_condition,
                no_condition=no_condition,
                deadline=deadline,
                timezone_name=timezone_name,
                notes=(*notes, "no named resolution source"),
            )

        return ResolutionCriteria(
            validity=ResolutionValidity.VALID,
            yes_condition=yes_condition,
            no_condition=no_condition,
            deadline=deadline,
            timezone_name=timezone_name,
            primary_source=source,
            notes=tuple(notes),
        )

    # --- Extraction -------------------------------------------------------
    def _payout(self, text: str, market: Market) -> tuple[str | None, str | None, str]:
        """Extract the YES condition, the NO condition, and which shape matched."""
        yes = _YES_CLAUSE.search(text)
        if yes is not None:
            no = _NO_CLAUSE.search(text)
            no_condition = None
            if no is not None:
                no_condition = (no.group(1) or "").strip() or "the YES condition is not met"
            return _tidy(yes.group(1)), no_condition, "explicit_binary"

        labelled = _labelled_binary(text, market)
        if labelled is not None:
            return labelled

        group = _GROUP_CLAUSE.search(text)
        if group is not None:
            # The text names the contest; only group_item_title names *this*
            # market's side of it. Without the entity there is no YES condition to
            # state, and inferring it from the question title is the one thing this
            # module exists to forbid.
            if not market.group_item_title:
                return None, None, "group_without_entity"
            noun, contest = group.group(1), _tidy(group.group(2))
            return (
                f"{market.group_item_title} is the {noun} who {contest}",
                f"a different {noun} {contest}",
                "group_winner",
            )

        return None, None, "unrecognised"

    @staticmethod
    def _deadline(text: str) -> datetime | None:
        """First explicit date in the text, as a naive date at midnight.

        Naive on purpose: the timezone is reported separately, and fabricating UTC
        here would make a market with no stated zone look fully specified.
        """
        match = _DEADLINE.search(text)
        if match is None:
            return None
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            return None
        try:
            return datetime(int(match.group(3)), month, int(match.group(2)))
        except ValueError:
            return None

    @staticmethod
    def _timezone(text: str) -> str | None:
        match = _TIMEZONE.search(text)
        return match.group(1) if match else None

    @staticmethod
    def _source(text: str) -> str | None:
        """The named resolution source, if the text names one we could follow."""
        low = text.lower()
        for marker in KNOWN_SOURCE_MARKERS:
            index = low.find(marker)
            if index == -1:
                continue
            sentence = text[index : index + 220]
            return _tidy(sentence.split(".")[0])
        return None

    @staticmethod
    def not_checked() -> ResolutionCriteria:
        """Default state for a freshly discovered market."""
        return ResolutionCriteria(validity=ResolutionValidity.NOT_CHECKED)


def _labelled_binary(text: str, market: Market) -> tuple[str | None, str | None, str] | None:
    """A binary market whose outcomes are **not** labelled Yes/No.

    Crypto up/down markets say:

        This market will resolve to "Up" if the Bitcoin price at the end of the time range
        specified in the title is greater than or equal to the price at the beginning of
        that range. Otherwise, it will resolve to "Down".

    Every word of that is a payout condition and `_YES_CLAUSE` matches none of it, because it
    requires the literal token ``yes``. The result was ``UNPARSEABLE`` on every up/down
    market, and since an unparsed market is never tradeable, the gate refused all of them —
    measured at 400 of 400 decision rows failing ``RESOLUTION_VALID``.

    Read from the market's **own outcome labels** rather than by special-casing Up/Down. The
    venue is free to label a binary anything, and a parser keyed to one vocabulary is a
    parser that will be surprised again; keyed to the labels, it handles whatever the market
    declares. Returns ``None`` when the market is not a two-outcome market or its labels do
    not appear in a resolve clause, so the remaining shapes still get their turn.
    """
    if len(market.outcomes) != 2:
        return None

    first, second = market.outcomes[0].label.strip(), market.outcomes[1].label.strip()
    if not first or not second or {first.lower(), second.lower()} == {"yes", "no"}:
        # Yes/No is `_YES_CLAUSE`'s job, and it reads the pair together rather than one
        # label at a time.
        return None

    clause = re.compile(
        rf'resolve[sd]?\s+(?:to|as)\s+"?{re.escape(first)}"?\s*(?:if|when|once)\s+'
        rf"(.{{10,400}}?)(?:\.\s|\.$|$)",
        re.I | re.S,
    ).search(text)
    if clause is None:
        return None

    # The complement is stated as "Otherwise, it will resolve to <second>". Recorded as the
    # label rather than as a negated sentence: the venue's own words are what the market pays
    # on, and a paraphrase invites a reader to trust a restatement nobody checked.
    otherwise = re.compile(
        rf'otherwise[^.]{{0,80}}resolve[sd]?\s+(?:to|as)\s+"?{re.escape(second)}"?', re.I | re.S
    ).search(text)
    no_condition = (
        f"the market resolves to {second}"
        if otherwise is not None
        else f"{first} condition is not met"
    )
    return _tidy(clause.group(1)), no_condition, "labelled_binary"


def _tidy(fragment: str) -> str:
    """Collapse whitespace so an extracted clause reads on one line in a journal."""
    return re.sub(r"\s+", " ", fragment).strip(" .,;:")
