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

> **Partly superseded by §49 (2026-09-13).** The cricket half is wrong. This
> finding is about the *socket*, which indeed has no cricket vocabulary — but
> Gamma's event index carries live cricket state, and a fixture was observed at
> `score="74-100"`, `period="Live"` with open markets. What cricket lacks is a
> rules module, not a data source. The badminton half stands, downgraded from
> "no source" to "none observed yet". Everything below about the socket's payload
> is still accurate, except its field list and casing — see §50.

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

# Round two — findings from live payloads (Phase 1 slice)

The review above was against the documentation. These came from calling the API
and reading what actually arrived. Both were invisible in the docs and in the
SDK's type signatures.

## 20. Book levels arrive worst-price-first on **both** sides

Confirmed on every market sampled. `bids` ascend from `0.001`; `asks` descend
from `0.999`. The tradeable top of book is the **last** element of each tuple.
`OrderBook` is the other way round, so `to_order_book` reverses both sides.

The scaffold's `mapping.py` docstring said "sort bids descending and asks
ascending", which is the right target — but nothing said the input was reversed,
and the field names give no hint. A pass-through produces:

```
best_bid = 0.001    best_ask = 0.999    spread = 0.998
```

`0.001 >= 0.999` is false, so the crossed-book validator **accepted it**. Two
outcomes, neither good: every market fails the 150 bps spread gate and the system
silently never trades, or a book walked from that end prices a 4¢ contract's fill
at 0.999.

**Fixed:** `to_order_book` reverses both sides; `OrderBook._check_ordering` now
validates the sort order itself, so wire order raises instead of producing a
plausible-looking number. `tests/unit/test_mapping.py` pins it against a recorded
real payload, and the fixture's wire order is itself asserted so a future tidy-up
can't make the tests pass for the wrong reason.

## 21. There is no staging environment

`polymarket.environments` exports exactly one value: `PRODUCTION`.
`PolymarketSettings.environment` offered `Literal["prod", "staging"]`, so a run
configured for staging would have pointed at the live exchange.

**Fixed:** `PolymarketSession.start` raises `ConfigurationError` for anything but
`prod`.

## 22. `get_order_books` does not return books in request order

Observed differing on **5 of 5 trials** with 12 tokens, at *varying* positions
(index 0 on four trials, index 2 on the fifth). With two tokens it happened to
match — which is the worst possible property, because a two-token test passes and
production does not.

Zipping positionally is not merely wrong here, it is silently plausible. A binary
market's YES and NO books are complements: attribute the NO book to the YES token
and a 0.04 outcome prices at 0.96. Both are valid prices. Every gate downstream
agrees with the reflection of the truth, and in the 0.85–0.98 target band the
mistake lands exactly where the system is most willing to trade.

**Fixed:** `ClobMarketData.get_order_books` keys results by `asset_id` and
re-projects onto the requested order, and raises if any token is missing rather
than returning a shorter sequence. The unit-test fake deliberately returns results
in a *different* order from the request, so a positional zip cannot pass its tests.

**Cheap live detector, now in the slice script:** a binary market's two best asks
must sum to roughly 1. A swap breaks it. Depth is mirrored too — `Yes: 35x128`
against `No: 128x35` — which is a second, independent confirmation.

## 23. Two smaller call-shape traps

- **`get_market` has no condition-id lookup.** It accepts `id`, `slug` or `url`
  only. `MarketDiscoveryPort.get_market(condition_id)` was therefore
  unimplementable as literally specified; it routes through
  `list_markets(condition_ids=...)`. The condition id is what our database, the
  venue's position endpoints and analytics all key on, so translating inside the
  adapter beats leaking Gamma's numeric id upward.
- **`page_size` silently caps at 100.** Requesting 500 returns 100, no error, no
  warning. `list_active_markets(limit=500)` reading a single page would have shown
  a fifth of the universe and looked successful. Now capped explicitly and paged.

**Also useful:** `list_markets` supports far more server-side filtering than the
scaffold assumed — `liquidity_num_min`, `volume_num_min`, `condition_ids`,
`clob_token_ids`, `sports_market_types`, `game_id`, `end_date_min/max`, `tag_id`.
The liquidity floor is now pushed to the venue instead of paging the catalogue to
discard most of it.

## 24. `price_change`'s `price` is not the touch — but the event tells you what is

Each change carries `asset_id`, `side`, `price`, `size`, **plus `best_bid` and
`best_ask`**. The `price` is the level that changed, which can be deep in the
book: observed live, a 20,000-share bid at `0.10` on a market trading at `0.92`.
Reading `price` as the new best price would look like an 82-cent crash.

The `best_bid`/`best_ask` on every change turn out to be the most useful thing on
the feed. They give a **free integrity check**: fold the level, then compare our
computed touch against the venue's reported one. Disagreement means a dropped or
misapplied update, detectable immediately rather than at the next REST poll.
`BookState.drifted()` does exactly this, and a drift marks the book gapped.

One frame also carries a *list* of changes with independent `asset_id`s, so both
sides of a market move in a single event.

**Replacement semantics confirmed empirically.** Folded live for 25–45 s across
30 books (420+ level changes), then compared against fresh REST snapshots:
**16/16 books agreed at the touch, and every depth count matched exactly**
(`36x129` folded vs `36x129` REST). Delta arithmetic would have diverged
immediately.

## 25. Stream `book` events omit trading constraints that REST provides

The stream's `book` payload carries `tick_size`, but `min_order_size` and
`neg_risk` come back **`None`** — both are populated on the REST book. The
earlier docs review (§1 of round two, `streams.py`) said all three constraints
could be refreshed from the stream; only the tick size can.

Consequence: `neg_risk` is a *signing input*, so it must come from discovery or a
REST book, never from the stream. A market whose constraints were only ever seen
on the stream would sign against the wrong exchange.

## 26. `subscribe()` is a coroutine despite its return annotation

Its signature reads `-> SubscriptionHandle[...]`, but it is an `async def`, so
calling it yields a coroutine with no `__aenter__`. It must be awaited first, and
the result is then the async context manager:

```python
async with await client.subscribe([MarketSpec(token_ids=[...])]) as stream:
    async for event in stream:
        ...
```

