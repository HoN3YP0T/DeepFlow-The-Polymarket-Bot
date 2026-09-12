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
