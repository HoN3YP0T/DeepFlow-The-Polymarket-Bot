# Build order

The skeleton is step 0. Each step below is independently testable, and the
ordering is a dependency order — nothing here is optional scaffolding.

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
    is "not seen yet" rather than proven absent
14. `Btc5mEngine` — **unblocked, unwritten.** The markets exist: one per asset every
    five minutes across eight assets, plus a 15-minute variant, so the cadence
    question is answered as *both* (§53). Classification now reaches `BTC_5M` via the
    venue's own `5M` tag and a correctly measured contest window (§54–55). What
    remains is the model. The feed question is answered (§63): these markets resolve
    on a **Chainlink TWAP**, with a 30-second lookback for 5-minute markets and 60
    seconds for 15-minute and 4-hour ones, and both the price to beat and the
    settlement price come from that feed — so pricing off Binance spot would be wrong
    by the spot-to-TWAP basis at precisely the horizon where that basis is the edge
15. Calibration fitting (`BaseProbabilityEngine._calibrate`)

**Done when:** engines produce calibrated probabilities and abstain correctly
on missing state.

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
21. `OrderManager` — uncertain-outcome handling first, before the happy path
22. `ExecutionEngine`, `PaperExecutor`
23. `Reconciler`
24. `CircuitBreakerRegistry` wired to real triggers
25. `PolymarketExecution`, `PolymarketRelayer` — including the venue rules the
    conformance review surfaced: tick/size rounding before submission, `delayed`
    acceptance handled as pending, 425 and post-only windows waited out rather
    than tripping a breaker, and settlement followed to `CONFIRMED`
26. Venue order heartbeat (`start_order_heartbeat`) — the only safety mechanism
    that survives this process dying, so it lands with execution, not after it

**Done when:** paper trading runs end to end and survives an induced
kill/restart mid-order without losing or duplicating a position, and a killed
process leaves no resting orders behind.

## Phase 6 — Positions and intelligence
27. `PositionManager`, `ExitEngine` — noise band before exit triggers
28. `SmartMoneyEngine` — detection, then scoring
29. `EventPipeline`, `GeopoliticalEngine`, `PoliticalEngine`
30. `CrossMarketEngine` (stays disabled until backtested)

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
