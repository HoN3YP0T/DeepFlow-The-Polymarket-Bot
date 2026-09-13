# The venue's full published surface, and how to stop misreading it

Written after three findings in a row were wrong in the same way. Each claimed the
venue lacked something it publishes:

| Wrong claim | Reality | What the mistake actually was |
| --- | --- | --- |
| No market ↔ live-game join (§45) | `list_events(game_ids=…)` | Looked on the market; the id is on the **event** |
| No short-dated crypto markets (§53) | One per asset every 5 min | Measured the window from `startDate` (listing) not `eventStartTime` (contest) |
| Cricket has no `game_id` (§56) | `eventMetadata.gameId = "1000169067LIVE2026"` | A **string** id, in a field typed `int \| None` |

All three were found by the owner pasting a URL. None was findable by reading prose,
and none was visible to a passing test suite. The common root is not ignorance of the
docs — it is **concluding about the venue from one typed field or one query shape.**

So this document is two things: the complete surface inventory, and a tool that makes
the blind spots mechanical instead of a matter of recall.

## Source 1: the documentation MCP server

`https://docs.polymarket.com/mcp` is a real MCP server (Mintlify-hosted, streamable
HTTP, read-only apart from feedback submission), wired into this project in
`.mcp.json` as `polymarket-docs`. It exposes:

| Tool | What it does |
| --- | --- |
| `search_polymarket_documentation` | Semantic search across the docs site |
| `query_docs_filesystem_polymarket_documentation` | Read-only shell-like queries over the docs as a virtual filesystem |
| `submit_feedback` | Report an incorrect or incomplete docs page |

Plus a resource, `mintlify://skills/polymarket`, carrying integration guidance.

**It is a real improvement and it is not sufficient.** Tested by asking it the three
questions this project already got wrong:

| Question | Would the MCP have prevented it? |
| --- | --- |
| How do I find a live game's markets? | **Yes.** There is a page called *List Events for a Game*. The join was documented the whole time; it simply was not read. |
| What measures a 5-minute market's window? | **No.** The *Market Timing* page lists `startDate`, `endDate`, `secondsDelay` and `gameStartTime` — `eventStartTime` is not documented anywhere. Only the raw payload has it. |
| Does cricket have a game id? | **No.** Nothing documents `eventMetadata.gameId`, its string type, or cricket's state vocabulary. |

One of three. So the rule is not "ask the MCP instead of probing" — the two wrong
findings it could not have caught were both about **fields the venue sends and the
docs never mention**, which is exactly what the audit below exists to surface. Use the
MCP for intent, semantics and changelog history; use the audit for what is actually on
the wire. Where they disagree, the wire wins: the docs say `/events/keyset` accepts
`limit` up to 500, and the server caps it at 100 (§65).

What the MCP did surface that no amount of payload probing would have: crypto up/down
markets resolve on a **Chainlink TWAP** with a 30-second lookback at 5 minutes and 60
at 15 minutes and 4 hours (§63), and sports limit orders are **auto-cancelled at game
start** with no guarantee when a game starts early (§64). Both are *semantics*, not
fields — and both change how a model has to be built.

## Source 2: the tool

```bash
make audit-surface          # or: .venv/bin/python scripts/audit_venue_surface.py --verbose
```

`scripts/audit_venue_surface.py` fetches **raw JSON** from public endpoints across 17
payload shapes and does a three-way diff:

```
raw JSON keys  →  SDK model fields  →  our domain model fields
```

Anything the venue sends that neither layer names is printed **with a sample value**,
because the value is what carries the lesson: `"1000169067LIVE2026"` says "string id"
at a glance in a way `gameId: present` never would. Current count: **92 distinct
unmodelled field names**. Most are presentation or features out of scope; the ones
that matter are triaged below.

The rule this replaces "I checked the docs" with: *before concluding the venue lacks
a field, run the audit and read what it actually sends.*

## Inventory: 9 hosts, 223 operations

| Host | Spec | Ops | What it is |
| --- | --- | --- | --- |
| `gamma-api.polymarket.com` | `gamma-openapi.yaml` | 42 | Catalogue: events, markets, tags, teams, sports, series, search |
| `clob.polymarket.com` | `clob-openapi.yaml` | 66 | Books, prices, orders, auth, rewards, rebates, heartbeats |
| `data-api.polymarket.com` | `data-openapi.yaml` + `/v2/openapi.json` | 40 | Trades, positions, holders, activity, leaderboards, PnL, status |
| `api.perpetuals.polymarket.com` | `perps-openapi.json` | 59 | **Perpetual futures** — a separate product, entirely out of scope |
| `relayer-v2.polymarket.com` | `relayer-openapi.yaml` | 7 | Gasless transaction submission |
| `bridge.polymarket.com` | `bridge-openapi.yaml` | 5 | Deposits, withdrawals, supported assets |
| `combos-rfq-api.polymarket.com` | `combos-rfq-openapi.yaml` | 4 | Maker RFQ quoting for combo markets |
| `clob-staging.polymarket.com` | declared in `clob-openapi.yaml` | — | **Declared but does not answer** (see §58) |
| `data-api-rs.stage…polymarket.sh` | declared in `data-openapi.yaml` | — | Internal staging host, not reachable |

