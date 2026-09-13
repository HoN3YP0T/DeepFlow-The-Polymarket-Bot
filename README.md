# DeepFlow

A modular, production-grade automated trading system for Polymarket.

**Status: skeleton.** The architecture, contracts, state machine and safety
scaffolding are in place. The engines are deliberately unimplemented — every
stub raises `NotImplementedError` rather than returning a plausible default, so
nothing can half-work silently. Live execution is disabled and cannot be
enabled by accident.

---

## The objective

> Maximize long-term risk-adjusted expectancy — **not** win rate.

These pull in opposite directions on this venue. A strategy buying 95%
favourites wins 19 times out of 20 and still loses money if it pays 2c of
spread for a 1c edge. The system is therefore built to refuse trades:
positive `net_ev` after fees, spread, slippage, fill probability and an
uncertainty buffer is a precondition, and a high market probability is only ever
a reason to *look*.

Nothing in this system describes a trade as guaranteed or sure-shot. A 97%
probability, taken thirty times, loses.

---

## Architecture

```
Polymarket SDK ─ CLOB/WebSocket ─ Data API ─ Relayer
                        │
                  Data Ingestion
                        │
                 Market Classifier
                        │
               Resolution Validation
                        │
                   Feature Engine
                        │
                 Probability Engine ──── Smart Money Engine
                        │
                   EV / Signal Engine
                        │
                  Risk + Safety Gate
                        │
                  Execution Engine
                        │
              Position / Exit Manager
                        │
                PostgreSQL / TimescaleDB
                        │
            FastAPI + React/Next.js Dashboard
```

The layout is hexagonal. `deepflow.ports` defines every external dependency as
a `Protocol`; `deepflow.adapters` implements them. **`deepflow/adapters/polymarket/`
is the only package permitted to import the `polymarket` SDK** — everything
above it speaks `deepflow.core.domain`. That boundary is what makes the venue
integration replaceable and the engines testable without a network.

| Layer | Package | Responsibility |
|---|---|---|
| Core | `core/` | Types, domain models, state machine, errors, clock, bus |
| Ports | `ports/` | Protocol interfaces — the seams |
| Adapters | `adapters/` | Polymarket SDK, Postgres, Redis |
| Pipeline | `pipeline/` | Discovery, classification, resolution, features |
| Engines | `engines/` | Per-sport, BTC, political, geopolitical, smart money, EV |
| Risk | `risk/` | Sizing, limits, exposure, safety gate, circuit breakers |
| Execution | `execution/` | Order lifecycle, idempotency, reconciliation |
| Positions | `positions/` | Position tracking, exit engine |
| API | `api/` | Dashboard backend |

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # defaults to PAPER mode

pytest                        # unit tests
ruff check src tests          # lint
mypy                          # type check

docker compose up -d db redis # Postgres + Redis
alembic upgrade head          # schema

