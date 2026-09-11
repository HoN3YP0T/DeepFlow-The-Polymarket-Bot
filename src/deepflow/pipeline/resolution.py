"""Resolution rule parsing and validation. Section 4.

The single highest-value safety component in the system. A market can be
liquid, fresh, and mispriced, and still lose the full stake because the payout
condition was not what the title implied -- a "will X happen by date D" market
that resolves NO on a technicality, a source hierarchy that defers to an
authority which never publishes, an ambiguity clause that voids the market.

Rule: the bot never infers the payout condition from the market title. The
YES/NO definitions must be extracted from the resolution text, and a market
whose text cannot be parsed is ``UNPARSEABLE`` -- which is not tradeable.
"""

from __future__ import annotations

from deepflow.core.domain import Market, ResolutionCriteria
from deepflow.core.enums import ResolutionValidity
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: Phrases that make a market's payout conditional on a judgement call rather
#: than an observable fact. Presence forces AMBIGUOUS pending manual review.
AMBIGUITY_MARKERS: tuple[str, ...] = (
    "at the discretion",
    "generally accepted",
    "widely reported",
    "credible sources",
    "may be resolved",
    "consensus of",
)


class ResolutionValidator:
    """Parses resolution rules into a structured, checkable form."""

    def validate(self, market: Market) -> ResolutionCriteria:
        """Parse and classify ``market``'s resolution rules.

        TODO(skeleton): extract YES/NO conditions, deadline and its timezone,
        qualifying and non-qualifying events, the source hierarchy, and the
        ambiguity/cancellation clauses; then decide validity.

        Expected outcomes:
        * VALID              -- YES/NO conditions and a deadline with an
                                explicit timezone, from a named source
        * AMBIGUOUS          -- an ambiguity marker, or YES and NO are not
                                mutually exclusive and exhaustive
        * UNSUPPORTED_SOURCE -- resolves against a source we cannot observe
        * UNPARSEABLE        -- no usable resolution text at all
        """
        raise NotImplementedError("ResolutionValidator.validate")

    @staticmethod
    def not_checked() -> ResolutionCriteria:
        """Default state for a freshly discovered market."""
        return ResolutionCriteria(validity=ResolutionValidity.NOT_CHECKED)
