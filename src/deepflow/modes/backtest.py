"""BACKTEST mode. Sections 25-26.

Replays historical snapshots through the unmodified pipeline.

Two rules the harness exists to enforce, both of which are easy to violate by
accident and both of which make a backtest worthless:

* **No lookahead.** An engine may only see data with a timestamp at or before
  the simulated clock. This is why :class:`~deepflow.core.clock.Clock` is
  injected everywhere rather than ``datetime.now()`` being called inline.
* **Realistic fills.** Same pessimistic book-walking model as PAPER. A
  backtest that fills at the mid will show an edge that the spread alone
  consumes.
"""

from __future__ import annotations

from datetime import datetime

from deepflow.core.clock import ManualClock
from deepflow.core.domain import OrderIntent, OrderRecord
from deepflow.core.enums import RunMode
from deepflow.core.logging import get_logger
from deepflow.modes.base import ModeExecutor

log = get_logger(__name__)


class BacktestExecutor(ModeExecutor):
    """Fills orders against historical book snapshots."""

    mode = RunMode.BACKTEST

    def __init__(self, clock: ManualClock) -> None:
        self._clock = clock

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        raise NotImplementedError("BacktestExecutor.submit")


class BacktestRunner:
    """Drives a replay over a historical window."""

    def __init__(self, *, start: datetime, end: datetime) -> None:
        self._clock = ManualClock(start)
        self._end = end

    async def run(self) -> object:
        """TODO(skeleton): step the clock through stored snapshots, feed the
        pipeline, and collect results -- realized P&L, max drawdown, hit rate,
        realized-vs-modelled EV, and a calibration curve.

        The calibration curve is the headline output, not the P&L. It answers
        whether the probability estimates were honest, which is what determines
        whether the strategy survives a different sample.
        """
        raise NotImplementedError("BacktestRunner.run")