The documentation shows this correctly; the type signature does not. Trusting the
annotation gives `AttributeError: __aenter__` on a coroutine.

## 27. Ours, not the venue's: freshness is feed liveness, not last change

Found by running the quality check against the live stream, which reported
**4 STALE and 2 DEGRADED out of 120 snapshots on a healthy connection with zero
reconnects**. The venue was fine; the implementation was wrong.

The mistake was timestamping a folded book with `updated_at` — when that book last
*changed*. On an order book, no update means **no change**, so a book untouched
for thirty seconds on a live feed is entirely correct. Measured across sixteen
tokens on one connection, last-change ages spanned **1.4 s to 30.7 s** — a quarter
would have read as expired.

The consequence is the wrong way round from a normal bug: it silently *blocks*
trading, and it blocks it hardest on quiet markets — which in the 0.85–0.98 band
are exactly the ones this system exists to trade. Nothing would have crashed. The
system would simply have found fewer opportunities than it should, indefinitely,
and the reason would have looked like conservative risk settings.

**Fixed:** freshness is now connection-wide feed liveness
(`PolymarketStreams.last_event_at`), passed into `BookState.snapshot(as_of=...)`
and used as the snapshot's `captured_at`. `updated_at` is kept for diagnostics.
This also gives staleness detection for free: when the connection drops, liveness
stops advancing and every book ages into STALE with no extra bookkeeping. Live run
after the fix: **276/276 FRESH**.

Recorded here rather than in a commit message alone because it is the kind of
error a later refactor would reintroduce — `updated_at` is the obvious field to
reach for, and it is wrong.

## 28. Ours: the snapshot tables could not have become hypertables

`market_snapshots` and `smart_money_events` each had a surrogate `id` as sole
primary key. TimescaleDB **refuses to convert a table whose unique indexes omit
the partitioning column** — a surrogate key cannot be enforced across chunks. The
conversion would have failed the first time it ran, in production, against
populated tables.

Both now carry composite primary keys — `(id, captured_at)` and
`(id, observed_at)` — set in the *initial* migration, where it costs nothing.

Caveat stated plainly: this comes from Timescale's documented requirement, not
from a local reproduction. TimescaleDB is not installable in this container, so
the conversion path itself remains unproven until it runs against the Timescale
image. CI now uses `timescale/timescaledb:latest-pg16` so the next push exercises
it.

## 29. Ours: `OrderRepository.record(order)` could not write a row

`OrderRecord` carries what the venue told us — status, fills, errors — and
deliberately none of the trade's identity: no token, side, size, or price.
`OrderRow` requires all of those, `NOT NULL`. The port as specified was
unimplementable without inventing values.

The fix keeps the asymmetry rather than erasing it: `record(order, *, intent,
run_mode)`, where the intent is required on first write and ignored afterwards. An
intent does not change, so a later status update cannot silently rewrite what the
order was for — and a status update (a fill arriving on the user stream) genuinely
has no intent to hand over, so it reads the existing row instead. A new row
without an intent raises, because an order nobody can attribute to a token and
size is unreconcilable against the venue, which is the one thing the table exists
to support.

# Round three — findings from Phase 2 classification

## 30. `tags` are omitted unless you ask for them

The venue returns no tags at all without `include_tag=True`. Phase 1's discovery
adapter did not pass it, so every market arrived untagged — and tags are the
classifier's primary signal. A classifier reduced to keyword-matching question text
is how a cricket market gets routed to a football model.

Worse, `mapping.to_market` coerced each tag with `str()`. Tags are
`MarketTag(id, slug, label)` objects, so that produced the repr: every tag looked
unique and none would ever have matched.

**Fixed:** discovery passes `include_tag=True`; `Market` carries `tags` (slugs, for
reading) and `tag_ids` (for matching). Ids are the join key because `get_sports()`
publishes its league mapping as ids, and a slug rename would silently stop a match.

## 31. The venue publishes its own league→tag map

`get_sports()` returns **465 leagues** with their tag ids
(`cricpsl → 1,100639,517,103805`). A league the venue adds becomes classifiable
without a release. Live, this taught the classifier **70 tag ids** the static map
did not have.

Two traps in using it: the generic ids (`1` sports, `100639` games) appear on every
league, so mapping them to whichever sport was iterated first poisons the map; and
the call must be allowed to fail without stopping classification, or a Gamma hiccup
takes the pipeline down.

## 32. A sport tag does not mean the market is a game

Of **20 sport-tagged markets sampled live, zero carried a `sports_market_type`** —
they were awards and season milestones ("Will Mbappé win the 2026 Ballon d'Or?"),
correctly tagged `soccer`. Every real fixture carried both a market type and a
`game_start_time`.

The sport engines model **in-play** state — score, clock, wickets. An awards market
routed to one would be asked for state it cannot have. Tag-only classification put
36 markets in FOOTBALL; with the match-evidence gate, 17.

**Fixed:** a market in a match category must show venue evidence of a fixture
(`sports_market_type` or `game_start_time`), otherwise it becomes `OTHER_SPORTS` —
which has no engine, so it records what the market is and still cannot trade.

## 33. `politics` and `geopolitics` overlap on purpose

Every "leader out by" market carries both tags, at equal confidence, which the
naive separation check called ambiguous and refused to trade. They are not
ambiguous — they are both true.

**Fixed:** a tie between *related* categories resolves by specificity
(`GEOPOLITICS > POLITICS`, `BTC_5M > CRYPTO`); a tie between unrelated categories
(cricket vs politics) still abstains, because that means the tags or our reading of
them are wrong. UNKNOWN fell from 13 to 7 of 360.

## 34. Correction to §5: cricket markets exist

§5 said cricket and badminton "have no data source". Too strong. Cricket **markets
exist and are tradeable** — "BPL: Chattogram Challengers vs Durbar Rajshahi", tagged
`cricket`, type `moneyline` — and `get_sports()` lists many cricket leagues.

