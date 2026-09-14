# Status and handover

**As of:** 2026-09-13 · **Branch:** `claude/adoring-hypatia-pxi7up`
· 31 commits · **780 tests passing, 0 skipped** · **44 stubs remain** · 86 findings recorded
· 109 source files, ~12,500 lines

**Phase 1** complete · **Phase 2** complete · **Phase 3** 3 of 6 · **Phase 4** complete
· Phases 5–8 not started

> An earlier version of this line read "395 tests green" while 30 of those were
> integration tests **skipped** for want of a database — a skipped test reporting as
> green is how an unverified persistence layer passes review. `make db-local` starts
> the local Postgres they need; every number here is with them running.

---

## 1. What the process does today

`make run` brings up four supervised tasks:

| Task | What it does | Cadence |
| --- | --- | --- |
| `discovery` | Sweep the catalogue, classify, validate resolution text, persist | 300 s |
| `live-games` | Resolve in-play fixtures, read each with its own sport's rules | 20 s |
| `market-stream` | Fold the book stream, assess quality, persist snapshots | live socket |
| `health` | Feed liveness and reconnects, fed to the breaker supervisor | 30 s |

```
orchestrator.starting   mode=PAPER not_yet_wired=['sports socket (…)', 'signal loop', …]
sport_registry.loaded   leagues_known=304 added_from_venue=272
discovery.swept         markets=10 tokens=20
live_games.swept        fixtures=3 modellable=3 tradeable_markets=9 unresolved_leagues=[]
streams.connected       tokens=20
orchestrator.health     connected=True reconnects=0 dropped=0 in_play_fixtures=3 snapshots_written=336
orchestrator.stopped    snapshots_written=500
```

**What it can do:** find markets, read their rules, reject the ones it cannot read,
follow live books and live games, and — as of Phase 4 — compute expected value, run a
17-check safety gate, size a position under every limit, and record the whole
decision with its reasons.

**What it cannot do:** produce a probability. No model is written, so the decision
layer runs on injected estimates. And it cannot trade: `Orchestrator.start` rejects
`LIVE` before any socket opens, because execution and reconciliation do not exist.
There is no bypass.

The honest summary: **the system can explain in full why it would not trade, and
cannot yet explain why it would.**

### Operating it

```bash
make up            # postgres + redis (docker)     make check     # lint, typecheck, tests
make db-local      # local postgres, no docker     make migrate   # apply migrations
make run           # discover, stream, persist     make api       # dashboard API
make verify        # 5 live checks vs the venue    make audit-surface   # raw-JSON diff
make capture-fixtures   # refresh the payload corpus
```

Verification scripts need no credentials — every endpoint they touch is public:

| Script | Proves |
| --- | --- |
| `verify_slice.py` | discovery, mapping, batched books, fee arithmetic |
| `verify_stream.py` | stream folding matches a fresh REST snapshot |
| `verify_game_join.py` | in-play fixtures resolve to their tradeable markets |
| `verify_short_dated_crypto.py` | 5-minute crypto windows exist and classify correctly |
| `verify_phase1.py` | the whole chain, including persistence and staleness |

---

## 2. Done, and verified live

### Phase 1 — data spine (complete)

| Component | Verified by |
| --- | --- |
| `sdk_client.py` — session lifecycle, refuses a signing client without key **and** wallet | live |
| `mapping.py` — SDK → domain, the single anti-corruption layer | recorded real payloads |
| `discovery.py` — tradeable filtering, server-side liquidity floor, page cap | live |
| `clob.py` — books keyed by `asset_id`, midpoint, spread, trades, book walk | live, 16/16 vs REST |
| `streams.py` + `book_state.py` — one socket fanned out, folding, gap marking, drift detection | 16/16 books matched REST after 420 folded changes |
| `features.assess_quality` — FRESH / DEGRADED / STALE / INCONSISTENT | live, 276/276 FRESH |
| `persistence/` — markets, snapshots, orders; conditional Timescale migration | real PostgreSQL |
| `venue.py` — fee formula, tick grid, GTD arithmetic, failure classification | published tables |
| `orchestrator.py` — supervised task set, ordered shutdown | 500 rows in a 40 s run |

`verify_phase1.py`, last run: 6 markets discovered and persisted, 82 snapshots
streamed and folded, 164 rows written and read back, all FRESH — and the same book
correctly reported STALE when the clock is advanced past the budget.

### Phase 2 — classification and validation (complete)

