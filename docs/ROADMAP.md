# Build order

The skeleton is step 0. Each step below is independently testable, and the
ordering is a dependency order — nothing here is optional scaffolding.

**As of 2026-09-14:**

| Phase | State | Note |
| --- | --- | --- |
| 1 · Data spine | ✅ complete | verified live end to end by `verify_phase1.py` |
| 2 · Classification and validation | ✅ complete | 300 live markets, 0 unresolved |
| 3 · Probability | 5 of 6 | `Btc5mEngine` and calibration done; `FootballEngine` is the last item |
| 4 · EV and safety | ✅ complete | decides in full, on injected probabilities |
| 5 · Execution | ✅ complete | reads verified live; **every write unverified** — no order has been submitted |
| 6 · Positions and intelligence | 3 of 4 | exits, positions, smart money, events; cross-market deferred |
| 7 · Dashboard | not started | 24 API stubs; frontend is scaffolding |
| 8 · Validation before live | not started | |

The pipeline is finished at both ends and hollow in the middle: everything up to the
probability engines works, everything after them works, and the engines themselves
are stubs. Current state in detail: `docs/STATUS.md`.

## Phase 1 — Data spine

Verified vertical slice first (`scripts/verify_slice.py`): session → discovery →
mapping → book, run against the live venue. It caught two things the docs and the
SDK signatures both hid — see `docs/POLYMARKET-API-CONFORMANCE.md` §20-21.

1. ~~`PolymarketSession` — client construction, auth, lifecycle~~ **done**
2. ~~`mapping.py` — SDK → domain, against real payloads~~ **done** (real payloads
   recorded in `tests/fixtures/polymarket_payloads.json`)
3. ~~`SdkMarketDiscovery`, `ClobMarketData`~~ **done** — batched books are keyed
   by `asset_id`, never zipped positionally (§22); `page_size` capped at the
   venue's real maximum of 100 (§23)
4. ~~`PolymarketStreams` — reconnection with explicit gap markers~~ **done** —
   one connection fanned out into per-feed queues; `price_change` folded as level
   replacement, cross-checked against the venue's reported touch (§24)
5. ~~`FeatureEngine.assess_quality` — freshness before anything trades on it~~
   **done** — freshness is feed liveness, not last change (§27); consistency
   checked before age; a future timestamp is inconsistent, never fresh
6. ~~Repositories + Alembic migrations~~ **done** — markets, snapshots and orders
   implemented and verified against real PostgreSQL; composite primary keys so the
   time-series tables can actually become hypertables (§28); conditional Timescale
   conversion so one migration serves plain PG and Timescale alike. Positions and
   the journal stay stubbed until the phases that write them

**Done when:** the system discovers markets, streams books, persists snapshots,
and correctly reports stale data. No trading logic yet.

**Verified** by `scripts/verify_phase1.py`, which runs the whole chain live against
the venue and a real database rather than checking each module separately. Last
run: 6 markets discovered and persisted, 82 snapshots streamed and folded, 164
rows written and read back, all FRESH, and the same book correctly reported STALE
when the clock is advanced past the budget.

## Phase 2 — Classification and validation
7. ~~`MarketClassifier` — calibration at the low end matters more than accuracy~~
   **done** — venue tag ids primary, seeded from `get_sports()` (§30-31); a sport tag
   alone does not mean a game (§32); related-category ties resolve by specificity
   (§33). Live: 360 markets, 1.9% UNKNOWN
8. ~~`ResolutionValidator` — the highest-value safety component in the system~~
   **done** — two resolution shapes parsed (§36), typographic quotes normalised
   (§37), tiered verdicts with only VALID tradeable. Calibrated on 360 live markets:
   47% VALID, 36% AMBIGUOUS, 17% UNPARSEABLE
9. ~~`DiscoveryService` — lifecycle transitions and rejection journalling~~
   **done** — sweeps are idempotent, every transition and refusal is journalled with
   its reason, and `CLASSIFIED → VALIDATED → MONITORED` runs as two separate edges so
   "rules read and accepted" is recorded distinctly from "we are watching it". Live:
   400 markets, 393 classified, 7 rejected (1.8%); on a second run against a real
   database, 300 markets → 161 monitored, 139 rejected, 0 unresolved.

   *(This entry previously ended "nothing advances past `CLASSIFIED` because the
   validator does not exist yet" — written before item 8 landed and left standing
   after it did.)*