What is missing is **live game state**. That market has `game_id=None`, so it cannot
be joined to the sports stream, and cricket is absent from the stream's status
vocabulary. The markets are there; the in-play feed is not. Since the 0.85–0.98 band
this system targets is an in-play phenomenon, the engines stay disabled — but the
markets classify correctly and get recorded rejections rather than being misrouted.

> **Superseded in turn by §49 (2026-09-13).** The `game_id=None` observation holds
> and is the reason the socket join cannot reach cricket. The conclusion drawn from
> it does not: live cricket state arrives through Gamma's event index, which needs
> no game id — a fixture was observed at `score="74-100"`, `period="Live"`. Two
> corrections deep on the same finding, both from testing one source and concluding
> about the venue.

## 35. Open markets can have an `end_date` in the past

Markets awaiting resolution stay `closed=False` with an end date already gone. Any
time-to-expiry arithmetic must treat a negative remainder as "pending resolution",
not as a market about to settle.

## 36. Resolution text comes in two shapes, and the second has no YES clause

Measured over 250 live markets after normalising punctuation:

| Shape | Share | Form |
|---|---|---|
| Explicit binary | 46% | `resolve to "Yes" if <condition>. Otherwise ... "No".` |
| Group winner | 54% | `This market will resolve to the person who wins <event>.` |

The group form is a negative-risk group: payout is winner-takes-all, so no market
states its own YES condition. The condition is *this market's entity* winning, and
the only field naming that entity is **`group_item_title`** — populated on 100% of
group markets sampled.

A validator demanding an explicit YES clause marks that 54% UNPARSEABLE and refuses
over half the catalogue, for markets that are not ambiguous at all.

The venue writes the group form at least six ways across 23 observed templates,
including `resolve according to the party that wins`, `resolve according to the
winner of`, `resolve based on`, a multi-word noun (`the listed candidate that
wins`), and one typo of its own (`resolve to according to`). Generalising the
pattern across these moved UNPARSEABLE from **47.5% to 16.7%**.

## 37. The curly-quote trap

Polymarket writes the payout clause with typographic quotes (U+201C / U+201D), not
ASCII, on most markets. Matching ASCII quotes alone detects the clause on **7% of
markets instead of 46%**.

A validator built without normalising punctuation would reject nearly every market,
and the cause would be one invisible character. Every text now passes through a
substitution table before any pattern runs.

## 38. `consensus of` is not an ambiguity marker

It appears in **58% of live markets**, almost always as *"a consensus of official
&lt;named&gt; sources"* — a specific, checkable authority. The scaffold's marker list
included it, which would have graded most of the catalogue AMBIGUOUS over a turn of
phrase. The genuinely vague cousin, *"consensus of credible reporting"*, is caught by
a narrower marker.

### Calibration, 360 live markets

| Verdict | Share |
|---|---|
| VALID | 46.9% |
| AMBIGUOUS | 36.4% |
| UNPARSEABLE | 16.7% |

Only `VALID` is tradeable, via `ResolutionCriteria.is_tradeable` — named explicitly
so that widening it has to edit a tested property rather than being smuggled in as a
comparison at a call site. The dominant reasons for holding a market back are a
deadline with no stated timezone (75) and a judgement-call marker (56).

**Roughly half of all markets are held back.** That is the safe direction, and it is
also the largest single lever on how many markets this system can ever trade.

## 39. Book imbalance is meaningless without a stated depth band

Measured on live books, the sign of book imbalance **flips with the band**:

| Market (mid) | within 1¢ | within 5¢ | whole book |
|---|---|---|---|
| Xi Jinping (0.0405) | +0.18 | +0.98 | +0.57 |
| Newsom (0.1385) | **−0.88** | **+0.17** | **−0.87** |
| AOC (0.1755) | **+0.51** | +0.38 | **−0.89** |

These books span the full 0–1 range, so a whole-book total compares "all buy
interest below the touch" against "all sell interest above it" — including people
resting at 0.999. The whole-book figure is dominated by dust and is an artifact of
where participants park orders, not a property of the market.

So the band is the definition, not a parameter. Depth is measured inside
`MicrostructureThresholds.depth_band` (one cent by default — comparable at any price
level on a 0–1 contract, and roughly what a taker of ordinary size sweeps), and the
same measure is recomputed at five times the band as a robustness check: **if the
signs disagree, no imbalance is reported at all.** On live books that check fires on
3 of 5 markets.

Two related observations from the same run:

* **Depth is highly concentrated near the touch** — 63% to 91% of banded depth at a
  single level on most politics markets sampled. Such a book is one cancellation
  from empty however large the total looks, which is why unmeasurable concentration
  reads as *not* liquidity-ok rather than as fine.
* **Slippage is brutal at size on cheap contracts.** Sweeping 500,000 shares of a 4¢
  market walks the book to 0.999 — 133,870 bps over the touch, a 13× price. Correct
  arithmetic, and the reason the estimate returns `None` rather than a partial walk
  when the book cannot fill the size.

# Round four — the sports feed, per sport

Captured live: 185 events across 22 leagues, saved to
`tests/fixtures/sports_feed_capture.json`.

## 40. The feed's eight fields mean different things per sport

| sport | `score` | `period` | `elapsed` |
|---|---|---|---|
| soccer | `'2-0'` goals | `'1H'` | `'23'` minutes, counting **up** |
| american football | `'0-10'` points | `'Q1'` | `'05:04'` mm:ss, counting **down** in the quarter |
| tennis | `'2-3'` games **in the current set** | `'S1'` | absent |
| esports | `'0-0\|0-1\|Bo5'` rounds\|maps\|format | `'2/5'` | absent |

Reading any with another sport's rules gives a confident wrong answer, not an error.
`'05:04'` as soccer minutes is 5 played and 85 left, when the truth is ~50 left.
`'2-3'` as a tennis *set* score is a match nearly over, when it is barely begun.
`'2-0|0-1|Bo5'` read on its first field is a player ahead, when they are behind in
the series.

Hence one rule module per sport, not one parser: `engines/sports/rules/{soccer,
gridiron,tennis,esports}.py`, with a registry resolving league → sport.

## 41. Tennis parses cleanly and is still unmodellable