| Component | Result on live markets |
| --- | --- |
| `classifier.py` — venue tag ids primary, seeded from `get_sports()` | 360 markets, 1.9% UNKNOWN |
| `resolution.py` — two resolution shapes, tiered verdicts | 46.9% VALID / 36.4% AMBIGUOUS / 16.7% UNPARSEABLE |
| `discovery.py` (pipeline) — lifecycle, journalled rejections, idempotent sweeps | 300 markets: 161 monitored, 139 rejected, **0 unresolved** |
| `SqlJournalRepository` — decision log | real PostgreSQL |

### Phase 3 — probability (3 of 6 items)

| Item | State |
| --- | --- |
| 10 · `FeatureEngine` + `MicrostructureEngine` | **done** — banded depth, robustness check, slippage |
| 11 · `TennisEngine` | **dropped** — the feed cannot support it (§41) |
| — · Per-sport rule modules (added) | **done** — soccer, gridiron, tennis, esports, cricket; 22/22 captured leagues resolved |
| — · Market↔live-game join (added) | **done and wired** — `games.py`, the `live-games` task, `verify_game_join.py` (§46–48) |
| 12 · `FootballEngine` | **not started** — unblocked; the join that feeds it now exists |
| 13 · `CricketEngine` | **resolved as a structural abstention** — parses, cannot be modelled (§57) |
| 14 · `Btc5mEngine` | **not started** — unblocked; markets exist, TWAP spec known (§53, §63) |
| 15 · Calibration fitting | **not started** — needs recorded in-play history |

### Phase 4 — EV and safety (complete)

| Item | What it does | Key decision |
| --- | --- | --- |
| 16 · `EvEngine` | `net_ev = edge × fill − costs` | `market_probability` is the **ask crossed**, so the spread is inside the edge and `spread_cost_bps` stays zero rather than double-charging |
| 18 · `SafetyGate` | **17** checks, all mandatory | **Fail-closed**: every context field defaults to `None`, and `None` fails the check that reads it |
| 19 · `RiskEngine` + `ExposureTracker` | Six vetoes, correlation grouping | Exposure is **cost basis, never mark value**; an unknown market is its own correlation group |
| 20 · `JournalRecorder` | Entries, rejections, holds, exits | Rejections carry the **same** evidence as entries; a write failure is logged, never raised |

The two checks beyond the specified fifteen came from findings 63–64:
`BOOK_CLEARED_AT_START` (the venue empties the book at a contest start, best-effort)
and `REFERENCE_FEED_MATCHED` (crypto up/down settles on a Chainlink TWAP, not spot).

---

## 3. Fixed issues

Two kinds, kept separate because they fail differently: bugs in the code, and
conclusions about the venue that were wrong.

### Bugs found and fixed

| Bug | Consequence had it shipped |
| --- | --- |
| **Freshness measured by last *change*, not feed liveness** | 4 STALE + 2 DEGRADED of 120 on a healthy connection. Silently blocked trading on quiet markets — precisely the 0.85–0.98 band this system targets. Fixed via `snapshot(as_of=last_event_at)`; 276/276 FRESH after |
| **Surrogate primary keys blocked hypertables** | Time-series tables could never have been converted. Composite PKs `(id, captured_at)` added in the initial migration |
| **`OrderRepository.record(order)` was unimplementable** | No way to tie an order row to the intent that produced it. Now `record(order, *, intent, run_mode)`, raising `ReconciliationError` on a new row without an intent |
| **`monitored_count` counted "everything not invalid"** | Reported 393 monitored when 0 were. Now counts `MONITORED` only, with `classified_count` alongside |
| **Discovery discarded tags** (`include_tag=True` missing) **and `mapping` called `str()` on tag objects** | Produced a repr, so no tag could ever match. Both fixed; the classifier's primary tier was inert until then |
| **`is_modellable` returned True for tennis** while the module docstring said False | My docstring described behaviour I had not written. Fixed by adding `blocking_gaps` |
| **Double upsert per monitored market** | Two writes where one was needed; `upsert_market=False` on the second |
| **The classifier measured a market's window from `startDate`** | **Every** short-dated crypto market on the venue classified as long-horizon `CRYPTO` and would have been priced with the wrong model. Now `Market.contest_window_seconds()` measures from `event_start_time` and returns `None` rather than falling back (§54) |
| **The `CRYPTO` keyword set had no `xrp` or `ripple`** | A live XRP market classified as `UNKNOWN`. Masked by the window bug, which stopped anything reaching that check |
| **Two duplicate book-walk loops** | Each copy another chance to stop the walk in the wrong place. Consolidated into `OrderBook.vwap_to_fill` |
| **A registry that could not resolve soccer offline** | Soccer leagues came only from the network. `_PERIOD_SIGNATURES` added as the offline tier → 22/22 |
| **REST reports soccer full time as `VFT`**, the socket as `FT` | Found by *wiring* the sweep — no unit test could have. The offline tier missed every finished soccer fixture |

