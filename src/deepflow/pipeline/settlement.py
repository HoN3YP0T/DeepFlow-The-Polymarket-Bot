"""Recording how markets settled, so predictions can eventually be scored.

This is the half of calibration that was missing. ``_calibrate`` returned its input
unchanged for the life of this repo, and the reason was not that the fit was hard:
the ``signals`` table stored a model probability, and **nothing in the schema had
ever recorded how a market resolved**. Half of every (prediction, outcome) pair was
being thrown away, so no amount of running would have produced a curve.

The loop is deliberately dull. It asks the database which markets we predicted on
have ended without a stored outcome, asks the venue about them in batches, and
writes what comes back. Nothing here decides anything.

**Why it is scoped to markets we predicted on.** The venue settles thousands of
markets we never looked at, the resolutions endpoint takes 20 condition ids per
request, and a sample can only ever be built where a prediction already exists. A
sweep over everything closed would spend the entire request budget on rows no fit
will reference.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.adapters.polymarket.resolutions import MAX_CONDITION_IDS, PolymarketResolutions
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import Market
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: How often to look for newly settled markets.
#:
#: Five minutes. Settlement is not time-critical -- nothing trades on it, and a
#: sample is worth the same whether it lands now or in an hour -- while the endpoint
#: is capped at 20 ids per request, so a tight loop buys nothing and spends quota
#: that the discovery sweep needs.
POLL_SECONDS: float = 300.0

#: Markets to ask about per pass, across several requests.
#:
#: A 5-minute crypto cadence across eight assets settles 96 markets an hour, so a
#: pass has to clear more than one request's worth or the backlog outruns the loop.
BATCH = MAX_CONDITION_IDS * 5


class SettlementRecorder:
    """Finds ended markets we predicted on and stores their payouts."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        resolutions: PolymarketResolutions,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._sessions = sessions
        self._resolutions = resolutions
        self._clock = clock or SystemClock()
        self.recorded = 0
        self.pending = 0

    async def run_once(self) -> int:
        """One pass. Returns how many markets were newly resolved."""
        async with self._sessions() as session:
            uow = SqlUnitOfWork(session, self._clock)
            condition_ids = await uow.resolutions.unresolved_condition_ids(limit=BATCH)
            self.pending = len(condition_ids)
            if not condition_ids:
                return 0

            markets: list[Market] = []
            for condition_id in condition_ids:
                market = await uow.markets.get(condition_id)
                # A market we predicted on but never persisted cannot be aligned:
                # the payout pair is positional and the outcome order is what makes
                # it meaningful. Skipped rather than guessed.
                if market is not None and market.outcomes:
                    markets.append(market)

            resolved = await self._resolutions.resolve(markets)
            for resolution in resolved:
                await uow.resolutions.upsert(resolution)
            await uow.commit()

        self.recorded += len(resolved)
        if resolved:
            log.info(
                "settlement.recorded",
                markets=len(resolved),
                asked=len(markets),
                pending=self.pending,
            )
        return len(resolved)

    async def run_forever(self) -> None:
        """Poll until cancelled.

        A failed pass is logged and retried on the next tick rather than ending the
        task. Settlement is history: losing a pass costs a delay in scoring, and
        taking the process down over it would cost the trading loop for nothing.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("settlement.pass_failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)