A `wta` match went `'2-1'` then `'2-3'` while `period` stayed `'S1'`; a
`grand slam` match moved `S1 → S2` with the score resetting to `'0-0'`. So `score`
is **games in the current set**, and the **set score is never transmitted**.

That is disqualifying rather than degrading. A player 2-3 down in games during set
three could be two sets up or two sets down — the same payload describes a match
nearly won and one nearly lost. Serve, point score and match length (Bo3 vs Bo5) are
absent too.

So `MatchState` separates `blocking_gaps` from `unavailable`: soccer's missing
stoppage widens the uncertainty band, tennis's missing set score forbids a number at
all. `is_modellable` is False for tennis while `is_live` and both scores are present
— which is the honest combination.

## 42. Esports is better-specified than soccer

`Bo5` states the series length, so the target is **known** rather than assumed, and
maps-won is a small discrete state space. Soccer's format is fixed but its stoppage
time is not reported at all — and at 87' an assumed 2 minutes against an actual 7
understates the chance of an equaliser by more than a third, in exactly the window
the late-game strategy targets.

## 43. Period vocabularies resolve the sport offline

The venue's league list (`get_sports()`, 465 leagues) resolves soccer, cricket and
esports by tag id — 272 leagues learned, 304 known in total. But it is a network
call, and without it soccer was unresolvable, which is the sport the system most
wants to trade.

Period labels turn out to be sport-specific and reliable: only soccer uses
`1H`/`2H`/`HT`, only tennis `S1`/`TB1`, only gridiron `Q1`, only baseball `End 1`.
That became the offline resolution tier. Result: **22/22 captured leagues resolved,
0 unknown.**

Tag ids are *not* reliable beyond three sports — `678` appears on both baseball and
basketball leagues — so those sports are named explicitly, and an explicit mapping
beats the venue's.

## 44. Correction: the `League` enum was the wrong vocabulary

`sports_feed.py` listed `NFL, NHL, MLB, NBA, CBB, CFB, Soccer, Esports, Tennis`.
Those are the documentation's **status-vocabulary families**, not what the feed
sends. The wire values are league codes: `lal`, `nor`, `cze1`, `wta`,
`grand slam`, `cs2`, `lol`.

The overlap is what made it subtle — `NFL`, `NBA` and `CFB` are both a family and a
league code, so a naive match appears to work while silently missing every soccer,
tennis and esports league.

## 45. RETRACTED — "the market ↔ live-game join does not exist"

**This finding was wrong.** Kept, not deleted, because the way it was reached matters
more than the conclusion: every probe behind it was run against the live venue, and
every one of them still produced a false negative. See §46–52 for the correction.

What it claimed, and what is actually true:

| Claim | Reality |
| --- | --- |
| Markets never carry a `game_id` (0 of 26) | The *fixture* id is on the **event**. The market's `game_id` is a different id space — the child contest — and empty on fixture-level markets |
| 0 of 600 moneyline markets had a kickoff in −3h..+24h | Measured with `start_date_*` (market open date) instead of `start_time_*` (kickoff). Sports markets open weeks early, so the window excluded almost everything |
| Only fuzzy team-name matching remains | `list_events(game_ids=…)` is an exact join, and `list_events(live=True)` needs no key at all |

The root cause was not a bad probe. It was never reading the API spec: `gamma-openapi.yaml`
is listed in `https://docs.polymarket.com/llms.txt`, and the SDK already exposed every
parameter involved (`list_events(game_ids=…, live=…, start_time_min=…)`).

---

## 46. The join is on the event, and it is exact

`list_events(game_ids=[…])` maps a sports-feed `gameId` to the event families built on
that fixture. `game_id` is a repeatable parameter, so twenty fixtures cost one request.

Verified live on 2026-09-13: **9 of 9** fixtures streaming on the sports socket
resolved, yielding 15 tradeable markets with live asks; a second run resolved **8 of
8**. Also verified against a finished Ligue 1 fixture — feed `gameId` 90112380 returned
`fl1-str-asm-2026-09-12`, "RC Strasbourg Alsace vs. AS Monaco FC", the same two teams
the feed named.

Reproduce: `scripts/verify_game_join.py`. Implemented in
`adapters/polymarket/games.py`.

---

## 47. One fixture is many events, each repeating the same in-play state

Game 90112380 returned **nine** events: moneyline, halftime result, second half
result, exact score, first to score, spreads, and three first/second-half families.
Every one carried the same `score`, `period` and `live`.

Iterating events therefore produces nine game states for one game. `GameLink` folds by
fixture; the fold keeps the shortest slug as the fixture's identity, because the
siblings are that slug plus a market-family suffix.

---

## 48. `live=true` returns in-play fixtures with their state and markets — one request

`list_events(live=True, closed=False)` returned 15 events with `score`, `period`,
`elapsed` and their open markets: **278 open, order-accepting markets** in play at the
time of measurement. No socket required.

This is the better cold-start path. The sports socket only reports what *changes* after
you connect, so a bot that has just started knows nothing about a game already at half
time until something happens in it.

**But `live=true` does not mean in play.** A suspended Chile Primera fixture reported
`live=True` with `period="SUS"` and a `start_time` three days in the future. Guard:
`GameLink.is_in_play` requires live, not ended, a period outside
`{SUS, POST, CAN, INT, AB, DELAYED}`, and a kickoff that has passed.

---

## 49. Cricket has live state — retracting the "no venue-native feed" claim

An international cricket fixture was observed live via Gamma with `score="74-100"`,
`period="Live"` and open markets. `sports_feed` documents cricket as having no
venue-native state source, and `CricketEngine` was disabled on that basis.

The weaker claim is the true one: cricket has no *socket* coverage. It also carries
**no `game_id`**, so it is reachable through the `live=true` sweep and not through a
socket join. Folding keys on the event id when the game id is absent — dropping
id-less fixtures would have removed an entire sport silently.

---

## 50. The sports wire is camelCase, and the SDK discards its richest field

Read raw from `wss://sports-api.polymarket.com/ws`, the payload is `gameId`,
`leagueAbbreviation`, `homeTeam`, `awayTeam` — not the snake_case names the docs and
this codebase use. The SDK renames them via `validation_alias`, so only a raw reader is
affected; a raw reader keying on snake_case sees **zero games** and reads as a dead
feed.