### Cruft removed (2026-09-13)

- **Two islands wired.** `games.py` and `engines/sports/rules/` were each written,
  tested and verified against the live venue — and neither was reachable from the
  running process. Every unit test passed while the pipeline had a probability layer
  it could not feed. Joined by `CATEGORY_BY_SPORT`, the `SportKind ↔ MarketCategory`
  translation that did not exist, and driven by the `live-games` task.
- **Dead code deleted:** `League`, `SUPPORTED_LEAGUES`, `STATUS_VALUES`,
  `PERIOD_MEANINGS`, `VENUE_NATIVE_CATEGORIES`. None had a consumer in `src/`, and the
  recorded reason for keeping `League` was circular — "because `STATUS_VALUES` is
  keyed by it", where `STATUS_VALUES` was referenced by nothing at all.
  `PERIOD_MEANINGS` was also *wrong*: it listed `1Q`–`4Q` where the feed sends `Q1`–`Q4`.
- **A docstring describing behaviour that did not exist** — `sports_feed.py` claimed
  an engine registry used these constants to refuse unsourced engines. No registry did.
- **Numbers in the header that had stopped being true**, including "395 tests green"
  counting 30 skipped tests.
- **An unreproducible fixture** — the join corpus had been hand-trimmed in a scratch
  script. `scripts/capture_sports_fixtures.py` regenerates it and **exits non-zero
  when the capture would test less than the committed one**.
- **Four verification scripts in no Makefile target.** `make verify` now runs them.

### Wrong conclusions, retracted

Each was reached by probing the live venue and still produced a false negative. The
pattern in all four: **concluding about the venue from one typed field or one query
shape.** Three were caught by the owner pasting a URL.

| Claimed | Actual | The mistake |
| --- | --- | --- |
| No market↔live-game join (§45) | `list_events(game_ids=…)` — 9/9 live fixtures resolve | Looked on the *market*; the id is on the **event**. And `streams.py` documented the wrong place, so the error was written down as the design |
| No short-dated crypto markets (§53) | One per asset every 5 min, 8 assets, 20 windows open at once | Measured the window from `startDate`. Off by **287×** |
| Cricket has no data source (§5) → no in-play feed (§34) → no `game_id` (§49) | `eventMetadata.gameId = "1000169067LIVE2026"` | Three corrections, each smaller. The id is a **string**, in a field the SDK types `int \| None` |
| Sports feed covers no cricket | True of the socket; Gamma's event index carries live cricket | Tested one source, concluded about the venue |

**The fix was tooling, not resolve.** `make audit-surface` diffs raw venue JSON
against what the SDK and our domain model can see, and prints every unnamed field
*with a sample value* — because `"1000169067LIVE2026"` says "string id" at a glance
where `gameId: present` says nothing. 92 unmodelled fields currently.
`docs/POLYMARKET-SURFACE-AUDIT.md` holds the full inventory (9 hosts, 223 operations,
5 WebSocket channels) and the method as a checklist.

One retraction was **avoided** the same way: the CLOB spec declares
`clob-staging.polymarket.com`, so "no staging environment" looked wrong. Tested it
first — it does not resolve. The original finding stands (§58).

---

## 4. Incomplete inside work already called "done"

"Phase complete" does not mean "nothing missing".

**`venue.py`** — `taker_fee` supports only `exponent=1`, the single observed value.
Any other exponent gets a float approximation.

**`OrderBook`** — no tick-size validation at construction. `venue.py` can round to a
tick; nothing forces a price onto the grid before execution.

**`streams.py`** — three feeds stubbed: `subscribe_crypto_prices`,
`subscribe_crypto_twap`, `subscribe_user`. The user stream is Phase 5's dependency.
The SDK also drops the sports wire's `eventState` block entirely (§50), so the
authoritative per-sport type discriminator is unreachable through it.

**`clob.py`** — `get_last_trades` makes two calls, fetching a book purely to learn a
`condition_id` already held on the `Market`. Wasted round trip.

