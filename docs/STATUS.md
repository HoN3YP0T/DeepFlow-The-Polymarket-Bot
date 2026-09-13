# Status and handover

**As of:** 2026-09-13 · **Branch:** `claude/adoring-hypatia-pxi7up`
· 21 commits · **418 tests passing, 0 skipped** · **99 stubs remain** · 62 findings recorded

An earlier version of this line read "395 tests green" while 30 of those were
integration tests **skipped** for want of a database — a skipped test reporting as
green is how an unverified persistence layer passes review. `make db-local` starts
the local Postgres they need; the number above is with them actually running.

Phase 1 complete · Phase 2 complete · Phase 3 partially done · Phases 4–8 not started

---

## 1. What the process does today

`make run` sweeps the market catalogue, classifies and validates each market, folds
the live order-book stream, writes snapshots, and reports its own feed health.

```
orchestrator.starting   mode=PAPER not_yet_wired=['signal loop', 'reconciliation', ...]
discovery.swept         markets=10 tokens=20
streams.connected       tokens=20
orchestrator.health     connected=True reconnects=0 dropped=0 snapshots_written=336
orchestrator.stopped    snapshots_written=500
```

**It cannot trade, structurally.** `Orchestrator.start` rejects `LIVE` before any
socket opens, because reconciliation and the safety gate do not exist. There is no
bypass.

### Operating it

```bash
make up          # postgres + redis          make check   # lint, typecheck, tests
make migrate     # apply migrations          make run     # discover, stream, persist
```

Verification scripts, all runnable without credentials: `scripts/verify_slice.py`,
`scripts/verify_stream.py`, `scripts/verify_phase1.py`.

---

## 2. Done, and verified live

### Phase 1 — data spine (complete)

| Component | Verified by |
|---|---|
| `sdk_client.py` — session lifecycle, refuses a signing client without key **and** wallet | live |
| `mapping.py` — SDK → domain, the single anti-corruption layer | recorded real payloads |
| `discovery.py` — tradeable filtering, server-side liquidity floor, page cap | live |
| `clob.py` — books keyed by `asset_id`, midpoint, spread, trades, book walk | live, 16/16 vs REST |
| `streams.py` + `book_state.py` — one socket fanned out, folding, gap marking, drift detection | 16/16 books matched REST after 420 folded changes |
| `features.assess_quality` — FRESH / DEGRADED / STALE / INCONSISTENT | live, 276/276 FRESH |
| `persistence/` — markets, snapshots, orders; conditional Timescale migration | real PostgreSQL |
| `venue.py` — fee formula, tick grid, GTD arithmetic, failure classification | published tables |
| `orchestrator.py` — Phase 1 task set, supervision, ordered shutdown | 500 rows in a 40s run |

### Phase 2 — classification and validation (complete)

| Component | Result on live markets |
|---|---|
| `classifier.py` — venue tag ids primary, seeded from `get_sports()` | 360 markets, 1.9% UNKNOWN |
| `resolution.py` — two resolution shapes, tiered verdicts | 46.9% VALID / 36.4% AMBIGUOUS / 16.7% UNPARSEABLE |
| `discovery.py` (pipeline) — lifecycle, journalled rejections, idempotent sweeps | 300 markets: 161 monitored, 139 rejected, **0 unresolved** |
| `SqlJournalRepository` — decision log | real PostgreSQL |

### Phase 3 — probability (3 of 6 items)

| Item | State |
|---|---|
| 10 · `FeatureEngine.compute` + `MicrostructureEngine` | **done** — banded depth, robustness check, slippage |
| 11 · `TennisEngine` | **dropped** — feed cannot support it (§41) |
| — · Per-sport rule modules (added, not in the original plan) | **done** — soccer, gridiron, tennis, esports; 22/22 leagues resolved |
| — · Market↔live-game join (added) | **done and wired** — `games.py`, the `live-games` orchestrator task, `verify_game_join.py` (§46–48) |
| 12 · `FootballEngine` | **not started** — no longer blocked; the join it needed exists |
| 13 · `CricketEngine` | **resolved as a structural abstention** — `rules/cricket.py` parses cricket; no model, because runs without wickets or balls cannot place a chase (§57). `BadmintonEngine` still has no observed state source |
| 14 · `Btc5mEngine` | **not started** — no short-dated crypto markets found open |
| 15 · Calibration fitting | **not started** |

