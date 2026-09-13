# DeepFlow — working notes for Claude

Automated Polymarket trading bot. Python 3.11, async, hexagonal. **Not yet able to
trade and must not be made able to** — see *Hard rules*.

This file is a map and a list of traps, not a status report. It exists so a session
does not re-derive what previous sessions already established, and so the same
mistakes are not made a fourth time.

---

## Read these instead of re-exploring

| Question | File |
| --- | --- |
| What is done, what is left, what broke and was fixed | `docs/STATUS.md` — **start here** |
| What the venue actually does (70 findings, 4 retractions) | `docs/POLYMARKET-API-CONFORMANCE.md` |
| The venue's full surface + the method for not misreading it | `docs/POLYMARKET-SURFACE-AUDIT.md` |
| Build order, per-phase state | `docs/ROADMAP.md` |
| Module map, dependency rule, data flow | `docs/ARCHITECTURE.md` |
| Why the SDK / Gamma / venue-rules-as-code | `docs/ADR-000{1,2,3}-*.md` |

Findings are numbered and cross-referenced as `§N`. **Cite them rather than
restating them**, and when a finding turns out wrong, mark it RETRACTED in place with
the reason — §5, §34, §45 and §53 are all corrections and the trail matters.

## Commands

```bash
make check              # lint + typecheck + tests. Run before every commit.
make db-local           # local postgres (no docker). Without it 30 integration tests SKIP.
make redis-local        # local redis (no docker), persistence off.
make verify             # 5 scripts against the live venue. No credentials needed.
make verify-account     # read-only credentialed checks (needs .env). Places no orders.
make audit-surface      # raw venue JSON vs what our code can see. See Traps.
make capture-fixtures   # refresh the payload corpus; exits non-zero if it would test less
make run                # discover, stream, persist (PAPER)
```

`make check` does **not** run `verify`, `verify-account` or `audit-surface` — they need the network and
their result depends on what is trading right now.

---

## Hard rules

1. **Never make LIVE mode reachable.** `Orchestrator.start` raises on
   `RunMode.LIVE` before constructing anything. There is no execution adapter and no
   reconciler; a process that can trade but cannot establish what it already owns is
   the one configuration this design refuses.
2. **Never weaken a fail-closed default.** An unregistered safety check fails, a
   check that raises fails, and a check whose input is `None` fails. A gate that
   passes for want of an input is worse than no gate, because the journal then
   records that the checklist ran *and approved*.
3. **Fee arithmetic uses `feeSchedule`.** Never `makerBaseFee`/`takerBaseFee` — a
   constant `1000` that reconciles in no unit (§62).
4. **Push only to the designated branch.** Currently
   `claude/adoring-hypatia-pxi7up`. No PRs unless asked.
5. **Never disable TLS verification or unset `HTTPS_PROXY`.**

---

## Traps — every one of these has already cost a wrong conclusion

**The venue lesson, in one line: a field missing from a typed model is not a field
missing from the venue.** Four findings were wrong this way. Before recording that
the venue lacks anything, run `make audit-surface` and paste what it returned.

- **Contest identity and timing live on the *event*, not the market.** `game_id`,
  `startTime`, tags, live score. A discovery path built on `list_markets` cannot see
  any of them. This is why `games.py` exists and why `mapping.with_event_context`
  attaches event id, `event_start_time` and tags to a market reached through an event.
- **`startDate` is when the market *listed*; `eventStartTime` is when the contest
  *begins*.** A 5-minute crypto market opens ~24 h early, so `end_date - start_date`
  is 86,200 s for a 300 s contest — off by 287×. Use
  `Market.contest_window_seconds()`, which returns `None` rather than falling back
  (§54). The same confusion produced the "0 of 600 sports markets" error (§45).
- **Cricket's fixture id is a *string* in `eventMetadata.gameId`** while the typed
  `sports.game_id` is `int | None` and empty. `games.provider_game_id()` reads both.
- **The sports wire is camelCase** (`gameId`, `leagueAbbreviation`) and the SDK
  renames it. A raw reader keying on this repo's snake_case names sees **zero games**.
  The SDK is `extra="ignore"` and silently drops the wire's whole `eventState` block
  (§50).
- **Book levels arrive worst-price-first on both sides.** `mapping.to_order_book`
  reverses both. Passing the wire order through yields a 99.8c spread with every
  field name still looking correct.
- **`get_order_books` returns results in arbitrary order.** Key by `asset_id`, never
  zip positionally — the complement of a binary market is a plausible-looking price.
- **Freshness is *feed liveness*, not last change.** An order book that has not moved
  for 30 s on a live feed is correct. Timestamping by last change marks quiet markets
  STALE, and quiet markets in 0.85–0.98 are the ones this system exists to trade.
- **`live=true` does not mean in play.** Observed: a suspended fixture, `live=true`,
  `period="SUS"`, kickoff three days out. Guard with `GameLink.is_in_play`.
