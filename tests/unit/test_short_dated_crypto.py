"""Short-dated crypto markets, against payloads captured while they were open.

These exist because `docs/STATUS.md` recorded "no short-dated crypto markets" as a
blocker, and the venue lists one every five minutes for four assets. The scan that
produced that finding measured the window as ``end_date - start_date``, which on
these markets is ~24 hours because they open a day before the five minutes they
settle on.

The fixture is real ``list_events`` output, so the trap is in the data rather than
in an assertion: any test that invents ``start_date = end_date - 5 minutes`` passes
against a shape the venue never sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from polymarket.models.gamma.event import Event

from deepflow.adapters.polymarket import mapping
from deepflow.config.thresholds import Thresholds
from deepflow.core.enums import MarketCategory
from deepflow.pipeline.classifier import SHORT_DATED_CRYPTO_SECONDS, MarketClassifier

FIXTURE = Path(__file__).parent.parent / "fixtures" / "short_dated_crypto.json"


@pytest.fixture(scope="module")
def events() -> list[Event]:
    payloads: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    return [Event.model_validate(item) for item in payloads]


def _markets(events: list[Event]) -> list[Any]:
    return [
        mapping.with_event_context(mapping.to_market(sdk_market), event)
        for event in events
        for sdk_market in event.markets
    ]


def test_fixture_holds_real_short_window_markets(events: list[Event]) -> None:
    assert events, "fixture should hold captured up/down events"
    assert all("updown" in (event.slug or "") for event in events)


def test_the_contest_is_five_minutes_and_the_listing_is_a_day(events: list[Event]) -> None:
    """Both numbers, side by side, because the wrong one looks entirely reasonable."""
    for market in _markets(events):
        assert market.contest_window_seconds() == 300.0

        assert market.start_date is not None and market.end_date is not None
        listing = (market.end_date - market.start_date).total_seconds()
        assert listing > 80_000, "these markets open roughly a day before they settle"


def test_every_captured_market_classifies_as_short_dated(events: list[Event]) -> None:
    """The behaviour the old arithmetic denied for every market on the venue."""
    classifier = MarketClassifier(Thresholds())
    markets = _markets(events)
    assert markets

    verdicts = [classifier.classify(market) for market in markets]
    assert all(v.category is MarketCategory.BTC_5M for v in verdicts), [
        (m.slug, v.category) for m, v in zip(markets, verdicts, strict=True)
    ]


def test_window_sits_well_inside_the_short_dated_threshold(events: list[Event]) -> None:
    """300s against a 3600s threshold -- the old 86,200s missed it by 24x."""
    for market in _markets(events):
        window = market.contest_window_seconds()
        assert window is not None and window <= SHORT_DATED_CRYPTO_SECONDS


def test_more_than_bitcoin(events: list[Event]) -> None:
    """The category is named BTC_5M and the venue runs four assets on the cadence.

    Recorded rather than renamed: ``MarketCategory`` values reach the API schema, the
    database and the dashboard. What matters here is that ETH, XRP and SOL windows
    classify the same way instead of falling through to long-horizon ``CRYPTO``.
    """
    assets = {(event.slug or "").split("-", 1)[0] for event in events}
    assert len(assets) > 1, f"fixture should span assets, got {assets}"