**Both Phase 3 deliverables were islands until 2026-09-13.** `games.py` and
`engines/sports/rules/` were each written, tested and verified against the live
venue, and neither was reachable from the running process — so the pipeline had a
probability layer it could not feed, while every individual test passed. They are
now joined by `CATEGORY_BY_SPORT` (the `SportKind`↔`MarketCategory` translation that
was missing entirely) and driven by the orchestrator's `live-games` task. Wiring
them immediately surfaced a gap no unit test could: the REST index reports soccer
full time as `VFT`, which the socket-derived rules did not recognise.

---

## 3. Incomplete inside work already called "done"

This section exists because "phase complete" does not mean "nothing missing".

**`venue.py`** — `taker_fee` supports only `exponent=1`, the single value observed.
A market with any other exponent gets an approximation via float conversion.

**`OrderBook` / books** — no tick-size validation at construction. `venue.py` can
round to a tick but nothing forces a price onto the grid before it reaches execution.

**`streams.py`** — three feeds still stubbed: `subscribe_crypto_prices`,
`subscribe_crypto_twap`, `subscribe_user`. The user stream is Phase 5's dependency.
Also: the SDK opens a second TCP connection for the sports socket, so "one connection"
describes our fan-out, not the transport.

**`clob.py`** — `get_last_trades` makes two calls, fetching a book purely to learn the
`condition_id` we already hold on the `Market`. Wasted round trip.

**`persistence/`** — `SqlPositionRepository` (3 methods) and
`SqlJournalRepository.record_signal` still stubbed. The hypertable conversion has
never run: TimescaleDB is not installable in the build container, so the composite
primary keys follow the documented requirement but the conversion itself is unproven.
CI now uses the Timescale image, so the next push exercises it.

**`classifier.py`** — 183 of the venue's 465 leagues fall outside the reliable tag
ids (hockey, lacrosse, minor codes) and resolve only if their period vocabulary
matches. `WAR_CONFLICT`, `CEASEFIRE` and `MILITARY_DIPLOMATIC` categories exist with
keyword seeds but no tag ids and no engine.

**`resolution.py`** — plateaued at ~47% VALID. Roughly half of all markets are held
back, dominated by "deadline has no explicit timezone" (75) and judgement-call markers
(56). Improving this is the single largest lever on how many markets the system can
ever trade, and a regex validator is near its limit.

**`pipeline/discovery.py`** — `MONITORED` is terminal for now: nothing re-validates a
market whose rules change, nothing removes a market that closes, and a market that
goes stale stays monitored. No `MONITORED → CANDIDATE` step exists.

**`orchestrator.py`** — reconciliation is skipped (nothing can have ordered yet) and
health is a log line, not a breaker. The sports **socket** is still not subscribed;
in-play state now comes from the `live-games` REST sweep instead, which is the
cold-start-correct source (§48) and is wired.

**`sports_feed.py`** — the `League` enum, `SUPPORTED_LEAGUES`, `STATUS_VALUES` and
`PERIOD_MEANINGS` are **deleted** as of 2026-09-13. Nothing in `src/` referenced any
of them, and the justification recorded here — keep `League` because `STATUS_VALUES`
needs it — was circular, since `STATUS_VALUES` was referenced by nothing at all.
`PERIOD_MEANINGS` was also wrong: it listed `1Q`–`4Q` where the feed sends `Q1`–`Q4`.
The module docstring additionally claimed an engine registry used these to refuse
unsourced engines; no registry did.

