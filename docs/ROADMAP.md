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
5. `FeatureEngine.assess_quality` — freshness before anything trades on it
6. Repositories + Alembic migrations

**Done when:** the system discovers markets, streams books, persists snapshots,
and correctly reports stale data. No trading logic yet.

## Phase 2 — Classification and validation
7. `MarketClassifier` — calibration at the low end matters more than accuracy
8. `ResolutionValidator` — the highest-value safety component in the system
9. `DiscoveryService` — lifecycle transitions and rejection journalling

**Done when:** every discovered market reaches `MONITORED` or a documented
rejection.

## Phase 3 — Probability
10. `FeatureEngine.compute` — microstructure
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
