"""Expected-value engine. Section 16.

Turns a probability estimate and a live book into a net EV figure.

The whole system's objective lives in this module. ``edge`` is the number that
looks impressive; ``net_ev`` is the number that decides. In the 0.85-0.98 band
this bot targets, the gap between them is most of the trade: paying 2c of
spread on a 3c edge leaves 1c, and a 1c edge on a 95c contract is a 1% return
against a 5% chance of losing everything. That arithmetic -- not win rate -- is
what the gates are protecting.

## Units, and the double-count this avoids

Two unit systems meet here and mixing them silently is the easiest way to build a
bot that trades confidently at a loss:

* ``market_probability``, ``model_probability``, ``edge`` and ``net_ev`` are in
  **probability units** -- per-share payoff, where a contract pays 1.
* every term in :class:`CostBreakdown` is in **basis points of notional**, so the
  terms sum.

Converting between them needs the price, because notional per share *is* the price:
a cost of ``b`` bps is ``b / 10_000 * price`` per share of payoff. That conversion
happens in exactly one place, :meth:`_net_ev`.

``market_probability`` is the **ask we would cross**, not the mid. That choice makes
the edge honest -- a mid-based edge is optimistic by half the spread on every single
trade -- but it also means **the cost of crossing the spread is already inside the
edge**. So ``spread_cost_bps`` stays zero and says why: subtracting it again would
charge the same cent twice and reject trades that are genuinely profitable. The
remaining cost terms are the ones the edge does *not* already contain: the venue's
fee, the slippage from walking past the touch, and the buffer for our own
uncertainty.
"""

from __future__ import annotations

from decimal import Decimal

from deepflow.adapters.polymarket import venue
from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import (
    CostBreakdown,
    EvAssessment,
    Market,
    MarketSnapshot,
    OrderBook,
    ProbabilityEstimate,
)
from deepflow.core.enums import DataQuality, OrderSide
from deepflow.core.logging import get_logger
from deepflow.core.types import ONE, ZERO, ClobTokenId

log = get_logger(__name__)

#: Data quality -> the confidence multiplier it earns. ``INCONSISTENT`` is zero
#: rather than small: an inconsistent book means our folded state is wrong, and a
#: confident number derived from a wrong book is the failure mode this whole
#: pipeline is arranged to prevent.
_QUALITY_CONFIDENCE: dict[DataQuality, Decimal] = {
    DataQuality.FRESH: ONE,
    DataQuality.DEGRADED: Decimal("0.6"),
    DataQuality.STALE: Decimal("0.2"),
    DataQuality.INCONSISTENT: ZERO,
}