**`engines/base.py`** — `_calibrate` is the identity function. Every probability the
system produces will be uncalibrated until item 15, and in the 0.85–0.98 band a model
that says 0.97 and is right 0.93 of the time turns a positive edge negative.

**`safety_gate.py`** — the 15 `CheckId` values are declared; **none are implemented**.
`GateContext` is a stub.

---

## 4. Everything not started

**Phase 4 — EV and safety (0 of 5).** `EvEngine.assess` / `_costs` /
`_market_probability`; all 15 safety checks; `RiskEngine.approve`;
`ExposureTracker` (3 methods); `JournalRecorder` (4 methods).

**Phase 5 — execution (0 of 6).** `OrderManager.execute` / `reprice`;
`ExecutionEngine` (3); `Reconciler` (2); `CircuitBreakerRegistry` wiring;
`PolymarketExecution` (10 methods) and `PolymarketRelayer` (2); the order heartbeat;
`PaperExecutor` / `ShadowExecutor`.

**Phase 6 — positions and intelligence (0 of 4).** `PositionManager` (4);
`ExitEngine` (2); `SmartMoneyEngine` (3); `DataApiWalletIntel` (5);
`EventPipeline` (2); `PoliticalEngine`; `GeopoliticalEngine`; `CrossMarketEngine` (2).

**Phase 7 — dashboard (0 of 4).** 24 API stubs across auth, health, overview,
positions, risk controls, strategies, trades, whales, journal and the WebSocket.
The Next.js frontend is scaffolding only.

**Phase 8 — validation (0 of 5).** `BacktestRunner.run`, `BacktestExecutor.submit`;
calibration curves; failure-injection suite; paper then shadow run; limited live.

**Infrastructure.** `RedisCache` (3 methods) — connect, close, distributed lock.

---

## 5. Blockers that code cannot solve

**Retracted, 2026-09-13: the market ↔ live-game join does exist.** This section
previously listed it as unsolvable. That was wrong, and it was wrong because the API
surface was never read — the `gamma-openapi.yaml` spec was in the docs index the
whole time. Corrected in `docs/POLYMARKET-API-CONFORMANCE.md` §46–52.

The join is `list_events(game_ids=…)` — on the **event**, not the market. Verified
live twice: 9 of 9 fixtures streaming on the sports socket resolved to their events
(15 tradeable markets with live asks), and again 8 of 8 on a later run. Reproduce with
`scripts/verify_game_join.py`.

Why three probes all missed it:

1. Looked on the **market**. `market.sports.game_id` is a *different id space* — the
   child contest (`283508` for game 1 of a series whose fixture is `1642158`), and
   `None` on fixture-level markets. So `list_markets(game_id=<fixture id>)` returns
   nothing while looking correct.
2. `streams.py` documented the wrong place to look — "match on `game_id` against
   `Market.game_id`" — so the mistake was written down as the design.
3. Filtered on `start_date_*` (when the *market* opened) instead of `start_time_*`
   (kickoff). Sports markets open weeks early, so a "kickoff within −3h..+24h" window
   expressed against `start_date` excludes nearly every live fixture. That is the
   whole of the "0 of 600 markets" figure previously reported here.

**Also retracted: cricket has no live state — and then twice more.** An international
fixture was observed live with `score="74-100"`, `period="Live"` and open markets.
Cricket has now been mis-described three times, each correction smaller than the last:
"no data source" (§5) → "no in-play feed" (§34) → "no `game_id`" (§49/§56). The id
exists; it is a *string* in `eventMetadata.gameId` (`"1000169067LIVE2026"`), and the
SDK types the numeric field `int | None`, so reading only that field concludes the
sport is unidentifiable.

What is actually and durably true is narrower: cricket gives **runs and the innings
phase, and never wickets, overs or balls remaining** — neither spec mentions them
(§57). A side needing 100 with two overs and one wicket is nearly beaten; needing 100
with ten overs and eight wickets it is comfortable, and the feed says the same thing
in both cases. So `rules/cricket.py` parses cricket and declares those two gaps
blocking, exactly as tennis does with the set score. Cricket resolves, parses, and
abstains with a named reason — which is what it should have done from the start.