**`persistence/`** — `SqlPositionRepository` (3 methods) stubbed. The **hypertable
conversion has never run**: TimescaleDB is not installable in this container, so the
composite keys follow the documented requirement and the conversion itself is
unproven. CI uses the Timescale image.

**`classifier.py`** — 183 of the venue's 465 leagues fall outside the reliable tag
ids and resolve only if their period vocabulary matches. `WAR_CONFLICT`, `CEASEFIRE`
and `MILITARY_DIPLOMATIC` have keyword seeds, no tag ids and no engine.

**`resolution.py`** — plateaued at ~47% VALID. Dominated by "deadline has no explicit
timezone" (75) and judgement-call markers (56). The single largest lever on how many
markets this system can ever trade, and a regex validator is near its limit.

**`pipeline/discovery.py`** — `MONITORED` is terminal. Nothing re-validates a market
whose rules change, nothing removes one that closes, and a stale market stays
monitored.

**`orchestrator.py`** — reconciliation skipped (nothing can have ordered yet); health
is a log line, not a breaker. The sports **socket** is still not subscribed; in-play
state comes from the REST sweep, which is the cold-start-correct source (§48).

**`engines/sports/rules/`** — the offline period tier treats `1H`/`FT` as soccer,
which the published cricket vocabulary overlaps. The tag tier is what actually
separates them; this one is the fallback behind it.

**Phase 4 has no model to decide on.** Every path is tested against *injected*
probabilities. The arithmetic and every refusal are verifiable today; the models are
Phase 3 items 12–15.

**`_calibrate` is the identity function.** Every probability the system produces will
be uncalibrated until item 15, and in the 0.85–0.98 band a model that says 0.97 and
is right 0.93 of the time turns a positive edge negative.

---

## 5. What is left

**44 stubs**, by area:

| Area | Stubs | Notable |
| --- | --- | --- |
| `adapters/polymarket` | 1 | `streams.py` (`subscribe_user`); the crypto price and TWAP streams now implemented |
| `api/routers` + `api` | 24 | Whole dashboard surface: auth, health, overview, positions, risk, strategies, trades, whales, journal, WebSocket |
| `engines/sports` | 8 | Four engine bodies (football, tennis, cricket, badminton) |
| `execution` | 0 | complete — `OrderManager`, `ExecutionEngine`, `Reconciler` all implemented |
| `positions` | 0 | complete — `PositionManager` and `ExitEngine` implemented |
| `engines` | 3 | `signal` (1), `cross_market` (2); `smart_money` complete |
| `modes` | 2 | `backtest` (2); `paper` complete |
| `adapters/persistence` | 3 | `SqlPositionRepository` |
| `adapters/cache` | 3 | `RedisCache` |
| `engines/crypto` | 0 | complete — `Btc5mEngine` implemented and verified live |

**Phase 3 — probability (2 remaining).** `Btc5mEngine` is **done and verified against the
live feed** — the first model in this system that produces a number. Left: `FootballEngine`
(unblocked: the join, live fixtures, score/period/clock) and calibration fitting (needs
recorded in-play history, which only our own recorder can collect).

**What the BTC model does, and what it refuses to do.** `P = Phi(ln(S/K) / (sigma *
sqrt(T_eff)))`, where `K` is the Chainlink TWAP at the window's opening instant, `S` the
latest TWAP, and `T_eff = T - 2w/3` because settlement is the *average* over the final 30
seconds rather than the endpoint. Verified live: spot $8.71 below the strike at T=120s
priced 0.376, which matches `Phi(-0.3162)` by hand.

It abstains on: an unobserved strike (the venue publishes none, §77 — so the process must
have been watching when the window opened, and usually was not), a stale reference tick
however fresh the book is, a spot series where a TWAP is required, a market not naming the
Chainlink stream, an unknown asset, a time to expiry inside the 30-second averaging window
or outside the configured band, and an unmeasurable volatility. Three of those came out of
the live run rather than the design (§76-78).

**Phase 5 — execution: complete (6 of 6), with the writes unverified.** `OrderManager`,
`ExecutionEngine`, `Reconciler`, `PaperExecutor` / `ShadowExecutor`, breaker
supervision, all of `PolymarketExecution` (submit, cancel, cancel_all, the order
heartbeat, and the reads) and `PolymarketRelayer` (approvals, redemptions).

