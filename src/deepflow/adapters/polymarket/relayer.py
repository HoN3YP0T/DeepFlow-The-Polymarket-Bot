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

import inspect
from collections.abc import Sequence
from typing import Any

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.enums import RunMode
from deepflow.core.errors import ConfigurationError, PolymarketApiError
from deepflow.core.logging import get_logger

log = get_logger(__name__)


class PolymarketRelayer:
    """Approvals, redemptions and position maintenance."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def ensure_allowances(self) -> bool:
        """Verify, and if needed grant, the four approvals trading requires.

        **Unverified against the venue.** The account these credentials belong to is
        unfunded and unapproved, and granting approvals is a write, so no branch below
        has run for real.

        Uses ``setup_trading_approvals``, which reads the current state and submits only
        what is missing -- idempotent, and it covers all four approvals at once.
        Hand-rolling them from ``approve_erc20`` is where the neg-risk half gets
        forgotten, and the symptom is every order on a multi-outcome event being
        rejected while standard markets work fine.

        Returns whether the wallet is fully approved **afterwards**, established by
        re-reading the state rather than by trusting the call. That is not
        belt-and-braces: ``setup_trading_approvals`` returns a *deprecated*
        compatibility handle whose ``wait()`` returns immediately, so the handle proves
        nothing about whether anything landed on chain. The approval state is the only
        answer that means anything.

        Called at startup because a missing approval otherwise surfaces as a run of
        confusing ``not enough balance / allowance`` rejections rather than as one
        legible error.
        """
        if self._settings.mode is not RunMode.LIVE:
            log.info("relayer.allowances_skipped", mode=str(self._settings.mode))
            return False

        client = self._secure("check trading approvals")
        try:
            before = await client.get_trading_approvals_state()
            if getattr(before, "is_fully_approved", False):
                log.info("relayer.allowances_already_granted")
                return True

            log.info("relayer.granting_allowances", missing=_describe_missing(before))
            await client.setup_trading_approvals()
            after = await client.get_trading_approvals_state()
        except Exception as exc:
            raise PolymarketApiError(f"ensure_allowances: {type(exc).__name__}: {exc}") from exc

        granted = bool(getattr(after, "is_fully_approved", False))
        if not granted:
            # Deliberately returned rather than raised: the caller decides whether to
            # start, and this reads as "not ready" rather than as an adapter fault. What
            # must not happen is reporting success -- that turns one legible refusal into
            # a rejection on every order placed afterwards.
            log.error("relayer.allowances_incomplete", missing=_describe_missing(after))
        return granted

    async def redeem_positions(self, condition_ids: Sequence[str]) -> Sequence[str]:
        """Redeem resolved positions back to collateral, one condition at a time.

        **Unverified against the venue**, for the same reason as above: this is a write
        and there are no positions to redeem.

        One call per condition id, because the SDK accepts **exactly one** of
        ``condition_id`` / ``market_id`` / ``position_id`` and raises on more -- there
        is no batch form. Each redemption is awaited to a terminal outcome before the
        next starts: they are independent on-chain transactions, and firing them
        concurrently would put several in flight against one nonce.

        A failure on one condition does not abandon the rest. Returns the ids that
        redeemed, so the caller can tell a partial result from a total one; the
        failures are logged with their reason. Redemption is what closes the loop on
        realised P&L -- until it runs the capital is not available to size the next
        trade, which is what ``reserved_capital_fraction`` is covering for.
        """
        if self._settings.mode is not RunMode.LIVE:
            raise RuntimeError("refusing to redeem: run mode is not LIVE")

        client = self._secure("redeem positions")
        redeemed: list[str] = []
        for condition_id in condition_ids:
            try:
                handle = await client.redeem_positions(condition_id=str(condition_id))
                await _settle(handle)
            except Exception as exc:
                log.error(
                    "relayer.redemption_failed",
                    condition_id=str(condition_id),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            redeemed.append(str(condition_id))

        log.info(
            "relayer.redeemed",
            redeemed=len(redeemed),
            failed=len(tuple(condition_ids)) - len(redeemed),
        )
        return tuple(redeemed)

    def _secure(self, action: str) -> Any:
        """The authenticated client, or a failure naming what is missing.

        Gasless submission needs a relayer key *and* the signer, not a private key
        alone: a signer-only client can sign orders but cannot submit an approval
        batch. Both halves of the relayer key are required because
        ``polymarket.auth.RelayerApiKey`` cannot be constructed without an address.
        """
        client = getattr(self._session, "secure", None)
        if client is None:
            raise ConfigurationError(
                f"cannot {action}: no authenticated client. Set "
                "DEEPFLOW_POLYMARKET__PRIVATE_KEY and DEEPFLOW_POLYMARKET__WALLET_ADDRESS."
            )
        if not self._settings.polymarket.can_submit_relayer_transactions:
            raise ConfigurationError(
                f"cannot {action}: gasless submission needs "
                "DEEPFLOW_POLYMARKET__RELAYER_API_KEY and "
                "DEEPFLOW_POLYMARKET__RELAYER_API_KEY_ADDRESS. Signing alone places "
                "orders but cannot grant approvals or redeem positions."
            )
        return client


def _describe_missing(state: object) -> str:
    """The missing approvals, as a short log field.

    Named separately because the shape is two tuples of dataclasses
    (``MissingTradingApprovals.erc20`` / ``.erc1155``) and a bare repr of it in a log
    line is unreadable at the moment someone needs it most.
    """
    missing = getattr(state, "missing", None)
    erc20 = tuple(getattr(missing, "erc20", ()) or ())
    erc1155 = tuple(getattr(missing, "erc1155", ()) or ())
    spenders = [str(getattr(item, "spender", "?")) for item in erc20]
    operators = [str(getattr(item, "operator", "?")) for item in erc1155]
    return f"erc20->{spenders} erc1155->{operators}"


async def _settle(handle: object) -> None:
    """Await a transaction handle's terminal outcome, when it has one.

    ``redeem_positions`` returns a real ``TransactionHandle`` whose ``wait()`` is
    meaningful, unlike the deprecated handle ``setup_trading_approvals`` returns. Both
    shapes are tolerated because the distinction is the SDK's and may move.
    """
    wait = getattr(handle, "wait", None)
    if wait is None:
        return
    result = wait()
    if inspect.isawaitable(result):
        await result
