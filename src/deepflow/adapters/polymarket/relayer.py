"""Relayer adapter -- transaction infrastructure only.

Implements :class:`~deepflow.ports.execution.RelayerPort`.

The SDK's relayer surface (``polymarket._internal.actions.relayer``) covers
approvals, gasless submission, nonce handling, submit/poll and position
operations. Confirmed on ``AsyncSecureClient``: ``setup_trading_approvals``,
``approve_erc20``, ``approve_erc1155_for_all``, ``split_position``,
``merge_positions``, ``merge_multiple_positions``, ``redeem_positions``.

``setup_trading_approvals()`` is the one to call: it checks what is already
approved and submits only what is missing, so it is idempotent, and it covers
all four approvals at once. Hand-rolling them from ``approve_erc20`` is where
the neg-risk half gets forgotten, and the symptom is that every order on a
multi-outcome event is rejected while standard markets work fine.

Four approvals are required, not two -- pUSD and Conditional Tokens, each
against *both* the standard and the neg-risk exchange:

    pUSD               -> approve(CTF_EXCHANGE, max)
    pUSD               -> approve(NEG_RISK_CTF_EXCHANGE, max)
    ConditionalTokens  -> setApprovalForAll(CTF_EXCHANGE, true)
    ConditionalTokens  -> setApprovalForAll(NEG_RISK_CTF_EXCHANGE, true)

Collateral is **pUSD** (``0xC011a7...2DFB``), an ERC-20 wrapper over USDC with 6
decimals -- not USDC.e directly. Approving the wrong token leaves the allowance
check passing on chain and the order failing at the venue.

Gasless submission needs a Relayer or Builder API key alongside the signer, not
just a private key, and the account wallet address. A signer-only client can
sign orders but cannot submit an approval batch.

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

        TODO(skeleton): prefer ``secure.setup_trading_approvals()``, which is
        idempotent and covers all four approvals. Then refresh the CLOB's
        allowance cache (``/balance-allowance/update``) for COLLATERAL, and for
        each conditional token before its first SELL -- the venue caches
        allowances, so an on-chain approval that has not been synced still reads
        as missing.

        Checked at startup: a missing approval surfaces as a run of confusing
        order rejections (``not enough balance / allowance``) rather than as an
        obvious error.
        """
        if self._settings.mode is not RunMode.LIVE:
            log.info("relayer.allowances_skipped", mode=str(self._settings.mode))
            return False
        raise NotImplementedError("PolymarketRelayer.ensure_allowances")

    async def redeem_positions(self, condition_ids: Sequence[str]) -> object:
        """Redeem resolved positions back to collateral.

        Redemption is what closes the loop on realised P&L, and until it runs the
        capital is not available to size the next trade -- which is what
        ``reserved_capital_fraction`` is covering for. Neg-risk markets redeem
        through their own adapter path, so the flag travels with the position.
        """
        if self._settings.mode is not RunMode.LIVE:
            raise RuntimeError("refusing to redeem: run mode is not LIVE")
        raise NotImplementedError("PolymarketRelayer.redeem_positions")
