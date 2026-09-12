# Polymarket API conformance review

**Reviewed:** 2026-09-12 · **Against:** https://docs.polymarket.com (live)
· **Reviewing:** commit `1ce68da` (the scaffold)

The scaffold was written against the installed SDK's method signatures. That
establishes what can be *called*; it says nothing about what the venue will
*accept*, what it charges, or what its feeds actually contain. This document
records every place the two disagreed, and where each was fixed.

Findings are ordered by what they cost if left alone.

---

## 1. Taker fees were not modelled at all — they now are

`CostBreakdown.fee_bps` existed and defaulted to `0`; nothing anywhere derived a
fee. Polymarket charges a taker fee on every category except geopolitics:

```
fee = shares × rate × (p × (1 − p)) ^ exponent      (rounded to 5 dp)
```

Two properties matter more than the constant:

- **Symmetric about 0.50**, peaking there. A fill at 0.30 costs the same in
  collateral as one at 0.70.
- **In bps of notional it is `rate × (1 − p)`** — so it shrinks in relative terms
  as price rises, but never to zero. At 0.95 in a 4%-rate market that is 20 bps.
  Against `min_net_ev = 0.005` (50 bps) the fee alone is 40% of the minimum edge
  the system will accept. Omitting it does not shade EV; it inverts the sign of
  marginal trades.

Rates are per category (crypto 0.07, sports 0.05, politics/finance/tech 0.04,
geopolitics 0) but the authoritative source is `market.trading.fee_schedule`,
and a recategorised market disagrees with the table.

**Fixed:** `venue.taker_fee` / `venue.taker_fee_bps` (tested against the
published fee tables), `domain.FeeSchedule`, `Market.fees_enabled` /
`fee_schedule`, `EvEngine.fee_bps`, and
`ExecutionThresholds.fail_closed_on_unknown_fee_schedule` — a market flagged
`fees_enabled` whose schedule did not come through is refused, because the
unknown is always in the direction that flatters EV.

Makers pay nothing, so a resting order carries no fee term at all. That is a
whole cost line, not a discount, and it is the strongest argument for the
post-only path this system currently has no use for.

## 2. `order_timeout_seconds = 10` cannot be a GTD expiry

GTD orders expire **one minute before** their stated expiration, and the
expiration must be at least **three minutes** in the future or the order is
rejected. The shortest expressible GTD lifetime is therefore ~2 minutes — 12×
the configured timeout.

A 10-second working order must be GTC plus a client-side cancel. Had this been
discovered in implementation, the natural reading ("clamp to the minimum") would
have left orders resting twelve times longer than the strategy intended.

**Fixed:** `enums.TimeInForce`, `venue.gtd_expiration` (raises rather than
clamps), documented on `OrderIntent.expires_at` and `order_timeout_seconds`.

## 3. Delayed-matching markets break the entire entry path

Markets can carry `trading.seconds_delay`. An order there is accepted with
status `delayed`: zero filled amount, no trade ids, matching begins later. This
is neither a fill nor a rejection, and the scaffold's `OrderStatus` had no way
to say so — it would have been read as a rejection or as a zero-fill partial.

Worse, on such a market *every* entry outlives a 10-second timeout, and every
fill lands after the edge it was priced on has decayed. This is common on sports
markets, which is most of the system's target surface.

**Fixed:** `OrderStatus.DELAYED`, `Market.seconds_delay`, and
`ExecutionThresholds.max_seconds_delay = 0` — delayed markets are excluded by
default, and including them is an explicit decision to trade blind through the
window.

## 4. A matched trade is not a settled trade

The user stream reports trade settlement as
`MATCHED → MATCHED_NOT_BROADCASTED → MINED → CONFIRMED`, and it can instead go
`RETRYING` or `FAILED`. The scaffold's `OrderStatus.FILLED` collapsed all of
this. Booking a position on `MATCHED` produces a phantom holding — arriving
through the fast path that was documented as the authoritative one for
resolving uncertain submissions.

