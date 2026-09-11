# Build order

The skeleton is step 0. Each step below is independently testable, and the
ordering is a dependency order — nothing here is optional scaffolding.

## Phase 1 — Data spine
1. `PolymarketSession` — client construction, auth, lifecycle
2. `mapping.py` — SDK → domain, against real payloads
3. `SdkMarketDiscovery`, `ClobMarketData`
4. `PolymarketStreams` — reconnection with explicit gap markers
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
12. `FootballEngine`, `CricketEngine`, `BadmintonEngine`
13. `Btc5mEngine` — reference price handling is the whole problem
14. Calibration fitting (`BaseProbabilityEngine._calibrate`)

**Done when:** engines produce calibrated probabilities and abstain correctly
on missing state.

## Phase 4 — EV and safety
15. `EvEngine` — price we would actually pay, never the mid
16. `MicrostructureEngine`
17. All 15 `SafetyGate` checks wired
18. `RiskEngine`, `ExposureTracker` (correlation grouping included)
19. `JournalRecorder`

**Done when:** the system produces signals and rejections with full reasoning,
and still sends nothing.

## Phase 5 — Execution
20. `OrderManager` — uncertain-outcome handling first, before the happy path
21. `ExecutionEngine`, `PaperExecutor`
22. `Reconciler`
23. `CircuitBreakerRegistry` wired to real triggers
24. `PolymarketExecution`, `PolymarketRelayer`

**Done when:** paper trading runs end to end and survives an induced
kill/restart mid-order without losing or duplicating a position.

## Phase 6 — Positions and intelligence
25. `PositionManager`, `ExitEngine` — noise band before exit triggers
26. `SmartMoneyEngine` — detection, then scoring
27. `EventPipeline`, `GeopoliticalEngine`, `PoliticalEngine`
28. `CrossMarketEngine` (stays disabled until backtested)

## Phase 7 — Dashboard
29. Auth, then the read-only panels
30. Risk controls with typed confirmation and audit
31. WebSocket push
32. Next.js frontend

## Phase 8 — Validation before live
33. `BacktestRunner` with no-lookahead enforcement
34. Calibration curves per engine — the headline output, not P&L
35. Failure-injection suite (the list in ARCHITECTURE.md)
36. Extended paper run, then shadow run
37. Limited live with minimum size, then gradual scaling

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
- [ ] dashboard auth and audit verified
- [ ] position/exposure/loss limits verified by forcing each one