**Done when:** every discovered market reaches `MONITORED` or a documented
rejection.

**Verified** on 300 live markets against a real database: 161 monitored, 139
rejected with a recorded reason, **0 unresolved**.

The headline number to keep in view: roughly half of all markets are held back, and
most of those are `AMBIGUOUS` rather than unreadable. That is the safe direction, and
improving parser coverage is the largest single lever on how many markets this system
can ever trade — see `docs/POLYMARKET-API-CONFORMANCE.md` §36-38.

## Phase 3 — Probability
10. ~~`FeatureEngine.compute` — microstructure~~ **done** — depth measured inside a
    band around the touch, never whole-book (§39); imbalance withheld when its sign
    is not robust across bands; slippage `None` rather than a partial walk
11. ~~`TennisEngine` first~~ **dropped** — tennis is the cleanest model on paper and
    the least supported in practice: the feed sends games-in-current-set and never the
    set score, so the same payload describes a match nearly won and one nearly lost
    (§41). Rules parse it; `is_modellable` is False with the reason named.
    Per-sport rule modules landed instead — soccer, gridiron, tennis, esports —
    resolving 22/22 captured leagues (§40, §43)
11b. ~~The market-to-live-game join~~ **done** — `list_events(game_ids=...)` on the
    event, plus a `live=True` sweep that returns every in-play fixture with its
    state and markets in one request (§46, §48). Wired into the orchestrator as the
    `live-games` task, which resolves each fixture's sport through `SportRegistry`
    and parses it with that sport's rules; `scripts/verify_game_join.py` proves it
    against the live venue. Tradeable types gated to `moneyline` and
    `child_moneyline` — 2 of the venue's 240
12. `FootballEngine` — the venue feed supplies score, period and clock only, so
    build the score-and-clock model first and treat xG/shots/cards as a later
    upgrade gated on a third-party feed
13. ~~`CricketEngine`~~ **resolved as a structural abstention** — `rules/cricket.py`
    is written and parses cricket faithfully; the model is not, and should not be.
    The feed gives runs and the innings phase and never wickets or balls remaining,
    so a chase cannot be placed from it (§57). Cricket now behaves like tennis: it
    resolves, parses, and abstains with the missing state named, instead of being
    absent on a claim that was wrong three times (§5 → §34 → §49 → §56).
    `BadmintonEngine` stays blocked — no live state observed on either source, which
    is "not seen yet" rather than proven absent.

    *(Caveat on "resolved as a structural abstention": it is true in `rules/`, where
    `is_modellable` is False with the reason named. The engine classes themselves still
    raise `NotImplementedError` and `TennisEngine`'s docstring still describes a model
    that does not exist. They are unreachable — nothing registers them — so this costs
    nothing today, but it is the `is_modellable` / `sports_feed` pattern a third time.)*
14. ~~`Btc5mEngine`~~ **done and verified live** — the first model here that produces a
    number rather than consuming an injected one. These markets resolve on a
    **Chainlink TWAP** (§63), and the window is **60 s for every cadence** per each
    market's own resolution text, which retracts the changelog's 30 s for 5-minute
    markets (§85). So `T_eff = T - 2w/3`, the last 60 s of every window is unpriceable,
    and warm-up is ~12 minutes. No up/down market publishes its strike — it is the
    reference price at the window's opening instant, stated only in prose (§77) — so a
    process not already subscribed when a window opened cannot price it, and abstains.

    *(This entry previously read "unblocked, unwritten" and repeated the 30 s figure.
    Both were left standing after the work landed and after §85 retracted the number.)*