There is also a second, richer message shape. College football sent:

```json
{"gameId": 70898578, "sportradarGameId": "7c45f1f0-…", "turn": "haw",
 "turnProviderId": "97673f68-…", "updatedAt": "2026-09-13T07:18:23Z",
 "eventState": {"type": "college-football", "score": "19-29", "period": "Q4",
                "elapsed": "01:05", "footballState": {"possessionProviderId": "…"}}}
```

`eventState` is a typed per-sport envelope, and `eventState.type` is an authoritative
sport discriminator — strictly better than the league-tag and period-signature
heuristics in `engines/sports/rules`. The SDK's base model is `extra="ignore"`, so
**`eventState`, `turnProviderId` and `updatedAt` are all silently dropped**. Using them
means bypassing `SportsGameResult`.

Status casing is also inconsistent across sports for the same concept: `"running"`
(esports), `"inprogress"` (college football), `"InProgress"` (earlier capture). Any
exact-match status table will misread a live game as scheduled.

---

## 51. `sports_market_types` is silently ignored on the events endpoint

`list_markets(sports_market_types=…)` filters correctly. The same filter on
`/events/keyset` returns the **whole catalogue** — `kraken-ipo-in-2025`,
`macron-out-in-2025` — with no error. A filter that is ignored rather than rejected is
worse than one that is absent: it reads as a sports-only feed while delivering
everything.

`links_from_events` therefore skips events with no sports block instead of trusting the
caller's filter.

---

## 52. The sports surface that was never read

All of this was in `gamma-openapi.yaml` and in the SDK, unused:

- **`/teams`** — 34 leagues on the first page alone, with `abbreviation`, `alias`,
  `record`, `logo`, and an undocumented `providerId`. Market slugs are
  `{league}-{home abbr}-{away abbr}-{date}`, so this is a second, offline join path.
- **`/sports/market-types`** — **240** values. This system prices two (`moneyline`,
  `child_moneyline`); `TRADEABLE_SPORTS_MARKET_TYPES` is the gate, and widening it is a
  code change with a test attached.
- **Event sports fields** — `score`, `elapsed`, `period`, `gameStatus`, `gameId`,
  `rescheduledFromGameId`, `homeTeamName`, `awayTeamName`, `spreadsMainLine`,
  `totalsMainLine`, `bestLines`, `teams`. The SDK models all of them on
  `EventSportsMetadata`.
- **Event query params** — `live`, `ended`, `game_id`, `event_date`, `event_week`,
  `series_id`, `start_time_min`/`max`, `include_best_lines`.