**Fixed:** `TradeSettlementStatus`, `OrderStatus.MATCHED_UNSETTLED`,
`TradeSettlementFailedError`, `BreakerReason.SETTLEMENT_FAILURE`,
`max_settlement_failures_per_hour = 1`.

## 5. Cricket and badminton have no data source

The venue's sports feed covers **NFL, NHL, MLB, NBA, CBB, CFB, Soccer, Esports,
Tennis**. There is no cricket and no badminton — yet the scaffold ships
`CricketEngine`, `BadmintonEngine`, `CricketState`, `BadmintonState` and their
threshold blocks.

Separately, for the sports it *does* cover, the whole payload is:

```
game_id, sportradar_game_id, slug, league_abbreviation, home_team, away_team,
status, live, ended, score, period, elapsed, finished_at, turn
```

`score` is one `"<home>-<away>"` string. `turn` (possession) is NFL/CFB only.
There is no xG, no shots, no cards, no possession percentage, no server, no
wickets, no rally streak. Every richer field in `engines/sports/state.py` is a
third-party data dependency, and because the models correctly abstain on missing
inputs, they would abstain *permanently* on the venue feed alone. That reads as
a silent bug and is actually a procurement gap.

**Fixed:** `adapters/polymarket/sports_feed.py` (exact wire model, league list,
per-sport case-sensitive status vocabularies, period meanings,
`UNSOURCED_CATEGORIES`), with the constraint documented on `SportsThresholds`
and `state.py`. Both engines stay disabled by default, which they already were.

## 6. Collateral is pUSD, not USDC

Settlement moved to **pUSD** (`0xC011a7…2DFB`), an ERC-20 wrapper over USDC,
6 decimals. `not enough balance / allowance` refers to pUSD. Approving USDC.e
against the exchange leaves the on-chain allowance looking fine and every order
failing.

**Fixed:** `venue.PUSD_COLLATERAL` and the collateral constants; relayer
approval docs. Field names like `min_liquidity_usdc` and `notional_usdc` are
kept for continuity and annotated rather than renamed — a rename touches the
API schema, the database and the dashboard for no behavioural gain.

## 7. Four approvals are required, not two

pUSD *and* Conditional Tokens, each against *both* exchanges:

| Token | Contract | Call |
|---|---|---|
| pUSD | CTF Exchange | `approve(exchange, max)` |
| pUSD | Neg Risk CTF Exchange | `approve(exchange, max)` |
| Conditional Tokens | CTF Exchange | `setApprovalForAll(exchange, true)` |
| Conditional Tokens | Neg Risk CTF Exchange | `setApprovalForAll(exchange, true)` |

`ensure_allowances` was documented in terms of `approve_erc20` /
`approve_erc1155_for_all`, which is where the neg-risk half gets dropped — the
symptom being that every multi-outcome market rejects while binaries work. The
SDK has `setup_trading_approvals()`, which is idempotent and covers all four.
The CLOB also *caches* allowances: an on-chain approval that has not been synced
via `/balance-allowance/update` still reads as missing.

**Fixed:** `relayer.py` docs and `ensure_allowances` TODO.

## 8. A private key alone cannot trade

`PolymarketSettings` had `private_key` + `funder_address`. Two things were
missing:

- **The account wallet address is not derivable from the key.** For a Deposit,
  Safe or Proxy wallet the signer is not the wallet, and the wallet type selects
  the order's `signature_type` (deposit 3, proxy 1, safe 2, EOA 0). Given only a
  key the SDK signs as an EOA, and every order is rejected on signature.
- **Gasless relayer operations need a Relayer or Builder API key.** Without one,
  approvals cannot be granted and resolved positions cannot be redeemed — while
  order placement works fine, so the gap only appears on the first order of a
  fresh wallet and on the first redemption.

**Fixed:** `wallet_address`, `wallet_type`, `relayer_api_key`,
`can_submit_relayer_transactions`, both now enforced in the LIVE-mode guard;
`.env.example` updated. `funder_address` is kept as a deprecated alias.

