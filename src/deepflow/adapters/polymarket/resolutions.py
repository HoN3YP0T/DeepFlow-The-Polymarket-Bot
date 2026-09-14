"""Reading how a market actually settled.

The venue publishes this at ``/v2/resolutions``, which the SDK wraps as
``get_resolutions``. It is the authoritative record: per-outcome payouts in USDC
per share, with the resolution's lifecycle status and whether it was disputed.

**Three things here were measured rather than assumed**, because each was a
plausible-looking alternative that would have produced silently wrong outcome data
-- the worst kind, since a calibration curve fitted on inverted outcomes looks like
a well-behaved curve.

1. *The obvious source is wrong.* A closed market's ``outcomes.yes.price`` and
   ``outcomes.no.price`` both read **0** on settled markets, so the natural "price
   is 1 for the winner" reading finds no winner at all -- or, read as a payout,
   scores every outcome as a loss. ``market.resolution`` is no better: its
   ``question_id``, ``uma_resolution_status`` and ``resolved_by`` were all ``None``
   on every settled market sampled.

2. *The payout pair is positional and its alignment needed proof.* ``payouts`` is a
   two-element tuple from the data API, while :attr:`Market.outcomes` is built from
   the SDK's *named* ``yes``/``no`` accessors -- two different services, and nothing
   in either says the orders agree. §22 is in this repo because a plausible
   positional zip returns a binary market's complement, and here that would invert
   the outcome of every sample. Checked against what each token last traded at
   before expiry across 17 settled markets with trades: **17 agreed, 0 disagreed**,
   so ``payouts[i]`` belongs to ``outcomes[i]``. The guard in :meth:`resolve` keeps
   that a checked fact rather than a remembered one.

3. *The batch limit is 20 condition ids*, not a documented larger number, and an
   empty list is rejected outright (``condition_id must be non-empty``) -- the same
   shape as §79's ``symbols=[]``. Both are handled here rather than discovered in a
   loop that retries forever.
"""

from __future__ import annotations

from collections.abc import Sequence

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import Market, MarketResolution, OutcomePayout
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId

log = get_logger(__name__)

#: Condition ids per request. The SDK enforces it (``max_distinct=20``) and raises
#: rather than truncating, so a larger sweep must be chunked here.
MAX_CONDITION_IDS: int = 20


class PolymarketResolutions:
    """Per-outcome payouts for settled markets."""

    def __init__(self, session: PolymarketSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    async def resolve(self, markets: Sequence[Market]) -> tuple[MarketResolution, ...]:
        """Fetch resolutions for ``markets``, omitting those that have not settled.

        Takes markets rather than condition ids because the payouts are positional
        and turning them into token ids needs each market's own outcome order. That
        translation happens here, once, and nothing downstream ever sees an index.

        A market whose payout count does not match its outcome count is **skipped
        and logged**, never truncated or zipped as far as it goes. That mismatch is
        the one shape in which the positional assumption could be wrong, and a
        calibration sample built on a wrong alignment is worse than a missing one:
        it is indistinguishable from evidence that the model is inverted.
        """
        by_condition = {str(market.condition_id): market for market in markets if market.outcomes}
        if not by_condition:
            # The venue rejects an empty id list outright, and a caller with
            # nothing to ask about wants an empty answer, not an exception.
            return ()

        resolved: list[MarketResolution] = []
        ids = list(by_condition)
        for start in range(0, len(ids), MAX_CONDITION_IDS):
            chunk = ids[start : start + MAX_CONDITION_IDS]
            for record in await self._session.public.get_resolutions(condition_ids=chunk):
                market = by_condition.get(str(record.condition_id))
                if market is None:
                    continue
                resolution = self._to_resolution(market, record)
                if resolution is not None:
                    resolved.append(resolution)
        return tuple(resolved)

    def _to_resolution(self, market: Market, record: object) -> MarketResolution | None:
        payouts = getattr(record, "payouts", None)
        if not payouts:
            # Proposed, challenged or under review: a lifecycle stage, not a
            # failure. It will have payouts on a later sweep.
            return None

        if len(payouts) != len(market.outcomes):
            log.warning(
                "resolutions.payout_outcome_mismatch",
                condition_id=str(market.condition_id),
                payouts=len(payouts),
                outcomes=len(market.outcomes),
            )
            return None

        resolved_at = getattr(record, "resolved_at", None) or getattr(
            record, "last_updated_at", None
        )
        if resolved_at is None:
            # Without a settlement time a sample cannot be placed against the
            # prediction that preceded it, which is the whole point of storing it.
            log.warning(
                "resolutions.no_resolved_at",
                condition_id=str(market.condition_id),
            )
            return None

        source = getattr(record, "resolution_source", None)
        return MarketResolution(
            condition_id=ConditionId(str(market.condition_id)),
            payouts=tuple(
                OutcomePayout(token_id=outcome.token_id, payout=payout)
                for outcome, payout in zip(market.outcomes, payouts, strict=True)
            ),
            resolved_at=resolved_at,
            status=str(getattr(record, "status", "") or ""),
            was_disputed=bool(getattr(record, "was_disputed", False)),
            source=str(source) if source is not None else None,
        )