Five WebSocket channels, from the AsyncAPI specs: market (`asyncapi.json`), user
(`asyncapi-user.json`), sports (`asyncapi-sports.json`), RFQ (`asyncapi-rfq.json`),
perps (`asyncapi-perps.json`).

Fetch every spec with:

```bash
curl -sS https://docs.polymarket.com/llms.txt          # index, lists all of them
curl -sS https://docs.polymarket.com/api-spec/gamma-openapi.yaml
curl -sS https://data-api.polymarket.com/v2/openapi.json
```

The Gamma spec was in that index the whole time the join was being called impossible.

## What the audit surfaced (triaged)

### Worth acting on

- **`/clob-markets/{condition_id}` is the condition-id lookup** I had recorded as
  not existing (§59), and it returns everything the order path needs in one call —
  tick size, min order size, seconds delay, fee schedule, accepting-orders. Its keys
  are abbreviated; the decode table is in §59.
- **`data-api /v2/status` is the venue's own freshness oracle** (§61): `serving.lag_seconds`,
  per-mechanism `age_seconds` and `blocks_behind`, ingestion cursor counts. Our
  staleness logic measures our own clock only, so a Data API running 4s behind is
  invisible to it.
- **`clearBookOnStart`** — the book is cleared when the contest starts. On a
  5-minute crypto market that is the whole pre-window book disappearing at `t=0`, and
  on the cricket toss market it is `true`. Anything holding folded state across that
  boundary is holding a stale book.
- **`eventMetadata.opticOddsGameId` / `pandascoreMatchId`** — the venue names its odds
  provider per fixture, and `opticOddsSelection` / `opticOddsSelectionLine` expose the
  bookmaker's own selection mapping.

### Worth knowing, deliberately not used

- **`makerBaseFee` / `takerBaseFee` are a constant `1000`** on every market sampled,
  across fee types, and do not reconcile with `feeSchedule.rate` (§62). Fee arithmetic
  must keep using `feeSchedule`; treating these as bps would be catastrophic and
  treating them as anything is unjustified.
- **`funded` and `ready` are not tradeability gates** (§60): both `false` on 100 of
  100 markets that were actively accepting orders. Gating on them would refuse
  everything.
- **`is_50_50_outcome`** exists and was `false` on 1000 sampled markets, including
  where a true coin flip (the cricket toss) would justify it. Not a usable
  coin-flip detector.
- `approved`, `deploying`, `pendingDeployment`, `manualActivation`, `forceShow`,
  `forceHide`, `layout`, `isCarousel`, `recurrence`, `seriesType`, `primaryTagId`,
  `customLiveness`, `umaBond`, `umaReward`, `rfqEnabled`, `negRiskOther`,
  `marketMakerAddress`, `fpmm` — recorded as seen and unused, so "we looked and chose
  not to" is written down rather than assumed.

## The method, as a checklist

1. **Ask the docs MCP for intent and semantics**, and read the spec for shape.
   `llms.txt` lists every OpenAPI and AsyncAPI file. Neither is the last word: the
   documented pagination maximum is five times the real one (§65).
2. **Fetch raw JSON before believing a typed model.** The SDK is `extra="ignore"`; it
   silently drops what it does not model, including the sports socket's whole
   `eventState` block (§50) and cricket's string id (§56).
3. **Check both the event and the market.** Contest identity and timing live on the
   event: `game_id`, `startTime`, tags, live score. A market-only discovery path
   cannot see any of them.
4. **Never measure a contest from `startDate`.** That is when the market listed. Use
   `eventStartTime`; `Market.contest_window_seconds()` is the only place this is
   computed, and it returns `None` rather than falling back.
5. **A field absent from a typed model is not a field absent from the venue.**
6. **Before recording "the venue does not have X", run the audit** and paste what it
   returned into the finding. Every one of the three wrong claims would have failed
   that step.
7. **Test against captured payloads, never invented ones.** The short-dated crypto
   test passed against `start_date = end_date - 5min`, a shape the venue never sends.
8. **When docs and wire disagree, the wire wins** — and record the disagreement rather
   than quietly following one of them.