**Reads are verified live (§70); every write is not.** No order has ever been submitted
to this venue, no approval granted, no heartbeat posted, and the relayer key is
configured but unconfirmed — nothing readable exercises it. The write paths are written
from the published spec and the installed SDK models, and four findings (§71–74) came out
of reading them closely: the `"context canceled"` → 400 override, the unwrapped
heartbeat route, the hidden approval-and-re-post inside `place_*_order`, and rejection
arriving as a return value alongside `success: true`. The first live submission is their
first real test. **Uncertain-outcome handling comes before the
happy path**, and the reconciler must land before the execution adapter — a process
that can trade but cannot establish what it already owns is the one configuration
this design refuses.

**Phase 6 — positions and intelligence (3 of 4).** Done: `ExitEngine` and
`PositionManager` (item 27); `SmartMoneyEngine` and `DataApiWalletIntel` (item 28, wallet
reads verified live); `EventPipeline`, `GeopoliticalEngine` and `PoliticalEngine` (item
29). Left: `CrossMarketEngine` (item 30), deferred at the owner's request and due to stay
disabled until backtested.

**Two honest gaps in item 29, both deliberate and neither hidden:**

1. **No source feeds the event pipeline.** There is no news ingestion anywhere in this
   system — Gamma serves market metadata, the RTDS carries prices, comments and sports,
   and none of them carry wire copy. So nothing calls `EventPipeline.ingest` in
   production. The pipeline is written and tested so that adding a feed is wiring rather
   than design.
2. **Nothing constructs a `BaseRate`.** Political and geopolitical markets have no
   continuously observable state, so an engine's only candidates for a number are an
   external prior or the market price — and the price is circular, forbidden by
   `BaseProbabilityEngine`'s first contract. With no prior, **both engines always
   abstain.** That is the correct behaviour for an engine holding no evidence, not a
   stub: the abstention paths are implemented, tested and journalled, and a prior arrives
   from polling data or a scheduled timetable when one is wired.

Neither engine is registered with `EngineRegistry`, because the registry still has no
consumer — that arrives with a Phase 3 model. A test pins that the two can be registered
together without a category collision, which is the real risk.

**The short-dated crypto path is now wired end to end and runs.** `Orchestrator` gained a
`reference-feed` task holding the Chainlink TWAP for all eight published assets, a targeted
discovery sweep for up/down markets, and a decision chain in the stream loop: classify →
probability → sizing → EV → the 17-check gate → the journal. Decisions are recorded and
**never submitted** — no execution adapter is constructed in this process, so that is true
by construction rather than by a flag.

Getting it running found eight things no unit test could, all now recorded as findings:

| | What |
| --- | --- |
| §79 | The TWAP topic publishes 8 symbols; a hardcoded list had 3 wrong and missed 3. And `symbols=[]` is rejected where `None` means "everything" — which tripped a breaker via retry. |
| §80 | The general sweep returns **zero** up/down markets. They need their own event-based path, bounded by `start_time`, because stale windows stay `closed=False` with open books 39 days later. |
| §81 | Running the chain per snapshot dropped **1.3M events**; classification and resolution now run once per market. Volatility was also recomputed over a 1,800-element series per snapshot. |
| §82 | A zero bankroll produced **23,249 estimates and zero journal rows**, surfacing as "the book cannot support this" three layers from the real cause. |
| §83 | The journal reported **3,642 decisions and wrote none** — `signal_id` overflowed `varchar(64)`, and write failures are swallowed by design. `write_failures` is now on the health line. |
| §84 | The model needs **~12 minutes of warm-up** before it can speak, so a process restarted often can never trade these markets. |
| §85 | **Retracts part of §63.** Every market's own text says a **60-second** TWAP, including all 32 five-minute ones; the changelog says 30. The text is what the market pays on. |
| §86 | Up/down markets parsed as `UNPARSEABLE`, so the gate refused **400 of 400** decisions — the parser wanted the literal token `yes` and these are labelled Up/Down. |

**What the gate now says, which is the point of all of it.** Before, six checks failed for
want of an input — 400 of 400 rows on `RESOLUTION_VALID`, and the same on data age, spread,
slippage, liquidity and duplicate state. That is fail-closed working as designed *and* a gate
not examining the trade. With the inputs supplied from measurement, the failures are on
merit. Measured on the newest 60 decision rows after the 60-second-window correction:
`SPREAD_ACCEPTABLE` 60, `LIQUIDITY_SUFFICIENT` 53, `RISK_APPROVED` 53,
`SLIPPAGE_ACCEPTABLE` 23, and `RESOLUTION_VALID` down from 400 of 400 to **7 of 60**.