## 9. Restricted trading modes are waits, not failures

| Status | Meaning | Correct response |
|---|---|---|
| `425` | Matching engine restarting | Back off from 1–2 s and retry |
| `503` + `post_only_mode` | 2-minute post-only window after every restart | Wait `retry_after_seconds`, or convert to a maker order |
| `503` cancel-only | New orders refused, cancels accepted | Wait; exits still work |
| `429` | Rate limited | Exponential backoff |

The scaffold had one `PolymarketApiError` for all REST failures. Routing an
announced two-minute maintenance restart into `API_FAILURE` latches a
non-auto-resuming breaker and takes the system down for the day over a condition
it should have slept through.

**Fixed:** `VenueModeError` and subclasses, `RateLimitedError`,
`venue.is_definitive_rejection`, `venue.RETRYABLE_STATUSES`, restart backoff
thresholds.

`500 order timed out` is documented as provably not-executed, but recognising it
means string-matching an error message and being wrong costs a duplicate
position — so it stays in the uncertain bucket deliberately.

## 10. Prices and sizes must be pre-rounded, and the tick size moves

A sub-tick price is a hard rejection, and the precision table is **not**
derivable from the tick's exponent — `0.005` and `0.001` share a precision row,
as do `0.0025` and `0.0001`. Collateral amounts have their own two-step rule
(round up to `amount decimals + 4`, then down), which exists to stop
`5.19999…` truncating to `5.1999`.

Tick size also **changes at runtime**, announced by `tick_size_change` on the
market stream. A cached value goes stale and every order priced on it is
rejected.

**Fixed:** `venue.PRECISION_BY_TICK`, `round_price_to_tick` (rounds toward the
safe side per order side, so snapping can never worsen the price),
`round_shares` (down, so notional cannot exceed available collateral),
`round_notional`, `to_amount_integer`; the stream contract documented on
`Market.minimum_tick_size`.

## 11. `minimum_order_size` is a notional, not a share count

Documented as a minimum **collateral notional** per order. Comparing a share
quantity against it passes at 0.95 and fails at 0.05, and the scaffold's field
name (`minimum_order_size: Decimal`) invited exactly that reading.

**Fixed:** documented on the field.

## 12. `neg_risk` is a signing input

It selects the EIP-712 verifying contract (`CTF_EXCHANGE` vs
`NEG_RISK_CTF_EXCHANGE`). Signing against the wrong one fails with no hint that
neg-risk was the cause. It must come from the market or book payload, never from
a guess about the question's shape. Augmented neg-risk is configured on the
**event**, not the market.

**Fixed:** `venue.exchange_for`, documented on `Market.negative_risk`, note in
`clob.py` that the book response carries it.

## 13. `price_change` is a level replacement, not a delta

The market stream's `price_change` carries `price`, `size`, `side` per changed
level, where `size` is the level's **new total** (zero removes the level). Fold
it as a delta and depth inflates without bound — and every slippage and
liquidity figure derived from the book inflates with it, in the direction that
makes trades look safer. `book` events carry a `hash` for exactly this
cross-check.

**Fixed:** documented in `streams.py`.

## 14. Heartbeats differ per socket

| Socket | Cadence |
|---|---|
| Market (`/ws/market`) | we send `PING` every 10 s |
| RTDS | we send `PING` every 5 s |
| Sports | server sends `ping` every 5 s; we must reply `pong` within 10 s |

`PolymarketSettings.ws_ping_interval_seconds` was a single shared 20 s value,
which is too slow for all three and drops the sports socket outright.

**Fixed:** per-socket constants in `venue.py`.

## 15. The venue has a dead-man's switch and we were not using it

`POST /v1/heartbeats`: once armed, the venue cancels **every open order** owned
by those CLOB credentials if no valid heartbeat arrives within 10 s (swept every
5 s). Each response returns the next `heartbeat_id`.