- **`funded` and `ready` are not tradeability gates** — both `false` on 100 of 100
  markets accepting orders (§60).
- **Documented pagination maxima are wrong.** `/events/keyset` says `limit` up to
  500; the server caps at 100 (§65). A sweep sized at 500 silently receives a fifth.
- **The book is cleared when a contest starts**, best-effort — an early start can
  leave a resting order live into play (§64). Any folded state held across that
  boundary is stale.
- **Crypto up/down settles on a Chainlink TWAP** (30 s lookback at 5 min, 60 s at
  15 min and 4 h), not spot (§63). Modelling spot prices a different instrument.

- **A wrong import path poisons everything downstream of it.** mypy *does* check the
  SDK (it ships `py.typed`) and would have caught `AssetType.COLLATERAL` — but the
  import named `polymarket.models.clob.enums`, which does not exist, and
  `ignore_missing_imports` made that module and every symbol from it `Any`. One
  silenced import erased the type information that would have caught the real bug;
  604 tests, ruff and mypy all passed while the call was dead (§67). Import from the
  module that actually exists and the type checker works. Verify a path by importing
  it, not by reading it.
- **Position size is `current_size`**, not `size`. Reading the wrong name yields zero
  shares, and the zero-filter then drops the position, so a funded account reconciles
  as **flat** (§68). Both sites now share `mapping.position_shares`.

### Testing traps

- **Never build a payload the venue does not send.** The short-dated crypto test
  passed indefinitely against `start_date = end_date - 5min`, a shape that does not
  exist, and that is why the bug survived. Fixtures in `tests/fixtures/` are captured
  live; regenerate with `make capture-fixtures`.
- **A skipped test reads as green.** 30 integration tests skip without a database.
  Run `make db-local` before quoting a test count.
- **Per-module tests passing proves nothing about wiring.** `games.py` and
  `engines/sports/rules/` were each fully tested and reachable from nothing. Wiring
  them immediately surfaced a gap no unit test could: REST reports soccer full time
  as `VFT` where the socket sends `FT`.

---

## Layout and conventions

```
core/         domain models (frozen pydantic), enums, errors, types, clock  — imports nothing
ports/        Protocols for every external dependency
adapters/     polymarket/ (SDK, venue rules, streams, games, mapping), persistence/, cache/
pipeline/     discovery, classifier, resolution, features, orchestrator
engines/      ev, microstructure, sports/{rules,models}, crypto, politics, geopolitics
risk/         safety_gate (17 checks), limits (RiskEngine), exposure, sizing
execution/    order manager, reconciliation           ← Phase 5, stubs
positions/    manager, exit engine                    ← Phase 6, stubs
journal/      recorder
api/          FastAPI routers                         ← Phase 7, stubs
```

Nothing in `core/` or `ports/` imports an adapter. Only `adapters/polymarket/` imports
the `polymarket` SDK. One exception: `adapters/polymarket/venue.py` holds published
exchange rules, imports nothing, and any layer may import it (ADR-0003).

**Conventions that are load-bearing, not style:**

- **`None` means "not measured", never zero.** A missing score is not 0-0; a missing
  fee is not free; an unmeasurable imbalance is not neutral. Returning a plausible
  default where a value is unknown is the failure mode most of this codebase's
  comments are about.
- **`Decimal` everywhere for money and probability.** Never float.
- **Docstrings say *why*, and cite evidence.** Most non-obvious lines here exist
  because something was measured live and surprised us; the measurement belongs next
  to the code. Do not write a docstring describing behaviour you have not
  implemented — that has happened twice (`is_modellable`, `sports_feed`).
- **Dead code gets deleted, not justified.** Five constants were once kept alive by a
  circular argument.
- **Stubs raise `NotImplementedError`**, never return a plausible default. 72 remain
  and the count is a tracked figure in `docs/STATUS.md`.

## Current shape of the work

Phases 1, 2 and 4 complete; Phase 3 is 3 of 6; Phases 5–8 not started.

**The pipeline is finished at both ends and hollow in the middle.** Discovery,
classification, streaming, the live-game join, EV, the 17-check gate, risk, exposure
and the journal all work. **No probability model is written**, so the decision layer
runs on injected estimates: the system can explain in full why it would not trade and
cannot yet explain why it would.

Next most useful piece of work is a model — `Btc5mEngine` is the more tractable
(fully specified settlement, a fresh market every 5 minutes across 8 assets, so it
can be verified continuously); `FootballEngine` is more valuable and slower to check.

## Working style the owner has asked for

- **Ask and recommend before each unit of work**, then summarise what was done.
- **Commit after each completed item**, with the reasoning in the message.
- Be direct about being wrong. Four findings in this repo are retractions, and the
  owner found three of them by pasting a URL — so when a conclusion is "the venue
  cannot do X", check harder before writing it down.
