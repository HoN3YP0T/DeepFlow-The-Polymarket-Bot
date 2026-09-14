"""Reading settled outcomes, and the alignment that makes them meaningful.

The payout pair is positional and ``Market.outcomes`` is built from named accessors
on a different service's model. §22 is in this repo because a plausible positional
zip returns a binary market's complement -- and here that would invert the outcome of
every calibration sample, producing a curve that confidently teaches each engine to
be wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from deepflow.adapters.polymarket.resolutions import MAX_CONDITION_IDS, PolymarketResolutions
from deepflow.core.domain import Market, Outcome
from deepflow.core.enums import OutcomeSide

RESOLVED_AT = datetime(2026, 9, 14, 5, 1, 27, tzinfo=UTC)


class _Record:
    """Shaped like the SDK's ``Resolution``, with only what the adapter reads."""

    def __init__(
        self,
        condition_id: str,
        payouts: tuple[Decimal, ...] | None,
        *,
        status: str = "resolved",
        resolved_at: datetime | None = RESOLVED_AT,
        was_disputed: bool = False,
        source: str | None = "reported",
    ) -> None:
        self.condition_id = condition_id
        self.payouts = payouts
        self.status = status
        self.resolved_at = resolved_at
        self.last_updated_at = RESOLVED_AT
        self.was_disputed = was_disputed
        self.resolution_source = source


class _Public:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records
        self.calls: list[list[str]] = []

    async def get_resolutions(self, *, condition_ids: list[str]) -> tuple[_Record, ...]:
        if not condition_ids:
            # What the venue really does -- the SDK raises UserInputError. Mirrored so
            # a regression that passes an empty list fails here rather than live.
            raise AssertionError("condition_id must be non-empty")
        self.calls.append(list(condition_ids))
        return tuple(r for r in self._records if r.condition_id in condition_ids)


class _Session:
    def __init__(self, records: list[_Record]) -> None:
        self.public = _Public(records)


def _market(condition_id: str, labels: tuple[str, str] = ("Up", "Down")) -> Market:
    return Market(
        condition_id=condition_id,  # type: ignore[arg-type]
        question="Will it go up?",
        outcomes=(
            Outcome(token_id=f"{condition_id}-0", label=labels[0], side=OutcomeSide.YES),  # type: ignore[arg-type]
            Outcome(token_id=f"{condition_id}-1", label=labels[1], side=OutcomeSide.NO),  # type: ignore[arg-type]
        ),
        active=False,
        closed=True,
        accepting_orders=False,
    )


def _adapter(records: list[_Record]) -> PolymarketResolutions:
    return PolymarketResolutions(_Session(records))  # type: ignore[arg-type]


async def test_payouts_land_on_the_outcome_tokens_in_order() -> None:
    """Measured live: across 17 settled markets with trades, the token each payout
    index belongs to matched what that token last traded at -- 17 agreed, 0
    disagreed. This pins the alignment that measurement established."""
    market = _market("0xaa")
    resolved = await _adapter([_Record("0xaa", (Decimal(0), Decimal(1)))]).resolve([market])

    assert len(resolved) == 1
    assert resolved[0].payout_for(market.outcomes[0].token_id) == Decimal(0)  # Up lost
    assert resolved[0].payout_for(market.outcomes[1].token_id) == Decimal(1)  # Down won


async def test_a_payout_count_that_does_not_match_the_outcomes_is_skipped() -> None:
    """The one shape in which the positional assumption could be wrong.

    Zipping as far as it goes would produce a sample that is indistinguishable from
    evidence the model is inverted, which is worse than having no sample at all.
    """
    resolved = await _adapter([_Record("0xaa", (Decimal(1),))]).resolve([_market("0xaa")])
    assert resolved == ()


async def test_a_market_still_awaiting_payouts_is_not_a_failure() -> None:
    """Proposed, challenged and under review are lifecycle stages. The market will
    have payouts on a later sweep; inventing one now would be a fabricated outcome."""
    resolved = await _adapter(
        [_Record("0xaa", None, status="proposed")],
    ).resolve([_market("0xaa")])
    assert resolved == ()


async def test_a_resolution_with_no_timestamp_is_skipped() -> None:
    """Without a settlement time a sample cannot be placed against the prediction
    that preceded it, which is the only reason to store it."""
    record = _Record("0xaa", (Decimal(1), Decimal(0)), resolved_at=None)
    record.last_updated_at = None  # type: ignore[assignment]
    assert await _adapter([record]).resolve([_market("0xaa")]) == ()


async def test_an_empty_request_never_reaches_the_venue() -> None:
    """The venue rejects an empty id list outright -- the same shape as §79's
    ``symbols=[]``, which retried every two seconds and latched a breaker."""
    session = _Session([])
    assert await PolymarketResolutions(session).resolve([]) == ()  # type: ignore[arg-type]
    assert session.public.calls == []


async def test_requests_are_chunked_to_the_venue_limit() -> None:
    """20 condition ids per request, enforced by the SDK, which raises rather than
    truncating. A sweep sized larger would fail the whole pass."""
    markets = [_market(f"0x{i:02x}") for i in range(MAX_CONDITION_IDS * 2 + 3)]
    session = _Session([])
    await PolymarketResolutions(session).resolve(markets)  # type: ignore[arg-type]

    assert len(session.public.calls) == 3
    assert all(len(call) <= MAX_CONDITION_IDS for call in session.public.calls)
    # Every market asked about exactly once: a chunking bug that dropped the tail
    # would look like markets that never settle.
    asked = [cid for call in session.public.calls for cid in call]
    assert sorted(asked) == sorted(str(m.condition_id) for m in markets)


async def test_a_disputed_resolution_is_recorded_as_such() -> None:
    """Still a resolution, and also a reason to look twice at any sample from it."""
    resolved = await _adapter(
        [_Record("0xaa", (Decimal(1), Decimal(0)), was_disputed=True)],
    ).resolve([_market("0xaa")])
    assert resolved[0].was_disputed


async def test_an_unknown_token_scores_nothing_rather_than_zero() -> None:
    """A token absent from the payout set is one we cannot score. Reading it as zero
    would teach the curve that every unmatched prediction was wrong."""
    adapter = _adapter([_Record("0xaa", (Decimal(1), Decimal(0)))])
    resolved = await adapter.resolve([_market("0xaa")])
    assert resolved[0].payout_for("nonexistent") is None  # type: ignore[arg-type]


async def test_a_market_with_no_outcomes_is_never_asked_about() -> None:
    """Its payouts could not be aligned to anything, so the request would be wasted
    against a 20-id budget."""
    bare = Market(
        condition_id="0xbb",  # type: ignore[arg-type]
        question="?",
        outcomes=(),
        active=False,
        closed=True,
        accepting_orders=False,
    )
    session = _Session([])
    assert await PolymarketResolutions(session).resolve([bare]) == ()  # type: ignore[arg-type]
    assert session.public.calls == []


async def test_a_resolution_for_a_market_we_did_not_ask_about_is_ignored() -> None:
    """Defensive: without its market there is no outcome order to align against."""
    records: list[Any] = [_Record("0xzz", (Decimal(1), Decimal(0)))]
    assert await _adapter(records).resolve([_market("0xaa")]) == ()
