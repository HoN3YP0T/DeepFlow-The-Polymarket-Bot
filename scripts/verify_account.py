"""Read-only verification of the credentialed path against the live venue.

    .venv/bin/python scripts/verify_account.py

Needs credentials in ``.env`` (see ``.env.example``). **It places no orders and
cancels nothing.** Every call it makes is a read; the four write methods on the
execution adapter (``submit``, ``cancel``, ``cancel_all``,
``start_order_heartbeat``) are deliberately not exercised and remain unverified.

Why a script rather than a test: none of this is assertable offline. What it
establishes is that the credentials produce a *working* authenticated client and
that the account is in a state from which trading would be possible at all --
which is a different question from whether the code compiles, and one that four
retracted findings in ``docs/POLYMARKET-API-CONFORMANCE.md`` say must be asked
of the venue rather than of a typed model.

It prints no secrets. The private key, API secret and passphrase are read via
the settings loader and never rendered; the wallet address is masked, since it
is the one credential-adjacent value that is useful to eyeball.

The two checks that exist because of a specific failure mode:

* **A zero allowance with a healthy balance** is the state where every order is
  rejected for "not enough balance / allowance" while the account plainly has
  funds. It is reported as a distinct failure, not folded into the balance line.
* **Signature type decides which account is read.** A deposit wallet configured
  as ``eoa`` reads a *different* account's balance -- almost certainly zero,
  which looks like an empty account rather than a misconfiguration. A zero
  balance therefore prints the configured wallet type next to it.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from deepflow.adapters.polymarket.execution import PolymarketExecution
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import get_settings
from deepflow.core.errors import ConfigurationError


def _mask(address: str | None) -> str:
    """Enough of an address to recognise, not enough to be a leak in a log."""
    if not address:
        return "(not set)"
    return f"{address[:6]}...{address[-4:]}" if len(address) > 12 else "(short)"


async def main() -> int:
    settings = get_settings()
    poly = settings.polymarket

    print("=== configuration ===")
    print(f"  run mode:     {settings.mode}")
    print(f"  wallet:       {_mask(poly.account_wallet)}")
    print(f"  wallet type:  {poly.wallet_type}")
    print(f"  private key:  {'set' if poly.private_key else 'MISSING'}")
    print(f"  relayer key:  {'set' if poly.relayer_api_key else 'not set'}")

    if not poly.is_authenticated:
        print(
            "\nFAIL: no credentials. Copy .env.example to .env and set "
            "DEEPFLOW_POLYMARKET__PRIVATE_KEY and DEEPFLOW_POLYMARKET__WALLET_ADDRESS."
        )
        return 1

    failures = 0
    async with PolymarketSession(settings) as session:
        if not session.has_credentials:
            print("\nFAIL: session built no secure client -- see the log line above for why.")
            return 1
        print("\n=== authenticated client ===")
        print("  secure client: constructed (API key derived from the signing key)")

        execution = PolymarketExecution(session, settings)

        failures += await _check_balance_and_allowance(execution, poly.wallet_type)
        failures += await _check_closed_only(execution)
        failures += await _check_open_orders(execution)
        failures += await _check_positions(execution)

    print()
    if failures:
        print(f"{failures} check(s) failed.")
    else:
        print("All read-only checks passed. No orders were placed.")
    return 1 if failures else 0


async def _check_balance_and_allowance(execution: PolymarketExecution, wallet_type: str) -> int:
    print("\n=== collateral ===")
    try:
        allowances = await execution.get_allowances()
    except ConfigurationError as exc:
        print(f"  FAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"  FAIL: balance/allowance read failed: {type(exc).__name__}: {exc}")
        return 1

    balance = allowances["balance"]
    allowance = allowances["allowance"]
    print(f"  balance:   {balance} pUSD")
    print(f"  allowance: {allowance} pUSD")

    failures = 0
    if balance == 0:
        print(
            f"  NOTE: zero balance. Confirm wallet_type={wallet_type!r} is correct -- "
            "the signature type decides which account is read, so a wrong type reads "
            "an unfunded one."
        )
    if balance > 0 and allowance == 0:
        print(
            "  FAIL: funded but unapproved. Every order would be rejected for "
            "insufficient balance/allowance until the exchange contracts are approved."
        )
        failures += 1
    if balance > 0 and allowance > 0:
        print(f"  deployable: {min(balance, allowance)} pUSD (the binding constraint)")
    return failures


async def _check_closed_only(execution: PolymarketExecution) -> int:
    print("\n=== account restrictions ===")
    try:
        closed_only = await execution.get_closed_only_mode()
    except Exception as exc:
        print(f"  FAIL: closed-only read failed: {type(exc).__name__}: {exc}")
        return 1
    if closed_only:
        print("  closed-only: ON -- the venue accepts only position-reducing orders.")
    else:
        print("  closed-only: off (new positions permitted)")
    return 0


async def _check_open_orders(execution: PolymarketExecution) -> int:
    print("\n=== open orders ===")
    try:
        orders = await execution.list_open_orders()
    except Exception as exc:
        print(f"  FAIL: open-order read failed: {type(exc).__name__}: {exc}")
        return 1
    if not orders:
        print("  none working")
        return 0
    print(f"  {len(orders)} working:")
    for order in orders[:10]:
        print(
            f"    {order.order_id} {order.status} "
            f"filled={order.filled_shares} key={order.client_key}"
        )
    return 0


async def _check_positions(execution: PolymarketExecution) -> int:
    """Inventory, which is also what makes the LIVE interlock's reasoning concrete:
    a process that can trade but cannot establish what it already owns is the one
    configuration this design refuses."""
    print("\n=== positions ===")
    try:
        positions = await execution.list_positions()
    except Exception as exc:
        print(f"  FAIL: position read failed: {type(exc).__name__}: {exc}")
        return 1
    if not positions:
        print("  flat")
        return 0
    exposure = Decimal(0)
    for position in positions:
        cost = position.shares * position.average_entry_price
        exposure += cost
        print(
            f"    {position.token_id[:12]}... {position.shares} shares "
            f"@ {position.average_entry_price} = {cost} pUSD"
        )
    print(f"  total cost basis: {exposure} pUSD")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
