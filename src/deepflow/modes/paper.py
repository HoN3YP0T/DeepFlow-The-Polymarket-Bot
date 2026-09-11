"""PAPER and SHADOW executors."""

from __future__ import annotations

from deepflow.core.domain import MarketSnapshot, OrderIntent, OrderRecord
from deepflow.core.enums import RunMode
from deepflow.core.logging import get_logger
from deepflow.modes.base import ModeExecutor

log = get_logger(__name__)


class PaperExecutor(ModeExecutor):
    """Simulates fills against the live book.

    The simulation must be pessimistic to be useful. Filling at the mid, or
    filling the whole size at the touch, produces paper results the live system
    cannot reproduce -- and the gap shows up as a strategy that was never
    profitable rather than as a bug.

    So: walk the book for the real size, assume we are behind the existing
    queue at our price level, and model partial fills rather than assuming
    completion.
    """

    mode = RunMode.PAPER

    def __init__(self) -> None:
        self._snapshots: dict[str, MarketSnapshot] = {}

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """TODO(skeleton): simulate against the current book -- walk depth for
        the intended size, apply queue position for a resting limit, produce a
        partial fill where depth is insufficient, and charge the same fee
        schedule the live path would pay."""
        raise NotImplementedError("PaperExecutor.submit")


class ShadowExecutor(ModeExecutor):
    """Builds and validates a real order, then declines to send it.

    The last rehearsal before live. Everything a LIVE run would do happens --
    signing, formatting, venue-side validation of everything checkable without
    submitting -- and the transmission is suppressed. It catches the failures
    paper trading structurally cannot: malformed orders, tick-size violations,
    missing approvals, signature problems.
    """

    mode = RunMode.SHADOW

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """TODO(skeleton): build and sign the order exactly as LIVE would,
        record what would have been sent, and return a simulated record."""
        raise NotImplementedError("ShadowExecutor.submit")
