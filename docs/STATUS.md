# Status and handover

**As of:** 2026-09-12 · **Branch:** `claude/adoring-hypatia-pxi7up`
· **Phase 1 of 8 complete** · 251 tests green · 106 stubs remain

A readable version of this document is published at
<https://claude.ai/code/artifact/fd0408f3-96e1-417e-99e2-07bb1d3b0ce7>.

---

## 1. What the process does today

It runs. `make run` sweeps the market catalogue on a slow interval, folds the live
order-book stream for every tracked token, writes a snapshot row per priced book,
and reports its own feed health.

```
orchestrator.starting   mode=PAPER not_yet_wired=['signal loop', 'reconciliation', ...]
discovery.swept         markets=10 tokens=20
streams.connected       tokens=20
orchestrator.health     connected=True reconnects=0 dropped=0 snapshots_written=336
orchestrator.stopped    snapshots_written=500
```

**It cannot trade, structurally rather than by configuration.**
`Orchestrator.start` rejects `LIVE` before any socket opens, because reconciliation
and the safety gate do not exist. A process that can place orders but cannot
establish what it already owns is the one configuration this design refuses.

Run it anyway: the snapshot history a backtest replays can only be gathered in real
time. It is the only part of this build that cannot be caught up on later.

### Operating it

```bash
make up                  # postgres + redis
make migrate             # apply migrations
make run                 # discover, stream, persist
make check               # lint, typecheck, tests
make test-integration    # needs a database; see DEEPFLOW_TEST_DSN
```

Relevant settings: `DEEPFLOW_DISCOVERY_INTERVAL_SECONDS` (default 300),
`DEEPFLOW_MAX_TRACKED_MARKETS` (default 100), `DEEPFLOW_PERSIST_SNAPSHOTS`
(default true).

---

## 2. What Phase 1 delivered

| Component | Responsibility |
|---|---|
| `adapters/polymarket/sdk_client.py` | Client lifecycle. Refuses a signing client without both key and account wallet |
| `adapters/polymarket/mapping.py` | SDK payloads → domain models. The single anti-corruption layer |
| `adapters/polymarket/discovery.py` | Tradeable-market sweep, server-side liquidity floor, paging capped at the venue's real maximum |
| `adapters/polymarket/clob.py` | Books keyed by `asset_id`, midpoint, spread, trades, book-walk fill estimate |
| `adapters/polymarket/streams.py` | One socket fanned out into per-feed queues; reconnect with gap marking |
| `adapters/polymarket/book_state.py` | Incremental book folding, drift detection against the venue's reported touch |
| `adapters/polymarket/venue.py` | Exchange rules as code: fee formula, tick grid, GTD arithmetic, failure classification |
| `adapters/polymarket/sports_feed.py` | The sports feed's real payload, league list, per-sport status vocabularies |
| `pipeline/features.py` | `assess_quality` / `assess_snapshot` verdicts |
| `pipeline/orchestrator.py` | The Phase 1 task set, supervision, ordered shutdown |
| `adapters/persistence/` | Markets, snapshots, orders; migrations with conditional Timescale conversion |

### Verification scripts

All runnable without credentials. Public venue reads need no auth.

| Script | Proves |
|---|---|
| `scripts/verify_slice.py` | Discovery, mapping, batched books, complement invariant, live fee arithmetic |
| `scripts/verify_stream.py` | Folded stream state matches a fresh REST snapshot |
| `scripts/verify_phase1.py` | The whole chain including persistence and staleness reporting |

Last `verify_phase1.py` run: 6 markets discovered and persisted, 82 snapshots
streamed and folded, 164 rows written and read back, all FRESH, and the same book
correctly reported STALE once the clock advances past the budget.

---

## 3. Findings

**29 recorded** in `docs/POLYMARKET-API-CONFORMANCE.md`. Nineteen from reading the
published API docs against the scaffold (§1–19); ten from calling the API and
watching what arrived (§20–29), of which three were my own bugs (§27–29).

Every one of the ten was invisible in both the documentation and the SDK's type
signatures, and they share a shape: almost none crash. They produce plausible wrong
numbers, or silently stop the system doing something it should.

### The ones that would have cost money