- **`eventMetadata`** — names the odds provider per fixture: `opticOddsGameId` on
  soccer, `pandascoreMatchId` on esports (equal to the feed's `gameId`), plus
  `gridSeriesId`, `league`, `leagueTier`, `tournament`.
- **`/events/results`** — historical fixture results, marked `x-excluded` in the spec.
  The obvious calibration corpus, and untouched.

Also: `fee_type` is now `sports_fees_v3`, not the `sports_fees_v2` the classifier
docstring names. Matching is prefix-based so behaviour is unaffected.

---

## 53. RETRACTED — "no short-dated crypto markets exist"

`docs/STATUS.md` recorded this as a blocker: *"Scanned 900 open markets: 30 crypto,
all with windows over 24 hours, none short-dated and unexpired. `Btc5mEngine` has no
market."*

**Wrong.** The venue lists a 5-minute up/down market **per asset, every five
minutes**. Measured on 2026-09-13, eight assets were running the cadence
simultaneously — BTC, ETH, XRP, SOL, DOGE, BNB, HYPE, ZEC — with 20 windows open
within ±15 minutes of the moment of measurement, all `acceptingOrders: true` with
live books at tick 0.01. A 15-minute variant (`btc-updown-15m-…`) exists too, which
settles the open question about whether the cadence was 5 or 15 minutes: both.

Found because the owner pasted a link to one that was live at the time.

Reproduce: `scripts/verify_short_dated_crypto.py`.

---

## 54. The contest window is `end_date - eventStartTime`, never `end_date - startDate`

The reason the scan saw nothing. A short-dated market **opens about 24 hours before
the window it settles on**:

```
btc-updown-5m-1789303800
  startDate       2026-09-12T12:58:02Z   <- listed for trading
  eventStartTime  2026-09-13T12:50:00Z   <- the contest begins
  endDate         2026-09-13T12:55:00Z   <- settles

  end_date - start_date       = 86,217s   <- what the scan and classifier measured
  end_date - eventStartTime   =     300s   <- the contest
```

Off by **287×**, in the direction that makes every one of them look like a
long-horizon forecast. The consequence was not only a bad scan: `_apply_short_dated`
used the same arithmetic, so **every short-dated crypto market on the venue was
classified as plain `CRYPTO`** and would have been priced with a long-horizon model.

This is the same mistake as §45's third cause, in a second domain: measuring a
contest from when the *market* opened. `Market.contest_window_seconds()` is now the
single place it is computed, and it returns `None` rather than falling back to
`start_date` when `event_start_time` was not fetched — the fallback *is* the bug.

`eventStartTime` is only reachable through the event: the SDK's market model drops
the field, and the stub event nested on a market carries id, slug and title with no
schedule. So **a discovery path built on `list_markets` cannot see a contest window
at all.**

Also worth recording: the unit test for this promotion passed throughout, because it
built `start_date=NOW, end_date=NOW+5min` — a shape the venue never sends.

---

## 55. Tags are on the event; markets carry none — and the venue publishes the cadence

Every market on a captured short-dated crypto event had `tags: []` while its event
carried seven, including the decisive ones:

```
('21','crypto')  ('102892','5M')  ('102127','up-or-down')  ('1312','crypto-prices')
('235','bitcoin') / ('39','ethereum') / ('101267','xrp') + ('101312','ripple')
```

`102892` is the venue **stating the cadence** — strictly better than deriving it from
timestamps. It is now the primary signal for `BTC_5M`, with the window as
corroboration. `mapping.with_event_context` inherits event tags onto a market that
has none, since tags are the classifier's primary tier and a market reached through
an event otherwise arrives with its best signal missing.

The same fix exposed a keyword gap the window bug had been masking: a live XRP market
classified as **UNKNOWN**, because `CRYPTO`'s keyword seeds listed `ethereum` and
`solana` but no `xrp` or `ripple`. Nothing had caught it, because no short-dated
crypto market was ever being promoted far enough to notice.

---

## 56. Cricket's fixture id is a string, in `eventMetadata` — the third cricket correction

Cricket has now been wrong three times in this document, each time smaller:

| Finding | Claim | Verdict |
| --- | --- | --- |
| §5 | "no data source" | wrong — markets listed, accepting orders |
| §34 | "markets exist, no in-play feed" | wrong — live state observed via Gamma |
| §49 | "live state, but no `game_id`, so unjoinable" | **wrong in its reason** |

The typed field really is empty: `event.sports.game_id` is `None` on every cricket
fixture. But the venue does publish an id — in `eventMetadata.gameId`, as a string:

```
crint-ind-afg-2026-09-13   eventMetadata.gameId = "1000169067LIVE2026"
crint-jpn2-mys2-2026-09-12 eventMetadata.gameId = "id2705330274512646"
```

Not even a consistent shape between the two, so nothing would parse them as
integers. The SDK models the numeric field as `int | None`, so reading only that
field concludes the sport has no identity at all.

`games.provider_game_id()` now reads both, preferring the numeric one (the socket
only ever sends integers, so that field stays typed for the join) and falling back to
the metadata string. Cricket fixtures therefore fold on their real id instead of on
an event-id placeholder.

The general lesson is the one from §45 and §54 again: a field being absent in a typed
model is not the venue lacking the data.

---

## 57. Cricket gives runs and the innings phase — not wickets, overs or balls

What the feed actually carries for cricket, confirmed across live and finished
fixtures:

```
live      score="74-100"    period="Live"
finished  score="151-150"   period="FT"      elapsed=None
```

Published `period` vocabulary is `1H`, `1A`, `2H`, `2A`, `SO`, `FT` — innings by
batting side, super over, full time. **Neither the sports AsyncAPI spec nor the Gamma
OpenAPI spec mentions wickets, overs, balls or innings counts anywhere**, and cricket
has no `elapsed` because it has no clock.

Runs alone cannot place a chase. A side needing 100 with two overs and one wicket is
nearly beaten; needing 100 with ten overs and eight wickets it is comfortable. The
feed emits the same `score` in both cases. That is structurally identical to tennis's
missing set score (§41) and just as disqualifying: wickets and balls remaining are
both first-order terms, and no assumption substitutes for either.

So `rules/cricket.py` parses cricket faithfully and declares
`blocking_gaps=("wickets_fallen", "balls_remaining")`, making `is_modellable` `False`
with a named reason. That is a better outcome than the engine being absent on a claim
that was wrong three times — the abstention is now structural and explained.

Two market-type notes from the same fixture:

- **`cricket_toss_winner` prices at exactly 0.5 / 0.5.** A coin toss carries no
  information to model, so after the taker fee it is negative-EV by construction. It
  is already outside `TRADEABLE_SPORTS_MARKET_TYPES`; named in `rules/cricket.py` so
  nobody later mistakes the symmetry for an opportunity.
- **`cricket_completed_match`** ("will the match be completed?", 0.5055) is an
  abandonment market. It needs weather and ground data, not game state, so no venue
  source prices it either.

Also: cricket's event `endDate` is not the match end. `crint-ind-afg-2026-09-13`
starts 13:30 on the 13th and carries `endDate` a week out, so any expiry arithmetic
must use `eventStartTime` — the same distinction as §54.

---

## 58. The spec declares a staging CLOB that does not answer

`clob-openapi.yaml` lists `https://clob-staging.polymarket.com` as a server, and
`data-openapi.yaml` lists `data-api-rs.stage.pmd.use1.polymarket.sh`. Neither
resolves: `GET https://clob-staging.polymarket.com/time` fails to connect, where the
production host returns `200` immediately.

So the earlier finding — no usable staging environment, therefore every verification
runs against production with real markets — **stands**, and is now better grounded: it
is not that the venue never mentions staging, it is that what it mentions is not
reachable. The SDK agrees; `polymarket.environments` exposes `PRODUCTION` only.

Recorded because the spec listing it was nearly written up as a retraction. Checking
before concluding cuts both ways.

---

## 59. `/clob-markets/{condition_id}` exists, and its keys are abbreviated

An earlier finding recorded that Gamma's `get_market` accepts only `id`, `slug` or
`url`, so a condition-id lookup required a filtered `list_markets` call. True of
Gamma — and the CLOB has had a direct one all along.

It returns a terse payload unlike anything else on the venue. Decoded against Gamma
for the same market (the cricket toss market), every key maps 1:1:

| Key | Gamma field | Example |
| --- | --- | --- |
| `gst` | `gameStartTime` | `2026-09-13T13:30:00Z` |
| `sd` | `secondsDelay` | `1` |
| `mos` | `orderMinSize` | `5` |
| `mts` | `orderPriceMinTickSize` | `0.01` |
| `mbf` / `tbf` | `makerBaseFee` / `takerBaseFee` | `1000` |
| `ao` | `acceptingOrders` | `true` |
| `cbos` | `clearBookOnStart` | `true` |
| `aot` | `acceptingOrdersTimestamp` | `2026-09-06T14:16:21Z` |
| `fd` | `feeSchedule` | `{"r":0.05,"e":1,"to":true}` |
| `r` | rewards | `{"mi":50,"ma":4.5,"moas":30}` |
| `t` | tokens | `[{"t":"<token id>","o":"India"}]` |
| `c` | `conditionId` | — |
| `ibce` | (undocumented, `true`) | — |

This is the cheapest single call for everything the order path needs: tick size, min
order size, delay, fee schedule, accepting-orders, token ids. One caveat — **`fd`
omits `rebateRate`**, which Gamma reports as `0.15` on the same market, so it is not a
complete fee schedule.

Two things it confirms about cricket in passing: the toss market has `sd = 1`, so it
is a *delayed* market and our own `max_seconds_delay = 0` default excludes it; and its
tick is `0.01`, not `0.001`.

---

## 60. `funded` and `ready` are not tradeability gates

Both read `false` on **100 of 100** open markets that were simultaneously
`acceptingOrders: true` and `enableOrderBook: true`. Gating discovery on either would
reject the entire catalogue.

Recorded as a negative result because the names invite exactly that mistake, and
because a filter that silently rejects everything looks identical to a venue with no
markets — which is the shape of §53's error.

---

## 61. The Data API publishes its own ingestion lag

`GET data-api.polymarket.com/v2/status` returns a freshness report:

```json
{"computed_at": "2026-09-13T13:15:30Z", "age_seconds": 11,
 "serving": {"lag_seconds": 4, "worst": "activity_feed",
   "mechanisms": [{"name": "activity_feed", "age_seconds": 4},
                  {"name": "custody_balances", "age_seconds": 0, "blocks_behind": 27},
                  {"name": "pnl", "age_seconds": 2, "blocks_behind": 2}]},
 "ingestion": {"cursors": 148, "network": "polygon", "chain_id": 137,
               "max_synced_block": 93733530}}
```

`features.assess_quality` measures the age of data against **our** clock only, so a
Data API that is four seconds behind chain head is invisible to it: trades read from
it are stale by that much no matter how fresh our timestamps look. Per-mechanism
`age_seconds` and `blocks_behind` make that measurable rather than assumed, and
`worst` names the laggard directly.

Not yet wired. Recorded as the correct input to a staleness decision that currently
has no view of the source's own delay.

---

## 62. `makerBaseFee` and `takerBaseFee` are a constant `1000` and reconcile with nothing

Sampled across 100 open markets: `1000` for both, on every market, regardless of
`feeType`, while the same markets carry `feeSchedule: {"rate": 0.04, "exponent": 1,
"takerOnly": true}` (politics) or `0.05` (sports). The CLOB spec types them as bare
`integer` with no description, and `takerOnly: true` means makers pay nothing at all —
which a non-zero `makerBaseFee` contradicts outright.

So they are almost certainly an AMM-era remnant. The conclusion is not "they mean bps"
or "they mean hundredths of bps": it is that **fee arithmetic must keep using
`feeSchedule`**, and that reading these as a rate in any unit would be a large, silent
error in the one calculation that decides whether a trade is worth making.

---

## 63. Crypto up/down markets resolve on a Chainlink TWAP, not a price snapshot

From the changelog (7 Aug 2026), found via the docs MCP server:

* Crypto up/down markets resolve using **Chainlink-computed time-weighted average
  prices**, not a single price at the boundary.
* **Both** the price to beat and the settlement price come from the TWAP feed.
* Averaging windows: **5-minute markets use a 30-second lookback; 15-minute and
  4-hour markets use 60 seconds.**
* A **4-hour** variant exists, in addition to the 5- and 15-minute ones already found
  live (§53).

This answers the open question recorded against roadmap item 14 — "which feed (Binance
spot vs Chainlink TWAP 30/60 s) does each market settle against?" — and the answer
means a `Btc5mEngine` priced off Binance spot would be mispriced by the basis between
spot and a 30-second Chainlink average, at exactly the horizon where that basis is the
whole edge.