**Nothing has been approved**, which is the expected answer for markets priced near 0.5
against a 20% volatility floor, and the spread figure says why: the median spread of 217 bps
against a 300 bps cap leaves almost nothing, because **one 0.01 tick near the money is
already 200 bps**. Whether that is the market being untradeable or the cap being wrong is a
calibration question the journal can now answer — which is what the journal is for, and what
it could not do a day ago.

Final verified run: 282 estimates, 53 journalled decisions, 0 write failures, after the
~12-minute warm-up.

**Still unwired**, and each for a stated reason: execution (a journalled approval is not an
order), the position manager (nothing can open a position), reconciliation (nothing has
placed one), the user stream, the smart-money poll, the sports socket, and probability for
every category except short-dated crypto.

**Phase 7 — dashboard (0 of 4).** 24 API stubs; the Next.js frontend is scaffolding.

**Phase 8 — validation (0 of 5).** `BacktestRunner.run`, `BacktestExecutor.submit`;
calibration curves; failure-injection suite; paper then shadow run; limited live.

---

## 6. Blockers

### Genuinely blocked

**Tennis in-play state.** The socket sends games in the current set but never the set
score, so the same payload describes a match nearly won and one nearly lost.
`rules/tennis.py` parses it and declares the gap blocking. Gamma's event `score` was
not observed carrying it either; unverified against a live tennis fixture.

**Cricket in-play modelling.** Runs and the innings phase, never wickets, overs or
balls — neither spec mentions them (§57). 100 needed with two overs and one wicket is
nearly beaten; with ten overs and eight wickets it is comfortable, and the feed says
the same thing in both cases. Parses, abstains with the gap named.

**Badminton.** No live state observed on either source. Recorded as *not seen yet*
rather than proven absent — the distinction three cricket corrections earned.

**Calibration data.** Fitting needs (in-play probability, realised outcome) pairs.
The venue exposes current and final state only, so the history has to be recorded by
this system before item 15 can be done at all.

### One decision is yours

**Does "do not use Gamma" exclude Gamma-backed discovery?** The CLOB has no catalogue
endpoint; `list_markets`, `list_events` and `get_sports` all hit
`gamma-api.polymarket.com`, and the live-game join makes that dependency deeper, not
shallower. Recorded as `allow_gamma_backed_discovery` — a flag that is *documentary
only, enforced nowhere*. Needs deciding before live.

---

## 7. What is not proven

- **The hypertable conversion** — no Timescale locally. First real run is on CI.
- **Order submission, cancellation and the relayer.** Reads are now verified live
  against a real account (§70): the credentials derive an API key, and balance,
  allowance, closed-only mode, open orders and positions all come back. Every **write**
  remains unexercised — no order has been placed, and the relayer key cannot be
  confirmed by any read, since approvals and redemptions are writes.
- **Market variety** — verification ran mostly against binary politics markets and
  sports fixtures. Neg-risk groups will have their own surprises.
- **Anything about profitability.** No model produces a probability. The fee
  arithmetic already says margins are thin: at 0.85 the taker fee alone is 60 bps
  against a 50 bps minimum edge.
- **`makerBaseFee` / `takerBaseFee`** are a constant `1000` that reconciles with
  `feeSchedule` in no unit (§62). Fee maths uses `feeSchedule`; these are recorded as
  *do not use*.

---

## 8. Recommended next step

**A probability model, before Phase 5.** Execution builds order submission for
signals that do not exist; the decision layer is finished and idle for want of an
input.

Of the two unblocked models, **`Btc5mEngine` is the more tractable**: a barrier
problem against a known reference, with a fully specified settlement rule (Chainlink
TWAP, 30 s lookback) and a fresh market every five minutes across eight assets, so it
can be checked continuously rather than waiting for a fixture. `FootballEngine` is
the more valuable and the slower to verify — soccer fixtures arrive a few per hour and
the model needs a scoring-rate assumption the feed cannot supply.

Either way the gate now refuses anything a model gets wrong in a nameable way, which
is the point of having built it first.

---

Full finding list: `docs/POLYMARKET-API-CONFORMANCE.md` (86 findings).
Venue surface map and the method for not misreading it:
`docs/POLYMARKET-SURFACE-AUDIT.md` — 9 hosts, 223 operations, plus
`make audit-surface`.
Build order and per-item notes: `docs/ROADMAP.md`.
Design decisions: `docs/ARCHITECTURE.md`, `docs/ADR-000{1,2,3}-*.md`.