15. ~~Calibration fitting (`BaseProbabilityEngine._calibrate`)~~ **done** — and it was
    not merely unstarted, it was **unstartable**. `signals` stored a model probability
    and nothing in the schema had ever recorded how a market resolved, so only half of
    each (prediction, outcome) pair existed and no amount of running would have produced
    a curve. Three tables now close it: `predictions` (every estimate, not only the
    traded ones — calibrating the traded subset fits the curve to the region the system
    already believed in), `market_resolutions` (per-outcome payouts from `/v2/resolutions`,
    which is the authoritative source; `outcomes.*.price` reads 0 on *both* sides of a
    settled market and `market.resolution` is entirely `None` — §89), and
    `calibration_fits`. Isotonic by pool-adjacent-violators rather than Platt, because
    the errors expected here are band-specific and a sigmoid cannot represent that
    without distorting the region it was right about. Two guards against the error that
    would otherwise make this worthless: predictions are sampled at one row per token per
    minute, and the fitter counts **distinct markets**, not rows — 500 estimates on three
    5-minute windows is three coin flips (§90). Fits are stored but never self-activate

**Done when:** engines produce calibrated probabilities and abstain correctly
on missing state.

**Calibration is wired but every engine still runs on identity, and that is the correct
state rather than an unfinished one.** A curve is only installed when an operator marks a
fit active, and no fit can exist until settled markets have accumulated behind recorded
predictions. The floors are 200 samples from 50 distinct markets. What to watch on the
health line is `predictions` climbing with `settled` following it; `settled` pinned at zero
while `predictions` rises is the one shape that means scoring is broken rather than waiting.

One number from that work is worth carrying into every engine written from here: in the
0.95-1.00 band, across 91 settled markets, the crowd said **0.991** and delivered **0.989**
(§90). That is the baseline to beat in precisely the band this system targets.

**Unblocked, 2026-09-13.** This previously read "blocked, not merely unstarted", on the
claim that no market-to-live-game join existed. That was wrong — see
`docs/POLYMARKET-API-CONFORMANCE.md` §45 (retraction) and §46–52. The join is
`list_events(game_ids=…)` on the event, built in `adapters/polymarket/games.py` and
verified live against every fixture streaming at the time.

What a sports engine now has to attach to: in-play fixtures with score and period from
one request, their open `moneyline` and `child_moneyline` markets, and per-sport rules
that are already done and tested. What it still lacks is the probability model itself —
so the item is ordinary unstarted work, and the recommendation to do Phase 4 first
stands on Phase 4's own merits (EV arithmetic is verifiable today), not on a blocker.

Cricket is also no longer feed-blocked (§49); tennis still is, pending a live fixture
to check `sets_won_by_each_player` against.

## Phase 4 — EV and safety
16. ~~`EvEngine`~~ **done** — `market_probability` is the ask crossed, never the mid,
    so the edge is net of the half-spread by construction; `spread_cost_bps` is
    therefore zero *and says why*, because charging it again would reject profitable
    trades. Costs are bps of notional and the edge is payoff units, converted in one
    place. A book too thin to fill the size is no assessment rather than a bad one
17. ~~`MicrostructureEngine`~~ **done in Phase 3 (item 10)** — listed twice in the
    original plan; kept here as a pointer rather than silently dropped
18. ~~All 15 `SafetyGate` checks wired~~ **done, as 17** — the fifteen specified plus
    `BOOK_CLEARED_AT_START` and `REFERENCE_FEED_MATCHED`, from findings 63-64. Checks
    are pure functions over a `GateContext`; **a missing input fails the check that
    reads it**, so a context assembled by a forgetful caller refuses and names the
    gap rather than approving. `default_gate()` registers all 17
19. ~~`RiskEngine`, `ExposureTracker`~~ **done** — six independent vetoes in a fixed
    order, portfolio stops before sizing so a system in drawdown never computes a
    position size; a rejection still reports the stake it *would* have taken, which
    is the only record of whether a limit binds meaningfully or strangles everything.
    Correlation grouping is three tiers (explicit key → shared event → the market
    itself), and an unknown market is its own group rather than pooled or exempt.
    Exposure is cost basis, never mark value: marking to market frees capacity as a
    position moves in our favour, concentrating the book exactly when it feels safest
20. ~~`JournalRecorder`~~ **done** — entries, rejections, holds and exits, each with
    the full gate result set rather than only the failures, cost terms individually,
    both probabilities, and `run_mode` on every row. A write failure is logged and
    never raised: the journal is a record, not a safety mechanism, so losing a row
    costs analysis while a propagating insert error could abandon an exit halfway.
    `SqlJournalRepository.record_signal` implemented alongside, routed through the
    same stream so signals and decisions stay interleaved