| § | Finding | Why it mattered |
|---|---|---|
| 1 | Taker fees not modelled at all | At 0.85 the fee is 60 bps against a `min_net_ev` of 50 bps — inverts the sign on marginal trades |
| 20 | Book levels arrive worst-price-first on **both** sides | Pass-through gives a 99.8¢ spread, and the crossed-book validator *accepted* it |
| 22 | Batched books ignore request order, non-deterministically | Prices a 0.04 outcome off its complement's 0.96 book; every downstream gate agrees with the reflection of the truth |
| 4 | A matched trade is not a settled trade | Booking at match time manufactures a phantom position through the path documented as authoritative |
| 3 | Delayed-matching markets | Order accepted as `delayed` with no fills; every entry outlives a 10s timeout. Common on sports |
| 2 | 10s working order cannot be GTD | Venue minimum is ~2 minutes; the natural "clamp" fix leaves orders resting 12× too long |
| 8 | A private key alone cannot trade | pUSD not USDC, four approvals not two, wallet address not derivable, relayer key needed for approvals |
| 5 | Cricket and badminton have no data source | Both engines would abstain permanently — correct behaviour, indistinguishable from a bug |
| 27 | *Mine:* freshness was last-change, not feed liveness | Silently **blocks** trading, hardest on quiet markets — the 0.85–0.98 target band |
| 28 | *Mine:* time-series tables could not become hypertables | Would have failed on first conversion, in production, against populated tables |
| 29 | *Mine:* `OrderRepository.record` was unimplementable | `OrderRecord` carries no trade identity; `OrderRow` requires it `NOT NULL` |

### Guards that now exist

- `OrderBook` validates **sort order**, not just crossing, so wire order raises.
- `get_order_books` keys by `asset_id` and raises on a missing token rather than
  returning a short sequence. Its test fake deliberately returns a *different*
  order from the request, so a positional zip cannot pass.
- The **complement invariant** runs live: two best asks on a binary market must sum
  to ~1.00, with depth mirroring (`35x128` ↔ `128x35`) as a second check.
- `venue.py` pins the fee tables, tick grid and GTD arithmetic against published
  values, so a venue change surfaces as a named test failure.
- `gtd_expiration` raises below the venue minimum rather than clamping.
- `BookState.drifted()` compares our folded touch against the venue's reported one,
  so a dropped update is caught immediately rather than at the next REST poll.

---

## 4. Architecture decisions made this phase

- **ADR-0003 — venue rules are code, not configuration.** Exchange rules live in
  `adapters/polymarket/venue.py` as constants and pure functions with their source
  page cited, not in `config/thresholds.py` where everything is a tunable. The
  tie-breaker: *would Polymarket reject us for getting this wrong?* If yes, it is a
  rule. `venue.py` imports nothing, so any layer may import it.
- **Freshness is connection-wide feed liveness**, not per-book last change. See §27.
- **Composite primary keys on the time-series tables** from the first migration, so
  the Timescale conversion is possible at all.
- **Conditional hypertable migration** — creates the extension if installable,
  converts if present, logs and continues if not. One migration serves plain
  PostgreSQL for development and CI, and Timescale in production.
- **Integration tests run against real PostgreSQL** and skip cleanly without it. A
  UNIQUE constraint rejecting a duplicate is a property of the database; asserting
  it against a stub proves only that the stub agrees.

---

## 5. What is not proven

- **The hypertable conversion.** TimescaleDB is not installable in the build
  container. The composite keys follow Timescale's documented requirement and CI now
  runs the Timescale image, but the conversion runs for the first time on the next
  push.
- **Every authenticated path.** Nothing in Phase 1 needed credentials, so orders,
  balances and approvals are untested. First real need is Phase 5.
- **Market variety.** All verification ran against binary politics markets. Sports,
  negative-risk groups and short-dated crypto will have their own surprises.
- **Anything about profitability.** No model produces a probability yet, so there is
  no evidence of any kind that the strategy works.

---

## 6. Next: Phase 2

Three items. See `docs/ROADMAP.md` for the full ordering.

1. **`MarketClassifier`** — which engine owns a market. Needs *calibration at the
   low end*, not accuracy: `UNKNOWN` never auto-trades, so being unsure and saying
   so is the correct outcome.
2. **`ResolutionValidator`** — the highest-value safety component in the system. It
   enforces *never trade from the title alone*. The rules text is in
   `market.description`; the UMA fields (`question_id`, `resolved_by`,
   `uma_resolution_status`) are also available from discovery.
3. **`DiscoveryService`** — lifecycle transitions and rejection journalling.

**Recommended order: 1 and 3 first, then 2 with room to breathe.** Items 1 and 3 are
mechanical and give you markets moving through states with recorded reasons, which
makes 2 testable against real rejections rather than hypotheticals. The state
machine already forbids `CLASSIFIED → MONITORED`, so validation cannot be skipped
by accident while 2 is outstanding.

Expect `ResolutionValidator` to abstain (`UNPARSEABLE`) on many markets initially.
That is the safe direction, and it means Phase 2's rejection rate will look
alarmingly high while being correct.

Phase 2 will also need `SqlJournalRepository` implemented (currently stubbed) to
record rejections.

---

## 7. Decisions blocked on the owner

1. **Does "do not use Gamma" exclude Gamma-backed discovery?** The CLOB service has
   no market-catalogue endpoint and the SDK's discovery calls are Gamma-backed
   internally. Isolated behind one swappable adapter and recorded as
   `allow_gamma_backed_discovery`, but it needs deciding before live. See ADR-0002.
2. **Will you buy a sports data feed?** Without one, cricket and badminton cannot be
   written at all and football is a score-and-clock model. Blocks Phase 3 work
   regardless of code.