Every circuit breaker in this codebase assumes a running supervisor. A crashed,
hung or partitioned process leaves resting orders exposed with nothing watching
them, and this is the only mechanism that survives that. It belongs on by
default.

**Fixed:** `PolymarketExecution.start_order_heartbeat`,
`ExecutionThresholds.require_order_heartbeat = True`, cadence constants.

## 16. Market BUYs are denominated in collateral, not shares

`place_market_order(amount=…)` spends `amount` of pUSD; `shares=…` is the
SELL-side form. Passing a share count as `amount` misizes by `1 / price` — 5% at
0.95, twentyfold at 0.05. `max_spend` caps the *all-in* cost, since taker fees
are charged on top of `amount`.

**Fixed:** documented in `execution.py`; `OrderIntent.max_spend` added.

## 17. Account-level closed-only mode

The venue can restrict an account to position-reducing orders only. Worth
checking before an entry so the refusal is legible once, rather than appearing as
a rejection on every new position while exits keep working.

**Fixed:** `PolymarketExecution.get_closed_only_mode`, `ClosedOnlyModeError`.

## 18. SDK shapes the scaffold described slightly wrong

| Scaffold said | Actually |
|---|---|
| `subscribe(spec)` | `await client.subscribe([spec, …])` → one async context manager yielding a **merged** stream; discriminate on `event.topic` then `event.type` |
| `list_markets(...)` awaited | returns a **paginator**: `await pages.first_page()`, `pages.iter_items()`, `pages.from_cursor(...)` |
| `CryptoPricesSpec` | takes a `topic` (`prices.crypto.binance` / `…chainlink`) and source-specific symbol formats: `btcusdt` vs `btc/usd` |
| flat market fields | grouped: `market.state.*`, `market.trading.*`, `market.outcomes.yes/no`, `market.sports.*` |
| — | `SportsSpec()` takes no filter: it streams **every** game, ours or not |
| — | error hierarchy is `PolymarketError`, with `RateLimitError`, `UserInputError`, `RequestRejectedError(.status)` |

**Fixed:** `__init__.py`, `streams.py`, `discovery.py`, `mapping.py`,
`sdk_client.py`, ADR-0001.

## 19. Crypto reference feed / settlement mismatch

Binance spot and Chainlink TWAP (30 s or 60 s windows only) are different feeds.
`Btc5mEngine` already documents that a wrong reference price flips a 0.02 into a
0.98 at short horizons — which makes the *choice of feed* part of that hazard,
not an implementation detail. The docs also describe crypto markets as
15-minute, while the engine is named and gated for 5-minute; the cadence should
be confirmed against live markets before the engine is enabled.

**Fixed:** `subscribe_crypto_prices(source=…)` and `subscribe_crypto_twap`
split, with the mismatch documented.

---

## Not changed, deliberately

- **`usdc` in field and setting names.** Renaming touches the API schema, the
  database models and the dashboard types for no behavioural gain. Annotated
  instead.
- **`Btc5mThresholds` cadence.** The docs mention 15-minute crypto markets; the
  brief specifies 5-minute. Left as specified, flagged above — the live market
  list settles it, not the docs.
- **Builder attribution, Combos, Perps, session keys, liquidity rewards.**
  Real venue surface, entirely outside this system's scope.
- **ADR-0002's Gamma question.** Unchanged by this review; the docs confirm
  discovery (including `sports_market_types` and tag filters) is Gamma-backed,
  which is what the ADR already says.

## How to re-run this review

```bash
curl -sS https://docs.polymarket.com/llms.txt          # page index
curl -sS https://docs.polymarket.com/llms-full.txt     # all pages, ~1.6 MB
curl -sS https://docs.polymarket.com/changelog/predictions.md
```

`tests/unit/test_venue.py` pins the fee tables, tick grid and GTD arithmetic
against the published values, so a venue change surfaces as a test failure
rather than as a slow bleed in production.
