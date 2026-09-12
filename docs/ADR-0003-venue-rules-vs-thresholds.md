# ADR-0003: Venue rules are code, not configuration

**Status:** Accepted · **Date:** 2026-09-12

## Context

Reviewing the scaffold against https://docs.polymarket.com surfaced nineteen
discrepancies (`docs/POLYMARKET-API-CONFORMANCE.md`). They fell into two clearly
different kinds, which the codebase had no way to distinguish:

1. **Rules of the exchange.** The fee formula, the tick-size precision grid, the
   minimum GTD lifetime, the four required approvals, which contract to sign
   against. Getting these wrong produces a rejection, or worse a silently
   mispriced trade. They are not ours to choose.
2. **Our opinions.** Candidate probability bands, Kelly fraction, exposure caps.
   These are meant to be swept in a backtest and overridden from the dashboard.

Both would naturally have landed in `config/thresholds.py`, where everything is
a tunable `Field(default=...)`. That is actively harmful for the first kind: it
invites someone to "tune" the GTD minimum, or to sweep a fee rate the venue
fixes. Three of the findings were ones where the scaffold *had* configured a
venue rule as a preference — `order_timeout_seconds = 10` as though GTD could
express it, `ws_ping_interval_seconds = 20` as one value across three sockets
with different requirements, and an implied zero fee.

## Decision

Venue rules live in `src/deepflow/adapters/polymarket/venue.py` as module-level
constants and pure functions, each with its source page cited. They are not
configurable, and `tests/unit/test_venue.py` pins them against the values
Polymarket publishes.

`venue.py` imports no SDK and no other DeepFlow module, so — uniquely within
`adapters/polymarket/` — anything may import it, including engines. The
alternative was routing the fee formula through a port, which would have
implied it is a substitutable modelling choice. It is arithmetic.

Where a venue rule needs a *policy* response, the policy is a threshold and the
rule is not:

| Venue rule (`venue.py`) | Our policy (`thresholds.py`) |
|---|---|
| GTD minimum is ~2 min effective | `order_timeout_seconds`, enforced client-side |
| markets may set `seconds_delay` | `max_seconds_delay = 0` — refuse them |
| venue offers an order heartbeat | `require_order_heartbeat = True` |
| 425 means restarting | `engine_restart_backoff_base_seconds` |
| taker fee = `C·r·p(1−p)` | `fail_closed_on_unknown_fee_schedule` |
| feed covers 9 leagues, no cricket | cricket/badminton engines stay disabled |

## Consequences

**Good.** A venue change surfaces as a failing test naming the rule, not as a
slow bleed in production. The distinction is visible in the import: an engine
reaching for `venue` is consuming a fact; an engine reaching for `thresholds` is
consuming a decision.

**Cost.** Two places to look for a number, and the boundary needs judgement at
the margin. The tie-breaker is the question "would Polymarket reject us for
getting this wrong?" — if yes, it is a rule.

**Duplication, accepted.** `TAKER_FEE_RATE_BY_CATEGORY` restates the published
table while `Market.fee_schedule` carries the authoritative per-market values.
The table is for planning and backtests where no live market object exists; the
market always wins at decision time, and the docstring says so.

**Open item for the owner.** Two findings are procurement decisions, not code:
cricket and badminton have no venue-native state feed, and even the covered
sports supply only score and clock. Enabling those engines requires buying a
data feed. Until then they abstain — correctly, but permanently.