class EvEngine:
    """Computes edge, costs and net expected value."""

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self._thresholds = thresholds or Thresholds()

    def assess(
        self,
        *,
        estimate: ProbabilityEstimate,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
        size_shares: Decimal,
        market: Market | None = None,
        max_spread_bps: Decimal | None = None,
    ) -> EvAssessment | None:
        """Assess a prospective trade.

        ``None`` when the book cannot support a priced assessment -- no book, no
        ask, or not enough depth to fill ``size_shares``. That is deliberately the
        same answer as "cannot fill": an EV figure derived from a partial walk
        would understate the cost of entry precisely when the book is too thin to
        enter, and the caller must be unable to tell the difference between "no
        opinion" and "bad opinion" here, because both mean do not trade.

        ``market`` is optional only so the fee term can be *omitted honestly*: with
        no market the schedule is unknown, and :meth:`fee_bps` returns zero. A
        caller that wants a tradeable number must pass the market -- see
        :meth:`_costs` on why a zero fee is not a small error.

        ``max_spread_bps`` comes from the *strategy's* thresholds, because how wide
        a book is too wide is a strategy judgement and this engine has no business
        inventing one. Omitted, the spread term drops out of confidence rather than
        defaulting to a limit nobody chose.
        """
        book = snapshot.book_for(token_id)
        if book is None or book.best_ask is None or size_shares <= 0:
            return None

        market_probability = self._market_probability(snapshot, token_id)
        if market_probability is None:
            return None

        vwap = book.vwap_to_fill(size_shares, side=OrderSide.BUY)
        if vwap is None:
            # Not enough depth. See the docstring: this is a refusal, not a price.
            log.info(
                "ev.insufficient_depth",
                token_id=str(token_id),
                size_shares=str(size_shares),
            )
            return None

        edge = estimate.calibrated_probability - market_probability
        costs = self._costs(
            estimate=estimate,
            snapshot=snapshot,
            token_id=token_id,
            size_shares=size_shares,
            market=market,
        )
        fill_probability = self._fill_probability(book, size_shares)
        net_ev = self._net_ev(
            edge=edge,
            fill_probability=fill_probability,
            costs=costs,
            price=market_probability,
        )

        return EvAssessment(
            token_id=token_id,
            market_probability=market_probability,
            model_probability=estimate.calibrated_probability,
            edge=edge,
            costs=costs,
            fill_probability=fill_probability,
            net_ev=net_ev,
            confidence=self._confidence(
                estimate=estimate,
                snapshot=snapshot,
                book=book,
                max_spread_bps=max_spread_bps,
            ),
        )

    def _market_probability(
        self, snapshot: MarketSnapshot, token_id: ClobTokenId
    ) -> Decimal | None:
        """Implied probability at the price we would actually transact.

        The best ask, because that is what a buy crosses. Not the mid: the mid is a
        price nobody is offering, and using it books half the spread as edge on
        every trade. See the module docstring on why this makes
        ``spread_cost_bps`` zero rather than double-counted.
        """
        book = snapshot.book_for(token_id)
        return None if book is None else book.best_ask

    @staticmethod
    def _net_ev(
        *,
        edge: Decimal,
        fill_probability: Decimal,
        costs: CostBreakdown,
        price: Decimal,
    ) -> Decimal:
        """Per-share expected value in probability units, after every cost.

        The only place the two unit systems meet. Costs are bps of notional and
        notional per share is the price, so ``total_bps / 10_000 * price`` is the
        cost in the same per-share payoff units as the edge.

        Costs are **not** scaled by fill probability. A partial fill pays
        proportionally less fee and less slippage, but it also captures
        proportionally less edge, so scaling both changes nothing about the sign --
        and the sign is what this number is for. Leaving costs unscaled is the
        conservative reading of the two.
        """
        cost_per_share = (costs.total_bps / Decimal(10_000)) * price
        return edge * fill_probability - cost_per_share

    def _fill_probability(self, book: OrderBook, size_shares: Decimal) -> Decimal:
        """Probability the intended size actually transacts.

        Crossing the spread for a size the book can already cover is as close to
        certain as this system gets, so this returns 1 for a fillable marketable
        order and 0 when the depth is not there. It is deliberately not a
        queue-position model: resting-order fill probability belongs with the
        execution style that rests orders, and inventing a number here would put a
        guess inside the one calculation that decides whether to trade.

        The honest limitation: between assessment and submission the book can move,
        so 1 means "fillable on the book we measured", not "guaranteed". The gate's
        freshness check is what keeps that window short.
        """
        return ONE if book.vwap_to_fill(size_shares, side=OrderSide.BUY) is not None else ZERO

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
        market: Market | None,
    ) -> CostBreakdown:
        """Full cost stack, in basis points of notional.

        The uncertainty buffer is a real cost, not a safety flourish: acting on
        a probability we hold loosely is worth less than acting on one we hold
        tightly, and pricing that difference is what stops the system from
        trading its own noise. It scales with the estimate's own 1-sigma band, so
        a model that admits it does not know is charged for saying so.

        ``spread_cost_bps`` is zero by construction -- crossing the spread is
        already inside the edge, because ``market_probability`` is the ask. The
        field stays in :class:`CostBreakdown` because a future resting-order path
        does *not* cross the spread and will need to attribute the cost
        differently; leaving it present and zero is a smaller lie than removing it.
        """
        book = snapshot.book_for(token_id)
        if book is None or book.best_ask is None:
            return CostBreakdown()

        price = book.best_ask
        vwap = book.vwap_to_fill(size_shares, side=OrderSide.BUY)
        slippage_bps = ZERO if vwap is None else (vwap - price) / price * Decimal(10_000)

        return CostBreakdown(
            # The one term that is arithmetic rather than estimate, and therefore
            # the one that must never sit at zero by accident.
            fee_bps=ZERO if market is None else self.fee_bps(market, price=price),
            spread_cost_bps=ZERO,
            slippage_bps=slippage_bps,
            uncertainty_buffer_bps=self._uncertainty_bps(estimate, price=price),
        )

    def _uncertainty_bps(self, estimate: ProbabilityEstimate, *, price: Decimal) -> Decimal:
        """Charge for the width of our own belief, in bps of notional.

        ``uncertainty`` is a 1-sigma band in probability units, so it converts the
        same way any payoff figure does: divide by the price to express it against
        notional. Multiplied by a configured coefficient so the appetite is a
        setting rather than a constant buried here.
        """
        if price <= 0:
            return ZERO
        coefficient = self._thresholds.btc_5m.uncertainty_buffer
        return (estimate.uncertainty * coefficient / price) * Decimal(10_000)

    def _confidence(
        self,
        *,
        estimate: ProbabilityEstimate,
        snapshot: MarketSnapshot,
        book: OrderBook,
        max_spread_bps: Decimal | None,
    ) -> int:
        """Confidence 0-100 in this assessment -- not in the direction of the trade.

        Three inputs, each able to veto on its own by driving the product toward
        zero: how tightly the model holds its estimate, how fresh the data is, and
        how wide the spread is against the strategy's own limit. Combined
        multiplicatively rather than averaged, because these are not interchangeable
        virtues -- a stale feed is not compensated for by a tight spread, and an
        average would quietly allow exactly that trade.

        ``INCONSISTENT`` data therefore yields confidence 0 outright. An
        inconsistent book means our folded state disagrees with the venue's, and a
        number computed from a book we know to be wrong should not be merely
        discounted.
        """
        certainty = ONE - min(estimate.uncertainty, ONE)
        freshness = _QUALITY_CONFIDENCE.get(snapshot.quality, ZERO)

        tightness = ONE
        spread = book.spread
        if max_spread_bps is not None and spread is not None and book.best_ask:
            if max_spread_bps <= 0:
                tightness = ZERO
            else:
                spread_bps = spread / book.best_ask * Decimal(10_000)
                tightness = max(ZERO, ONE - (spread_bps / max_spread_bps))

        product = certainty * freshness * tightness
        return int(max(ZERO, min(ONE, product)) * 100)