**Done when:** the system produces signals and rejections with full reasoning,
and still sends nothing. ✅ **Complete 2026-09-13.** EV, 17 gate checks, risk,
exposure and the journal are implemented and tested; nothing in the process can
place an order, and LIVE mode still refuses to start.

**What Phase 4 does not do.** It decides, and nothing produces a probability for it
to decide on -- the sports and crypto models are Phase 3 items 12-15. Every path here
is tested against injected probabilities, which is the honest scope: the arithmetic
and the refusals are verifiable today, the models are not.

## Phase 5 — Execution
21. ~~`OrderManager`~~ **done** — the uncertain path first: an `ExecutionUncertainError`
    ends submission immediately as `UNKNOWN`, with no retry, because a timeout means
    the order *may* be live. Only a definitive rejection retries. A reprice submits
    the unfilled **remainder** under a **fresh client key**, and only after the
    cancellation is confirmed. Two bugs found while building it: reprice re-entered
    the working loop with the attempt counter reset (bounded nothing, recursed
    forever), and the poll loop trusted the clock alone — a stopped clock would have
    polled an order indefinitely, so it now has a poll budget as well
22. ~~`ExecutionEngine`, `PaperExecutor`~~ **done** — the limit price is the signal's
    target walked by the **assessed** slippage, so the order and the arithmetic that
    approved it describe the same trade; prices and sizes snap to the venue grid
    conservatively (both roundings from the price tick, which is what the venue
    derives size precision from) and the idempotency key is taken from the *rounded*
    intent, so two intents the venue sees as identical cannot produce two orders.
    Paper fills walk the real book at the same VWAP the EV engine priced,
    partial-fill when depth runs out, charge the market's own fee schedule, and
    **refuse to invent a fill** with no observed book. `ShadowExecutor` validates
    what is checkable without credentials and records every suppressed order
23. ~~`Reconciler`~~ **done** — `resolve_uncertain_order` returns one of four
    outcomes, and **only a positive "the venue does not have it" permits a
    re-intend**: a failed lookup is `UNRESOLVED`, not `ABSENT`, because being wrong
    that way costs a missed trade while the other costs twice the position.
    `MATCHED_UNSETTLED` counts as filled for this purpose. Full reconciliation diffs
    balance, orders and positions in **both** directions — an orphan order at the
    venue is a live commitment appearing in no local exposure figure, which is
    exactly what a process killed mid-submission leaves behind. An unreadable venue
    is a discrepancy, never a pass
24. ~~`CircuitBreakerRegistry` wired to real triggers~~ **done** — the registry was
    already the halt *authority*; `BreakerSupervisor` is the half that observes and
    decides, so the registry can be consulted without importing streams, orders,
    bankrolls and the database. Rate conditions use a **sliding hour**, not a
    lifetime counter — twenty reconnects in a month is healthy and twenty in an hour
    is a broken feed, and a counter that cannot tell them apart teaches operators to
    ignore the breaker. Wired into the orchestrator's health loop, which feeds
    reconnects as *deltas* and trips the database breaker on a persist failure.
    Nothing in the supervisor resets a breaker: they latch, and each flap of a
    self-rearming breaker is a window in which entries are allowed again
25. ~~`PolymarketExecution`, `PolymarketRelayer`~~ **done, unverified** — reads are
    verified against a live account (§70); **no order has ever been submitted**, no
    approval granted and no heartbeat posted, so every write is written from the
    published spec and the installed SDK models. Reading them closely produced four
    findings, each of which would have been a live bug: a rejection arrives as a
    *return value* and the venue sends `success: true` beside an `errorMsg` (§74);
    `"context canceled"` comes back as **400**, the status meaning "definitively
    refused", when it is the one case that must never be retried (§71);
    `place_limit_order` hides an on-chain approval **and a re-post**, so submission
    now signs and posts explicitly (§73); and a BUY's filled shares are
    `takingAmount` while a SELL's are `makingAmount`, both in 6-decimal fixed math
    whose scaling is checked rather than trusted, since a fill that exceeds its order
    goes to reconciliation instead of the books (§74). The relayer re-reads the
    approval state rather than believing `setup_trading_approvals`, whose handle is
    deprecated and returns immediately, and redeems one condition per call because
    the venue has no batch form
