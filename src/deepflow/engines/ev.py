"""Expected-value engine. Section 16.

Turns a probability estimate and a live book into a net EV figure.

The whole system's objective lives in this module. ``edge`` is the number that
looks impressive; ``net_ev`` is the number that decides. In the 0.85-0.98 band
this bot targets, the gap between them is most of the trade: paying 2c of
spread on a 3c edge leaves 1c, and a 1c edge on a 95c contract is a 1% return
against a 5% chance of losing everything. That arithmetic -- not win rate -- is
what the gates are protecting.
"""

from __future__ import annotations

from decimal import Decimal

from deepflow.adapters.polymarket import venue
from deepflow.core.domain import (
    CostBreakdown,
    EvAssessment,
    Market,
    MarketSnapshot,
    ProbabilityEstimate,
)
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId

log = get_logger(__name__)


class EvEngine:
    """Computes edge, costs and net expected value."""

    def assess(
        self,
        *,
        estimate: ProbabilityEstimate,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
        size_shares: Decimal,
    ) -> EvAssessment | None:
        """Assess a prospective trade.

        TODO(skeleton):
        1. market probability from the book -- the price we would actually pay
           (the ask we cross), not the mid. Mid-based edge is optimistic by
           half the spread on every single trade.
        2. ``edge = calibrated_probability - market_probability``
        3. costs: fees, spread paid, slippage from walking the book for
           ``size_shares``, and an uncertainty buffer scaled by the estimate's
           own uncertainty
        4. fill probability -- a resting limit that never fills has no EV, and
           a marketable limit that only partially fills has less than modelled
        5. ``net_ev = edge * fill_probability - total_costs``
        6. confidence 0-100 from estimate uncertainty, data quality, liquidity
           and model agreement

        Returns ``None`` when the book cannot support a priced assessment.
        """
        raise NotImplementedError("EvEngine.assess")

    def _market_probability(
        self, snapshot: MarketSnapshot, token_id: ClobTokenId
    ) -> Decimal | None:
        """Implied probability at the price we would actually transact."""
        raise NotImplementedError("EvEngine._market_probability")

    @staticmethod
    def fee_bps(market: Market, *, price: Decimal) -> Decimal:
        """Taker fee in bps of notional for a fill at ``price``.

        Implemented rather than left to the caller because it is arithmetic the
        venue fixes, and because the sign of the mistake is always the same: a
        system that omits it books an edge it does not have. Reads the market's
        own schedule; a market with fees enabled but no schedule attached falls
        back to nothing, which is the one case worth flagging upstream rather
        than guessing a rate for.

        Makers pay nothing, so a resting (``post_only``) intent carries no fee
        term at all -- the difference is a whole cost line, not a discount.
        """
        if not market.fees_enabled or market.fee_schedule is None:
            return Decimal(0)
        schedule = market.fee_schedule
        return venue.taker_fee_bps(price=price, rate=schedule.rate, exponent=schedule.exponent)

    def _costs(
        self,
        *,
        estimate: ProbabilityEstimate,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
        size_shares: Decimal,
    ) -> CostBreakdown:
        """Full cost stack.

        The uncertainty buffer is a real cost, not a safety flourish: acting on
        a probability we hold loosely is worth less than acting on one we hold
        tightly, and pricing that difference is what stops the system from
        trading its own noise.

        TODO(skeleton): populate ``fee_bps`` from :meth:`fee_bps` at the price
        actually crossed, then spread, slippage and the buffer. The fee is the
        only term here that is not an estimate, so it is the one that must never
        be left at zero.
        """
        raise NotImplementedError("EvEngine._costs")
