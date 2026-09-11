"""Relayer adapter -- transaction infrastructure only.

Implements :class:`~deepflow.ports.execution.RelayerPort`.

The SDK's relayer surface (``polymarket._internal.actions.relayer``) covers
approvals, gasless submission, nonce handling, submit/poll and position
operations. Confirmed on ``AsyncSecureClient``: ``approve_erc20``,
``approve_erc1155_for_all``, ``split_position``, ``merge_positions``,
``merge_multiple_positions``, ``redeem_positions``.

Scope boundary: plumbing only. No probability, EV or sizing logic may read
from this module.
"""

from __future__ import annotations

from collections.abc import Sequence

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.enums import RunMode
from deepflow.core.logging import get_logger

log = get_logger(__name__)


class PolymarketRelayer:
    """Approvals, redemptions and position maintenance."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def ensure_allowances(self) -> bool:
        """Verify (and if needed set) the exchange approvals trading requires.

        Checked at startup: a missing approval surfaces as a run of confusing
        order rejections rather than as an obvious error.
        """
        if self._settings.mode is not RunMode.LIVE:
            log.info("relayer.allowances_skipped", mode=str(self._settings.mode))
            return False
        raise NotImplementedError("PolymarketRelayer.ensure_allowances")

    async def redeem_positions(self, condition_ids: Sequence[str]) -> object:
        """Redeem resolved positions back to collateral."""
        if self._settings.mode is not RunMode.LIVE:
            raise RuntimeError("refusing to redeem: run mode is not LIVE")
        raise NotImplementedError("PolymarketRelayer.redeem_positions")
