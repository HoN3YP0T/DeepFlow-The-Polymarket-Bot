# Architecture

## Principles

**Hexagonal.** `ports/` declares every external dependency as a `Protocol`;
`adapters/` implements them. The pipeline and engines depend only on ports and
`core.domain`. This is what lets the whole system be tested without a network,
and what confines an SDK upgrade to one package.

**Fail closed.** Every default is the safe one. An unregistered safety check
fails. A safety check that raises fails. An engine without state abstains. An
unclassifiable market is not traded. An uncertain order is never retried.

**Explainable.** Every decision — taken or rejected — is journalled with the
evidence that produced it.

## Dependency direction

```
        api/        pipeline/      engines/      risk/    execution/
          │             │             │            │          │
          └─────────────┴──────┬──────┴────────────┴──────────┘
                               ▼
                         ports/  +  core/
                               ▲
          ┌────────────────────┴────────────────────┐
     adapters/polymarket   adapters/persistence   adapters/cache
```

Arrows point at dependencies. Nothing in `core/` or `ports/` imports an adapter.
Only `adapters/polymarket/` imports the `polymarket` SDK.

One deliberate exception: `adapters/polymarket/venue.py` holds published
exchange rules — the fee formula, the tick-size precision grid, contract
addresses, order-lifetime minimums — and imports nothing at all. Any layer may
import it. The fee formula is an input to expected value, and hiding it behind a
port would have implied it is a substitutable modelling choice rather than
arithmetic the venue fixes. See ADR-0003.

## Market lifecycle

```
DISCOVERED → CLASSIFIED → VALIDATED → MONITORED → CANDIDATE → SIGNAL
   → RISK_APPROVED → ORDER_PENDING → [PARTIALLY_FILLED] → FILLED
   → MANAGED → EXIT_PENDING → CLOSED
```

Failure states: `DATA_STALE`, `MARKET_INVALID`, `EXECUTION_UNKNOWN`,
`RECONCILIATION_REQUIRED`, `HALTED`.

Two venue realities the lifecycle has to absorb (see
`docs/POLYMARKET-API-CONFORMANCE.md` §3 and §4):

- **`ORDER_PENDING` covers a venue `delayed` status.** A market with
  `seconds_delay` accepts an order without matching it — no fills, no trade ids.
  That is pending, not partial and not rejected. Such markets are excluded by
  default (`max_seconds_delay = 0`).
- **`FILLED` requires settlement, not just a match.** A matched trade progresses
  `MATCHED → MINED → CONFIRMED` and can instead go `RETRYING` or `FAILED`, so
  `MATCHED_UNSETTLED` sits between them. Booking a position at match time
  produces a holding the venue does not believe in.

Enforced by `core/state_machine.py`. The transition table is the single source
of truth and the guard lives in one place, so no call site can bypass it.

Edges that carry real safety weight:

- `CLASSIFIED → MONITORED` **does not exist.** Resolution validation cannot be
  skipped. This is the edge that enforces *never trade from the title alone*.
- `EXECUTION_UNKNOWN → CLOSED` **does not exist.** An indeterminate order must
  pass through `RECONCILIATION_REQUIRED`.
- States holding exposure cannot transition to `MARKET_INVALID` — that would
  abandon a position that still exists on the venue.
- `DATA_STALE` returns to `MONITORED` or `MANAGED`. Staleness suspends; it does
  not destroy.

## Data flow

```
Discovery (slow poll)                WebSocket (push)
  SDK discovery ──► classify           CLOB market stream ──┐
       └──► validate resolution        Sports stream ───────┤
              └──► MONITORED           Crypto price stream ─┤
                       │               User stream ─────────┘
                       ▼                        │
                 monitored set ◄────────────────┘
                       │
                       ▼
              FeatureEngine ──► microstructure + data quality
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
  probability     smart money      cross-market
   engine          engine            engine
       └───────────────┼────────────────┘
                       ▼
                  EvEngine ──► net EV after all costs
                       │
                  SafetyGate ──► 15 mandatory checks
                       │
                  RiskEngine ──► capped fractional Kelly
                       │
              ExecutionEngine ──► mode-routed submission
                       │
              PositionManager ──► ExitEngine (continuous)
                       │
                   Journal + PostgreSQL ──► Dashboard
```

## Concurrency

One asyncio event loop. Long-lived tasks owned by `Orchestrator`: discovery
sweep, four stream consumers, smart-money poll, signal loop, position manager,
health monitor.

Streams publish to `core.bus.EventBus`, which fans out over bounded queues and
**drops for a slow subscriber** rather than applying backpressure. A dashboard
tab on a sleeping laptop must not be able to stall market-data ingest.

Shutdown order: stop new entries → drain in-flight orders → cancel tasks →
disconnect. Tearing down the stream while an order is in flight manufactures
the uncertain-execution state everything else works to avoid.

## Persistence

PostgreSQL, with TimescaleDB hypertables for `market_snapshots` and
`smart_money_events`.

`orders.client_key` is **UNIQUE**. Idempotency is a database constraint, not an
application-level check — the check-then-act version has a race that produces
exactly the duplicate order it was meant to prevent.

All money and probability columns are `Numeric`. Float rounding on a 0.97
market is the difference between positive and negative EV.

## Testing strategy

| Layer | Approach |
|---|---|
| Core (state machine, sizing, gate, idempotency) | Pure unit tests, no I/O |
| Engines | Fixture game states → expected probability bands |
| Adapters | `respx`-mocked HTTP; recorded WebSocket frames |
| Venue rules | Fee tables, tick grid and GTD arithmetic pinned against the published values (`test_venue.py`) |
| Execution | Simulated venue: timeouts, partials, duplicates, rejections |
| Recovery | Kill/restart mid-order, reconciliation divergence |
| E2E | Full pipeline against a replayed session |

Failure cases that must have tests before live: duplicate orders, partial
fills, API timeout, WebSocket disconnect with a gap, stale data, database
outage, process restart mid-order, reconciliation failure, extreme volatility,
HTTP 425 during a matching-engine restart, the post-only window that follows it,
a `delayed` order acceptance, and a matched trade that settles `FAILED`.
