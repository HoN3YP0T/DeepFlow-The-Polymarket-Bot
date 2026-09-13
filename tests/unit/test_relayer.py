"""Relayer adapter: approvals and redemptions.

**Unverified against the venue.** The account is unfunded and unapproved and both
operations are writes, so nothing here has run for real. These tests pin the decisions,
not the liveness.

The decision each test defends, in one line: never report approvals granted on the
strength of a call that returns a handle proving nothing, and never abandon the
remaining redemptions because one failed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket.relayer import PolymarketRelayer
from deepflow.config.settings import Settings
from deepflow.core.enums import RunMode
from deepflow.core.errors import ConfigurationError, PolymarketApiError

WALLET = "0x43c3F958321EAE86333fE97993E58dCB5B9B7B49"


def _settings(*, mode: RunMode = RunMode.LIVE, relayer: bool = True) -> Settings:
    polymarket: dict[str, str] = {
        "private_key": "0x" + "1" * 64,
        "wallet_address": WALLET,
    }
    if relayer:
        polymarket["relayer_api_key"] = "k"
        polymarket["relayer_api_key_address"] = WALLET
    live = mode is RunMode.LIVE
    return Settings(
        _env_file=None,
        mode=mode,
        live_trading_confirmed=live,
        live_trading_ack="I ACCEPT REAL CAPITAL RISK" if live else "",
        api={"jwt_secret": "x" * 32},
        polymarket=polymarket,
    )


def _state(*, approved: bool, erc20: tuple[Any, ...] = (), erc1155: tuple[Any, ...] = ()) -> Any:
    return SimpleNamespace(
        is_fully_approved=approved,
        missing=SimpleNamespace(erc20=erc20, erc1155=erc1155),
    )


class _Client:
    def __init__(
        self,
        *,
        states: list[Any] | None = None,
        setup_raises: Exception | None = None,
        redeem_failures: set[str] | None = None,
        handle: Any = None,
    ) -> None:
        self.states = states or [_state(approved=True)]
        self.setup_raises = setup_raises
        self.redeem_failures = redeem_failures or set()
        self.handle = handle
        self.setup_calls = 0
        self.redeemed: list[str] = []

    async def get_trading_approvals_state(self) -> Any:
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def setup_trading_approvals(self) -> Any:
        if self.setup_raises:
            raise self.setup_raises
        self.setup_calls += 1
        # The deprecated compatibility handle: wait() returns immediately and proves
        # nothing about what landed on chain.
        return SimpleNamespace(wait=lambda: None)

    async def redeem_positions(self, *, condition_id: str) -> Any:
        if condition_id in self.redeem_failures:
            raise RuntimeError(f"redemption reverted for {condition_id}")
        self.redeemed.append(condition_id)
        return self.handle if self.handle is not None else SimpleNamespace(wait=lambda: None)


def _relayer(client: _Client | None, **kwargs: Any) -> PolymarketRelayer:
    return PolymarketRelayer(SimpleNamespace(secure=client), _settings(**kwargs))  # type: ignore[arg-type]


# --- Mode gating ----------------------------------------------------------
@pytest.mark.asyncio
async def test_approvals_are_skipped_outside_live() -> None:
    """Not an error: a PAPER run has nothing to approve, and returning False keeps the
    caller's startup check honest rather than claiming a state we did not check."""
    relayer = _relayer(_Client(), mode=RunMode.PAPER)
    assert await relayer.ensure_allowances() is False


@pytest.mark.asyncio
async def test_redemption_is_refused_outside_live() -> None:
    with pytest.raises(RuntimeError, match="not LIVE"):
        await _relayer(_Client(), mode=RunMode.PAPER).redeem_positions(["0xabc"])


# --- Credentials ----------------------------------------------------------
@pytest.mark.asyncio
async def test_a_signer_without_a_relayer_key_cannot_approve() -> None:
    """Signing places orders but cannot submit an approval batch, and the message has
    to say so — otherwise the symptom is every order rejected for allowance."""
    with pytest.raises(ConfigurationError, match="RELAYER_API_KEY"):
        await _relayer(_Client(), relayer=False).ensure_allowances()


# --- Approvals ------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_already_approved_wallet_submits_nothing() -> None:
    client = _Client(states=[_state(approved=True)])
    assert await _relayer(client).ensure_allowances() is True
    assert client.setup_calls == 0


@pytest.mark.asyncio
async def test_success_is_established_by_re_reading_the_state() -> None:
    """`setup_trading_approvals` returns a deprecated handle whose wait() returns
    immediately, so it proves nothing. Only the approval state is an answer."""
    missing = _state(approved=False, erc20=(SimpleNamespace(spender="0xexchange"),))
    client = _Client(states=[missing, _state(approved=True)])
    assert await _relayer(client).ensure_allowances() is True
    assert client.setup_calls == 1


@pytest.mark.asyncio
async def test_an_incomplete_grant_is_reported_as_not_ready_not_as_success() -> None:
    """Reporting success here turns one legible refusal into a rejection on every
    order placed afterwards."""
    missing = _state(approved=False, erc1155=(SimpleNamespace(operator="0xnegrisk"),))
    client = _Client(states=[missing, missing])
    assert await _relayer(client).ensure_allowances() is False


@pytest.mark.asyncio
async def test_an_approval_failure_is_an_adapter_error() -> None:
    client = _Client(states=[_state(approved=False)], setup_raises=RuntimeError("rpc down"))
    with pytest.raises(PolymarketApiError, match="ensure_allowances"):
        await _relayer(client).ensure_allowances()


# --- Redemptions ----------------------------------------------------------
@pytest.mark.asyncio
async def test_each_condition_is_redeemed_in_its_own_call() -> None:
    """The SDK accepts exactly one of condition_id/market_id/position_id and raises on
    more; there is no batch form."""
    client = _Client()
    redeemed = await _relayer(client).redeem_positions(["0xa", "0xb"])
    assert client.redeemed == ["0xa", "0xb"]
    assert redeemed == ("0xa", "0xb")


@pytest.mark.asyncio
async def test_one_failed_redemption_does_not_abandon_the_rest() -> None:
    """Each is an independent on-chain transaction. Capital tied up in the others is
    capital the sizer cannot deploy, so a single revert must not stop the sweep."""
    client = _Client(redeem_failures={"0xb"})
    redeemed = await _relayer(client).redeem_positions(["0xa", "0xb", "0xc"])
    assert redeemed == ("0xa", "0xc")


@pytest.mark.asyncio
async def test_an_awaitable_handle_is_awaited() -> None:
    """`redeem_positions` returns a real TransactionHandle whose wait() is meaningful,
    unlike the approvals handle. Both shapes are tolerated because the distinction is
    the SDK's and may move."""
    waited: list[bool] = []

    async def _wait() -> None:
        waited.append(True)

    client = _Client(handle=SimpleNamespace(wait=_wait))
    await _relayer(client).redeem_positions(["0xa"])
    assert waited == [True]