Symbol formats differ by source and are not interchangeable: Binance takes
`btcusdt`, `ethusdt`, `solusdt`, `xrpusdt`; Chainlink takes `btc/usd`, `eth/usd`,
`sol/usd`, `xrp/usd`. Note the live venue runs the cadence on eight assets (§53),
more than either list covers.

---

## 64. Sports limit orders are cancelled automatically at game start — and may not be

Documented behaviour, and the counterpart to the `clearBookOnStart` flag the surface
audit surfaced:

> Outstanding limit orders are **automatically cancelled** once the game begins,
> clearing the order book at the official start time. However, game start times can
> shift — if a game starts earlier than scheduled, orders may not be cleared in time.

Two consequences. Any folded book state held across the start boundary is stale by
construction, because the venue empties the book there. And the guarantee is
best-effort: an early start can leave a resting order live into a game in progress,
which is the one case a pre-game price was never meant to survive. Resting orders into
a start time therefore need cancelling by us, not by the venue.

---

## 65. The documented pagination maximum is wrong

`/events/keyset` documents `limit` as "Maximum number of results to return (max 500)".
Measured: requesting 100, 200 and 500 returns **100 every time**. The server caps at
100 regardless of what the spec claims, which confirms the earlier finding of a
100-item page cap and contradicts the published figure.

Recorded because the direction matters: a caller trusting the spec would size its
sweep at 500, silently receive a fifth of it, and conclude the venue had fewer markets
than it does. That is the mechanism behind §53.

## 66. The CLOB accepts no client-supplied order id

`ExecutionPort.find_by_client_key(key)` could not be implemented, and the reason is
a venue fact rather than an SDK gap:

- `create_limit_order` / `create_market_order` take no client-id parameter.
- `OpenOrder` returns only the venue's own `id`; there is no field our key could
  come back in.
- `client_order_id` exists **only on the perps API**, which is a different venue
  surface and not one this system trades.
- The signed order's `metadata` bytes32 is neither indexed nor echoed, and
  `_order_contents_hash` is private to the SDK.

So the idempotency key is ours alone: the venue never sees it and cannot be asked
about it. The port therefore became
`find_by_intent(intent: OrderIntent) -> OrderRecord | None`, matching on the
material the key is itself derived from — token id, side, **exact** price, **exact**
size — read back off the open-order set (`_matches_intent` in
`adapters/polymarket/execution.py`).

Two consequences worth stating, because they are now load-bearing:

1. **Two identical intents inside one idempotency window are indistinguishable at
   the venue.** A scale-in at the same price must pass an explicit salt, which is
   what `execution.engine.build_intent(..., salt=...)` is for.
2. **Absence from the open set is not proof the order never existed.** A filled or
   cancelled order is not "open". The reconciler pairs the lookup with a position
   read for exactly this reason — an order that filled between our request and the
   lookup appears as inventory, not as a working order. This is why only
   `UncertainOutcome.ABSENT` permits a re-intend.