**Also retracted: a sports data feed must be bought.** One request —
`list_events(live=True, closed=False)` — returns every in-play fixture with score,
period and its open markets: 15 events and 278 open, order-accepting markets at the
time of measurement. For `moneyline` and `child_moneyline` that is the entire model
input. A paid feed buys only what the venue does not publish (xG, shot data,
per-player state) and is therefore a *widening* decision, not a prerequisite.

### What remains genuinely blocked

**Retracted, 2026-09-13: short-dated crypto markets exist.** This section previously
claimed a 900-market scan found none. The venue lists a 5-minute up/down market **per
asset, every five minutes** — eight assets running simultaneously (BTC, ETH, XRP, SOL,
DOGE, BNB, HYPE, ZEC), 20 windows open within ±15 minutes of measurement, all
accepting orders with live books. A 15-minute variant exists too, answering the open
"5 or 15 minutes?" question: both. See §53–55; reproduce with
`scripts/verify_short_dated_crypto.py`.

The scan measured each window as `end_date - start_date`. These markets open ~24 hours
before the five minutes they settle on, so that returned 86,217s for a 300s contest —
**off by 287×**. The same arithmetic sat in `_apply_short_dated`, so every short-dated
crypto market on the venue was being classified as plain `CRYPTO` and would have been
priced with a long-horizon model. Fixed: `Market.contest_window_seconds()` measures
`end_date - event_start_time` and returns `None` rather than falling back, because the
fallback is the bug.

That is the **same error as §45's third cause, in a second domain** — measuring a
contest from when the market opened. Both were found by someone else pointing at a
market, not by a test.

**Tennis in-play state.** The socket supplies games in the current set but not sets
won by each player (`tennis.py` `BLOCKING`), and Gamma's event `score` for tennis was
not observed carrying it either. Still needs checking against a live tennis fixture —
none was in play during these runs.

**One decision is yours:** does "do not use Gamma" exclude Gamma-backed discovery?
The CLOB has no catalogue endpoint; `list_markets`, `list_events` and `get_sports` all
hit `gamma-api.polymarket.com`, and the join above makes that dependency deeper, not
shallower. Recorded as `allow_gamma_backed_discovery` — a flag that is *documentary
only, not enforced anywhere*. Needs deciding before live.

---

## 6. What is not proven

- **The hypertable conversion** — no Timescale locally. First real run is on CI.
- **Every authenticated path** — orders, balances, approvals, the relayer. Nothing
  needed credentials yet; first real need is Phase 5.
- **Market variety** — verification ran mostly against binary politics markets.
  Neg-risk groups and short-dated crypto will have their own surprises.
- **Anything about profitability** — no model produces a probability yet. The fee
  arithmetic already says margins are thin: at 0.85 the taker fee alone is 60 bps
  against a 50 bps minimum edge.

---

## 7. Recommended next step

**Phase 4 — EV and the safety gate.** It is the only remaining phase that is fully
verifiable today: EV is arithmetic over (probability, book, fees), and all three
inputs exist for the 161 `MONITORED` markets with a probability injected as a
fixture. It is also where the fee finding (§1) and the microstructure findings (§39)
start doing work.

Sports engines are no longer blocked (§5). The join is built and verified; what they
still need is a probability model, which is Phase 3 work resting on Phase 4's EV
arithmetic.

Full finding list: `docs/POLYMARKET-API-CONFORMANCE.md` (62 findings).
Venue surface map and the method for not misreading it:
`docs/POLYMARKET-SURFACE-AUDIT.md` — 9 hosts, 223 operations, plus
`make audit-surface`, which diffs raw venue JSON against what our code can see.
Build order and per-item notes: `docs/ROADMAP.md`.