deepflow                      # run (PAPER)
uvicorn deepflow.api.app:create_app --factory --reload   # dashboard API
```

---

## What runs today

Phase 1 is complete, so the process is runnable and collects data. It does not
trade, and cannot — the signal loop and execution adapter are not wired, and
`Orchestrator.start` refuses LIVE mode outright until reconciliation and the
safety gate exist.

```bash
make up                  # postgres + redis
make migrate             # apply migrations
make run                 # discover, stream, persist
```

Running it does three things: sweeps the market catalogue on a slow interval,
folds the live order-book stream for every tracked token, and writes a snapshot
row per priced book. A health line reports feed liveness, reconnects, dropped
events and rows written.

Worth starting early for one reason: the snapshot history a backtest replays can
only be gathered in real time. It is the single part of this build that cannot be
caught up on later.

Verification scripts, all runnable without credentials:

| Script | Proves |
|---|---|
| `scripts/verify_slice.py` | discovery, mapping, batched books, fee arithmetic |
| `scripts/verify_stream.py` | stream folding matches a fresh REST snapshot |
| `scripts/verify_game_join.py` | in-play fixtures resolve to their tradeable markets |
| `scripts/verify_short_dated_crypto.py` | 5-minute crypto windows exist and classify as `BTC_5M` |
| `scripts/verify_phase1.py` | the whole chain, including persistence and staleness |

`make verify` runs all five. `make capture-fixtures` refreshes the captured payload
corpus the sports tests read.

## Run modes

`BACKTEST → PAPER → SHADOW → LIVE`, and that order is the promotion path.

| Mode | Data | Orders |
|---|---|---|
| `BACKTEST` | Historical replay | Simulated against stored books |
| `PAPER` | Live | Simulated against the live book |
| `SHADOW` | Live | Built, signed, validated — **not sent** |
| `LIVE` | Live | Real |

Every mode runs the identical pipeline; they differ only at the final hop. If
`PAPER` took a different code path from `LIVE`, a clean paper run would prove
nothing. `SHADOW` goes one level deeper and exercises the real signing and
validation path, catching what paper trading structurally cannot: malformed
orders, tick-size violations, missing approvals.

### Arming live mode

Three independent interlocks, all required:

```bash
DEEPFLOW_MODE=LIVE
DEEPFLOW_LIVE_TRADING_CONFIRMED=true
DEEPFLOW_LIVE_TRADING_ACK="I ACCEPT REAL CAPITAL RISK"
```

plus a configured private key and funder address. A single stray environment
variable cannot arm real capital. The final guard sits in the execution adapter
itself (`_assert_live`), so no upstream bug can produce a live order from a
paper run.

---

## Safety model

Five independent layers. Each assumes the others may have failed.

1. **Resolution validation** — a market is tradeable only when its YES/NO
   conditions, deadline and source are parsed from the resolution text.
   Never inferred from the title.
2. **Safety gate** (`risk/safety_gate.py`) — 15 mandatory checks, no weighting.
   An unregistered check counts as a failure; a check that raises counts as a
   failure.
3. **Risk engine** — capped fractional Kelly with an uncertainty haircut and a
   hard cap. Full Kelly is never used: it is optimal only under a *correct*
   probability, and ours is an estimate.
4. **Circuit breakers** — halt new entries, never exits. A system that cannot
   reduce risk during a failure is more dangerous than one that keeps trading.
5. **Reconciliation** — venue state versus local state, on startup, reconnect
   and any uncertain execution. Failure halts new trading.

The rule that prevents the worst failure mode: **an order whose outcome is
unknown is never retried.** It becomes `EXECUTION_UNKNOWN` and goes to
reconciliation. Retrying an order that actually landed doubles the position, at
a worse price, in a market that has already moved.

---

## Design commitments

**No generic sports model.** A two-goal lead at minute 85 and a two-set lead in
tennis are both "ahead" and decay completely differently. One model per sport;
an unknown category is never traded.

**Engines abstain.** Missing game state returns `None`, not `0.5`. Abstaining
costs one skipped trade; a fabricated probability flows into sizing.

**Probability is independent of price.** An engine that reads the market price
and nudges it produces an edge that is an artefact of its own input.

**Size triggers detection; performance earns the score.** A wallet is not smart
because it is large. `SMART_MONEY_SCORE` comes from realized performance, ROI,
category specialization and timing — never from notional. Smart money is a
capped weighted feature, never a copy-trade.

**Noise is not a state change.** 94% → 92% is spread and a thin book; exiting
there pays the spread twice. A goal, a wicket, a break of serve, or BTC
crossing the strike is a different position entirely. The exit engine separates
price-derived evidence (must clear a volatility-scaled noise band) from
state-derived evidence (bypasses it).

**Rejections are recorded.** The trades taken are a biased sample of the
opportunities seen. Without the rejected set there is no way to distinguish a
correctly protective gate from one that never fires.

---

## Documentation

- [`docs/STATUS.md`](docs/STATUS.md) — **start here**: what runs today, findings, what's next
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module map, data flow, lifecycle
- [`docs/ADR-0001-sdk-choice.md`](docs/ADR-0001-sdk-choice.md) — why `polymarket-client`
- [`docs/ADR-0002-market-discovery.md`](docs/ADR-0002-market-discovery.md) — the Gamma constraint
- [`docs/ADR-0003-venue-rules-vs-thresholds.md`](docs/ADR-0003-venue-rules-vs-thresholds.md) — why exchange rules are code, not config
- [`docs/POLYMARKET-API-CONFORMANCE.md`](docs/POLYMARKET-API-CONFORMANCE.md) — review against the published API docs, and what it changed
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — build order

## Security

Credentials come from the environment or a secret manager, never from code.
`SecretStr` keeps them out of reprs and tracebacks, and a logging processor
scrubs them at any nesting depth. The dashboard is authenticated in every mode
that touches the venue, destructive controls require a typed confirmation, and
sensitive actions are audited with the actor.