## 67. `AssetType` is a `Literal` alias, not an enum — and mypy cannot see the difference

`polymarket.models.clob.AssetType` is

```python
AssetType = Literal["COLLATERAL", "CONDITIONAL", "CONDITIONAL-V2"]
```

so `AssetType.COLLATERAL` raises `AttributeError` at call time. Both collateral
reads in the execution adapter were written that way and both were dead on the
first call. The SDK passes the value straight through to the query string, so the
correct argument is the plain string; the adapter now names it as
`COLLATERAL: Final = "COLLATERAL"`.

The part worth keeping is **why it survived lint, mypy and 604 tests**:
`pyproject.toml` sets `ignore_missing_imports` for `polymarket.*`, which makes every
SDK symbol `Any`, and `Any.ANYTHING` type-checks. The import path was also wrong
(`polymarket.models.clob.enums` does not exist) and that too was invisible for the
same reason. Nothing that consults only the type checker can catch this class of
error — it needs an actual import and an actual call.

Which is what `scripts/verify_account.py` is for: it importing the adapter is what
found both bugs. Recorded alongside §50 and §60 as another instance of the standing
lesson in the other direction — **a typed symbol that satisfies mypy is not a symbol
that exists at runtime.**

## 68. Position size is `current_size`, and reading the wrong name reports a flat account

`polymarket.models.data.portfolio.Position` (what `list_positions` yields) has:

| Field | Note |
| --- | --- |
| `current_size` | shares held — **not** `size`, `shares` or `quantity` |
| `asset_id` | token id, aliased from `token_id` on the wire |
| `avg_price`, `realized_pnl`, `condition_id` | as expected |
| `total_size` | a *different* quantity; not a fallback for `current_size` |
| `end_date`, `last_event_at` | there is **no open time** on the model |

Two independent call sites each guessed at a name the model does not have —
`mapping.to_position` read `size`, and the adapter's zero-filter read
`size`/`shares`/`quantity`. The two bugs compounded in the worst available direction:
every position mapped to **zero shares**, and every zero-share position was then
**dropped by the filter**, so `list_positions` returned an empty tuple for an account
holding inventory.

A reconciler that reports flat while the account holds positions is the precise
failure the LIVE interlock exists to prevent acting on, and it would have passed
every gate — nothing downstream can tell "flat" from "read wrongly". Both sites now
go through one reader, `mapping.position_shares`, so the next rename is one failure
rather than two silent ones.

`opened_at` has no venue source at all. It is set to an explicit epoch sentinel with
the local journal as the authority, and reconciliation must not diff that field
between the two sides; last activity is not an open time.

Found the same way as §67 — by importing the adapter from a script and reading the
installed model, not by type-checking it.

## 69. A blank credential is not an absent credential (our bug, not the venue's)

Recorded here because it is the same shape as the venue findings and was found the
same way — by filling in a real `.env`.

`.env.example` ships every credential as a bare `KEY=`. Copying it therefore loads
`private_key` as `SecretStr("")`, which is **not `None`**, and
`PolymarketSettings.is_authenticated` read `private_key is not None`. With a wallet
address filled in and the key line left untouched, the guard would report an
authenticated account, the session would build a secure client from an empty key, and
the failure would surface as a signature rejection with nothing pointing at the
cause. The LIVE guard has the same hole, on the side that matters.

Fixed with one `mode="before"` validator over every credential field — normalising
blank-to-`None` in a single place, so a new credential field cannot reintroduce it —
and pinned by two tests. This is hard rule 2 in `CLAUDE.md`: a check that passes for
want of an input is worse than no check.

---

## Confirmed correct

Worth recording, since these were guesses that happened to be right:
`trading.fee_schedule` matches the `FeeSchedule` model field-for-field
(`rate`, `exponent`, `taker_only`, `rebate_rate`); the grouped accessors
(`state.*`, `trading.*`, `outcomes.yes/no`, `sports.*`) are as documented;
`seconds_delay` is `None` rather than `0` when absent; token ids are `None` until
a book opens.

## Also observed, not yet used

The SDK returns more than the scaffold models, and some of it removes work later:

- `market.prices` — `best_bid`, `best_ask`, `spread`, `last_trade_price`, and
  price changes over 1h/1d/1w/1mo/1y. Discovery already carries a coarse quote,
  so a cheap pre-filter need not fetch a book per market.
- `market.metrics` — `liquidity`, `volume_24hr`, `volume_clob` and more, which is
  what `min_liquidity_usdc` needs and what `MarketSnapshot` already has fields for.
- `market.trading.fee_type` — a category string (`'politics_fees'`), a direct
  cross-check against the published per-category rate table.
- `market.rewards` — `rewards_min_size`, `rewards_max_spread`, daily rate. Only
  relevant if this system ever quotes, but it is the maker-rebate surface.
- `market.resolution` — `question_id`, `resolved_by`, `uma_resolution_status`;
  UMA plumbing that `ResolutionValidator` will want in Phase 2.
- `outcomes.*.position_id` and `market.sports.game_id` / `line` — the sports
  `game_id` is the join key to the sports stream's `game_id`, which is how a
  market gets matched to a live game.
- Trades carry `wallet`, `name`, `pseudonym`, `outcome_index`,
  `transaction_hash` — the raw material for `SmartMoneyEngine`.

**Live fee reality check.** Running the slice against real books, a politics
market at 0.04 charges **383 bps** of notional (`0.04 × 0.96`). In the system's
0.85–0.98 target band that falls to 60 bps at 0.85 and 8 bps at 0.98 — but
`min_net_ev` is 50 bps, so at the bottom of the band the fee still exceeds the
entire minimum edge. Finding §1 is not a rounding concern.

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

**Read `docs/POLYMARKET-SURFACE-AUDIT.md` first.** It holds the complete inventory
(9 hosts, 223 operations, 5 WebSocket channels) and `make audit-surface`, which diffs
raw venue JSON against what the SDK and our domain model can see. Three findings in
this document were wrong because that diff did not exist; running it is now the step
before recording that the venue lacks anything.