26. ~~Venue order heartbeat~~ **done, unverified** — `POST /v1/heartbeats` is a real
    CLOB route the SDK does not wrap; its only heartbeats are WebSocket keepalives
    and the perps auto-cancel is a different surface (§72). Posted through the
    authenticated transport so the L2 signature covers the exact wire body, with the
    expected id adopted from the 400 that rejects a stale one — otherwise one dropped
    response ends order protection permanently and the only symptom is a log line
    every five seconds

**Done when:** paper trading runs end to end and survives an induced
kill/restart mid-order without losing or duplicating a position, and a killed
process leaves no resting orders behind.

**Not done, and deliberately so:** the venue-facing half of that sentence. Confirming
it needs a funded, approved account and a real order — a single minimum-size order on a
liquid market would exercise submit, the response mapping, the heartbeat and a cancel in
one pass. Until then the writes are code, not behaviour.

## Phase 6 — Positions and intelligence
27. ~~`PositionManager`, `ExitEngine`~~ **done** — a volatility-scaled noise band before
    any price-derived exit, because at 0.85-0.98 the spread is a large fraction of the
    edge and exiting on a wobble realizes a loss repeatedly. A verified state change
    bypasses the band entirely and is the only input that may trigger an emergency exit.
    Three bugs found in the writing: EMERGENCY_EXIT was unreachable because nothing
    populated `state_change_detected` (the same failure as `games.py`); a negative-EV
    position scored below the partial threshold and *held*, so negative EV is now its own
    exit condition rather than a contribution to a score; and a HOLD reason claimed a
    move was outside the band when an EV bypass was what got it there
28. ~~`SmartMoneyEngine`~~ **done** — detection and scoring kept apart, because a wallet
    is noticed for being large and counted for being right. Verifying the adapter live
    found §75: closed positions come back sorted by realized PnL **descending**, so one
    page of a prolific wallet is its 100 best trades — a wallet down 964 USDC showed 100
    of 100 winners. Every wallet with 100+ resolved positions would have scored a perfect
    hit rate. Now sorted by timestamp; that wallet reads 85 of 100 and is the scorer's
    calibration case, since many small wins against a few large losses is exactly the
    profile a high-probability strategy must not copy
29. ~~`EventPipeline`, `GeopoliticalEngine`, `PoliticalEngine`~~ **done, and abstaining
    by design** — corroboration counted across a six-hour window on distinct
    *publishers*, since two stories from one newsroom are one witness; reliability taken
    as the best source rather than a sum or an average; markets matched on parsed
    resolution criteria with a required entity overlap and an outright exclusion on
    non-qualifying clauses. Both engines need a **sourced** `BaseRate` and nothing
    constructs one, and no feed calls `ingest`, so in production they always abstain —
    the correct answer for an engine with no evidence. See `docs/STATUS.md`
30. `CrossMarketEngine` — **deferred** at the owner's request; stays disabled until
    backtested

## Phase 7 — Dashboard
31. Auth, then the read-only panels
32. Risk controls with typed confirmation and audit
33. WebSocket push
34. Next.js frontend

## Phase 8 — Validation before live
35. `BacktestRunner` with no-lookahead enforcement
36. Calibration curves per engine — the headline output, not P&L
37. Failure-injection suite (the list in ARCHITECTURE.md)
38. Extended paper run, then shadow run
39. Limited live with minimum size, then gradual scaling

## Gate to live

All of the following, no exceptions:

- [ ] full test suite green, including every failure-injection case
- [ ] `mypy --strict` and `ruff` clean
- [ ] backtest shows positive net EV **after** realistic fills
- [ ] calibration curves within tolerance for every enabled engine
- [ ] paper run sustained, with reconciliation clean throughout
- [ ] shadow run: every order accepted as well-formed
- [ ] kill/restart mid-order recovers with no duplicate or lost position
- [ ] every circuit breaker fires under induced failure
- [ ] taker fees priced from each market's own `fee_schedule`, never assumed zero
- [ ] exchange approvals granted for **both** exchanges and synced to the CLOB
- [ ] order heartbeat armed, and verified to cancel resting orders on process kill
- [ ] dashboard auth and audit verified
- [ ] position/exposure/loss limits verified by forcing each one
