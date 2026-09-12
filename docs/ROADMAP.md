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
   its reason, and nothing advances past `CLASSIFIED` because the validator does not
   exist yet. Live: 400 markets, 393 classified, 7 rejected (1.8%)

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
11. `TennisEngine` first: the cleanest analytic model, so the engine
    scaffolding gets validated against a model that can be checked exactly
12. `FootballEngine` — the venue feed supplies score, period and clock only, so
    build the score-and-clock model first and treat xG/shots/cards as a later
    upgrade gated on a third-party feed
13. `CricketEngine`, `BadmintonEngine` — **blocked**: no venue-native state feed
    exists for either sport. Needs an external data source before it is worth
    writing the model (see `docs/POLYMARKET-API-CONFORMANCE.md` §5)
14. `Btc5mEngine` — reference price handling is the whole problem; confirm
    against live markets whether the cadence is 5 or 15 minutes, and which feed
    (Binance spot vs Chainlink TWAP 30/60 s) each market settles against
15. Calibration fitting (`BaseProbabilityEngine._calibrate`)

**Done when:** engines produce calibrated probabilities and abstain correctly
on missing state.

## Phase 4 — EV and safety
16. `EvEngine` — price we would actually pay, never the mid, and the taker fee
    from the market's own `fee_schedule` (`EvEngine.fee_bps`) rather than zero
17. `MicrostructureEngine`
18. All 15 `SafetyGate` checks wired
19. `RiskEngine`, `ExposureTracker` (correlation grouping included)
20. `JournalRecorder`

**Done when:** the system produces signals and rejections with full reasoning,
and still sends nothing.

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
