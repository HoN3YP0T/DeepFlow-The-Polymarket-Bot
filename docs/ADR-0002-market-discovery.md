# ADR-0002: Market discovery and the Gamma constraint

**Status:** Accepted, with a flagged constraint · **Date:** 2026-09-11

## Context

The brief states plainly: **"Do not use Gamma."** Data sources are scoped to
the SDK, CLOB/WebSocket, the Data API, and the Relayer.

Inspecting `polymarket-client` 0.10.0 surfaces a conflict:

1. **The CLOB service has no market-catalogue endpoint.** Its module
   (`polymarket/_internal/actions/clob.py`) contains only pricing operations —
   midpoint, price, book, spread, last trade price. Every one takes a token id
   you must already hold. It answers questions *about* markets; it cannot
   enumerate them.
2. **The SDK's discovery calls are Gamma-backed.** `list_markets`,
   `get_market`, `get_event`, `list_tags`, `get_sports` and `search` all
   dispatch through `_internal/actions/gamma.py`.
3. **The Data API is wallet-centric.** It answers questions about traders,
   positions and activity, not "what markets exist right now".

So market metadata — question text, outcome token ids, expiry, tags, and
critically the **resolution rules** the safety model depends on — is reachable
only through Gamma, whether called directly or through the SDK.

Complying with the letter of the constraint would mean no discovery at all, and
therefore no system.

## Decision

Discovery is isolated behind `MarketDiscoveryPort`, deliberately separate from
`MarketDataPort`, with a single implementation (`SdkMarketDiscovery`) that uses
the SDK's discovery calls.

The policy is recorded explicitly as
`PolymarketSettings.allow_gamma_backed_discovery`, defaulting to `False` — so
the constraint is a visible, configurable decision rather than an assumption
buried in an adapter.

Gamma is used for **nothing else**. Live pricing, order books, trades and
execution state come from CLOB and the WebSocket. Wallet intelligence comes
from the Data API. No probability, EV or sizing input reads Gamma-sourced data
beyond static market metadata.

## Consequences

**Good.** Discovery is one swappable class. If a CLOB-native or Data-API-native
catalogue endpoint ships, or a different source is mandated, one module changes
and nothing above it moves.

**Constraint stays visible.** The flag and this ADR mean the next person reads
the decision rather than rediscovering the conflict.

**Open question for the owner.** If "do not use Gamma" was meant to exclude
Gamma-backed discovery *entirely*, the alternatives are: maintain an external
market catalogue, or receive the tradeable market list from an outside source.
Both are substantially more work and neither removes the need for resolution
text. This needs a decision before live trading; it does not block the
skeleton.
