# ADR-0001: Use `polymarket-client` as the SDK

**Status:** Accepted · **Date:** 2026-09-11

## Context

The brief specifies "the official Polymarket Python SDK as the primary
integration layer". Two packages answer to that description in circulation.

Verified against the published packages at the time of writing:

| Package | Repo | State |
|---|---|---|
| `py-clob-client` | `Polymarket/py-clob-client` | **Archived.** README states it "is no longer functional and should not be used for new or existing integrations" and directs users to the unified SDK. |
| `polymarket-client` | `Polymarket/py-sdk` | Current unified SDK. Version 0.10.0 inspected directly. |

Most third-party tutorials and generated code still reference
`py-clob-client`, which makes picking the wrong one the default outcome.

## Decision

Use **`polymarket-client`** (`polymarket` import namespace), pinned
`>=0.10,<1`. `py-clob-client` must not be reintroduced.

## Verified surface (0.10.0)

The SDK covers all four integration layers the brief requires:

| Layer | Surface |
|---|---|
| **CLOB** | `get_order_book(s)`, `get_midpoint(s)`, `get_price(s)`, `get_spread(s)`, `get_last_trade_price(s)`, `estimate_market_price` |
| **WebSocket** | `subscribe(spec)` with `MarketSpec`, `SportsSpec`, `CryptoPricesSpec`, `CryptoPricesChainlinkTwapSpec`, `UserSpec` |
| **Data API** | `list_trades`, `list_activity`, `list_positions`, `list_market_holders`, `get_user_stats`, `get_user_pnl`, `get_user_volume`, `list_trader_leaderboard`, `list_biggest_winners`, `list_price_history` |
| **Relayer** | approvals, gasless submission, nonce handling, submit/poll; `approve_erc20`, `approve_erc1155_for_all`, `split_position`, `merge_positions`, `redeem_positions` |
| **Execution** | `create_limit_order`, `create_market_order`, `post_order(s)`, `cancel_order(s)`, `cancel_all`, `list_open_orders`, `get_order`, `get_balance_allowance`, `wait_for_order_fill_settlement` |

Clients: `AsyncPublicClient` (unauthenticated reads) and `AsyncSecureClient`
(authenticated trading). Environment config exposes `clob_url`,
`clob_market_ws_url`, `clob_user_ws_url`, `relayer_url`, `data_url`,
`rtds_ws_url`, `sports_ws_url`.

## Consequences

**Good.** The SDK's own stack — Pydantic v2, httpx, websockets, asyncio —
matches ours, so there is no impedance layer. Its `SportsSpec` and
`CryptoPricesSpec` streams map directly onto the sports and BTC 5-minute
engines, which would otherwise need third-party data feeds.

**Cost.** `>=0.10,<1` is pre-1.0: minor releases may break. Mitigated by
confining all SDK imports to `adapters/polymarket/` and all type translation to
`adapters/polymarket/mapping.py`, so an upgrade surfaces as failures in one
module rather than as subtly wrong numbers in an engine.

**Requires Python >= 3.11.**

## Verification

Do not take this document's word for the surface. Re-check after any upgrade:

```bash
pip download polymarket-client --no-deps -d /tmp/pm && \
  unzip -o -q /tmp/pm/*.whl -d /tmp/pm/x && \
  grep -E "^\s{4}(async )?def [a-z]" /tmp/pm/x/polymarket/clients/async_secure.py
```
