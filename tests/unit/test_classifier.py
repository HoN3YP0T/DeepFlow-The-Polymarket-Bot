"""Market classification.

The property under test is calibration at the low end, not accuracy. An honest
abstention costs one skipped market; a confident error routes a market to a model
whose state variables are meaningless for it, and every gate downstream then agrees
with the wrong number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import Market, Outcome
from deepflow.core.enums import MarketCategory, OutcomeSide
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.pipeline.classifier import MarketClassifier

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def classifier() -> MarketClassifier:
    return MarketClassifier(Thresholds())


def _market(
    *,
    question: str = "Will something happen?",
    tags: tuple[str, ...] = (),
    tag_ids: tuple[str, ...] = (),
    fee_type: str | None = None,
    sports_market_type: str | None = None,
    game_start_time: datetime | None = None,
    resolution_text: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    event_start_time: datetime | None = None,
) -> Market:
    return Market(
        condition_id=ConditionId("0xabc"),
        question=question,
        outcomes=(Outcome(token_id=ClobTokenId("1"), label="Yes", side=OutcomeSide.YES),),
        active=True,
        closed=False,
        accepting_orders=True,
        tags=tags,
        tag_ids=tag_ids,
        fee_type=fee_type,
        sports_market_type=sports_market_type,
        game_start_time=game_start_time,
        resolution_text=resolution_text,
        start_date=start_date,
        end_date=end_date,
        event_start_time=event_start_time,
    )


def _match(**kw: object) -> Market:
    """A market with the venue's evidence that it is a real fixture."""
    kw.setdefault("sports_market_type", "moneyline")
    kw.setdefault("game_start_time", NOW + timedelta(hours=3))
    return _market(**kw)  # type: ignore[arg-type]


# --- Tier 1: venue tags ---------------------------------------------------
@pytest.mark.parametrize(
    ("tag_id", "expected"),
    [
        ("100350", MarketCategory.FOOTBALL),
        ("517", MarketCategory.CRICKET),
        ("124", MarketCategory.TENNIS),
        ("102880", MarketCategory.BADMINTON),
    ],
)
def test_sport_tag_places_a_match_market(
    classifier: MarketClassifier, tag_id: str, expected: MarketCategory
) -> None:
    result = classifier.classify(_match(tag_ids=("1", tag_id)))
    assert result.category is expected
    assert result.confidence >= Decimal("0.9")


def test_tag_beats_a_misleading_question(classifier: MarketClassifier) -> None:
    """Tags are checked first because question text lies. This cricket fixture
    mentions a football keyword; the tag decides."""
    result = classifier.classify(
        _match(question="Cricket: Kolkata vs Punjab — football stadium leg", tag_ids=("517",))
    )
    assert result.category is MarketCategory.CRICKET


def test_politics_tag(classifier: MarketClassifier) -> None:
    assert classifier.classify(_market(tag_ids=("264",))).category is MarketCategory.POLITICS


# --- The match-evidence gate ---------------------------------------------
def test_sport_tagged_award_market_is_not_a_game(classifier: MarketClassifier) -> None:
    """Observed live: of 20 sport-tagged markets sampled, none carried a market
    type — they were awards and season milestones. Handing one to a model that reads
    score and clock would feed it state the market cannot have."""
    result = classifier.classify(
        _market(question="Will Kylian Mbappé win the 2026 Ballon d'Or?", tag_ids=("100350",))
    )
    assert result.category is MarketCategory.OTHER_SPORTS
    assert "not treated as a game" in result.rationale


def test_game_start_time_alone_is_enough_evidence(classifier: MarketClassifier) -> None:
    """A scheduled fixture without a market type is still a fixture."""
    result = classifier.classify(
        _market(tag_ids=("517",), game_start_time=NOW + timedelta(hours=2))
    )
    assert result.category is MarketCategory.CRICKET


def test_other_sports_has_no_engine_so_it_cannot_trade(
    classifier: MarketClassifier,
) -> None:
    """The demotion is safe because OTHER_SPORTS resolves to no engine. This is the
    property that makes it the right destination rather than UNKNOWN: it records
    what the market is, and still will not trade."""
    result = classifier.classify(_market(tag_ids=("100350",)))
    assert result.category is MarketCategory.OTHER_SPORTS
    assert result.category not in {
        MarketCategory.FOOTBALL,
        MarketCategory.CRICKET,
        MarketCategory.TENNIS,
        MarketCategory.BADMINTON,
    }


def test_generic_sports_tag_without_a_league(classifier: MarketClassifier) -> None:
    """Knowing a market is sport but not which sport is different from not knowing
    what it is, and the rationale should say so."""
    result = classifier.classify(_market(tag_ids=("1", "100639"), tags=("sports", "golf")))
    assert result.category is MarketCategory.OTHER_SPORTS
    assert "league unrecognised" in result.rationale


# --- Tie breaking ---------------------------------------------------------
def test_politics_and_geopolitics_tie_resolves_to_the_specific_one(
    classifier: MarketClassifier,
) -> None:
    """Observed live on every "leader out by" market. Both tags are correct, so
    calling it ambiguous would refuse to trade a market we understand."""
    result = classifier.classify(
        _market(question="Putin out as President of Russia by 2026?", tag_ids=("2", "100265"))
    )
    assert result.category is MarketCategory.GEOPOLITICS
    assert "tie resolved" in result.rationale


def test_unrelated_tie_stays_unknown(classifier: MarketClassifier) -> None:
    """A tie between cricket and politics is not a close call — it means the tags or
    our reading of them are wrong, and abstaining is the safe response."""
    result = classifier.classify(_match(tag_ids=("517", "264")))
    assert result.category is MarketCategory.UNKNOWN
    assert "ambiguous" in result.rationale


# --- Tier 2: keywords ----------------------------------------------------
def test_keywords_are_used_only_without_tags(classifier: MarketClassifier) -> None:
    result = classifier.classify(_market(question="Who wins the presidential election in France?"))
    assert result.category is MarketCategory.POLITICS
    assert "keyword" in result.rationale


def test_keyword_matching_is_word_bounded(classifier: MarketClassifier) -> None:
    """A substring search matches "ipl" inside "multiple" and puts every market
    mentioning a president into politics."""
    result = classifier.classify(_market(question="Will multiple airlines merge in 2026?"))
    assert result.category is MarketCategory.UNKNOWN


def test_nothing_matching_is_unknown(classifier: MarketClassifier) -> None:
    result = classifier.classify(_market(question="Will the widget ship on time?"))
    assert result.category is MarketCategory.UNKNOWN
    assert result.confidence == 0


# --- Tier 3: fee_type ----------------------------------------------------
def test_fee_type_corroborates(classifier: MarketClassifier) -> None:
    with_fee = classifier.classify(_market(tag_ids=("264",), fee_type="politics_fees"))
    without = classifier.classify(_market(tag_ids=("264",)))
    assert with_fee.confidence > without.confidence


def test_fee_type_contradiction_forces_an_abstention(
    classifier: MarketClassifier,
) -> None:
    """The venue disagreeing with us about a market's category is a reason to stand
    down, not to shade a number — so the penalty is large enough to cross the
    threshold."""
    result = classifier.classify(_match(tag_ids=("517",), fee_type="politics_fees"))
    assert result.category is MarketCategory.UNKNOWN


def test_fee_type_never_creates_a_verdict(classifier: MarketClassifier) -> None:
    """Too coarse to classify on, and absent on many markets."""
    result = classifier.classify(_market(question="Nothing matches", fee_type="sports_fees_v2"))
    assert result.category is MarketCategory.UNKNOWN


# --- Short-dated crypto --------------------------------------------------
def test_sub_hourly_crypto_becomes_its_own_category(
    classifier: MarketClassifier,
) -> None:
    """The distinguishing feature is the window, not the asset: over minutes the
    whole probability is a barrier problem against a reference price, which is a
    different question rather than a faster one.

    Note the shape: the market opened a day before the five minutes it settles on.
    That is what the venue actually sends, and an earlier version of this test
    invented ``start_date=NOW, end_date=NOW+5m`` instead -- a payload the venue
    never produces -- which is why it passed while the classifier misread every
    real one.
    """
    result = classifier.classify(
        _market(
            question="Bitcoin Up or Down - September 13, 8:50AM-8:55AM ET",
            tag_ids=("21",),
            start_date=NOW - timedelta(hours=24),
            event_start_time=NOW,
            end_date=NOW + timedelta(minutes=5),
        )
    )
    assert result.category is MarketCategory.BTC_5M


def test_real_shaped_short_dated_market_is_not_read_as_long_horizon() -> None:
    """The regression, stated as arithmetic.

    ``end_date - start_date`` is 86,400s on this market and the contest is 300s --
    off by 288x, in the direction that routes a five-minute barrier problem into a
    long-horizon forecast model.
    """
    market = _market(
        start_date=NOW - timedelta(hours=24),
        event_start_time=NOW,
        end_date=NOW + timedelta(minutes=5),
    )
    assert market.contest_window_seconds() == 300.0
    assert (market.end_date - market.start_date).total_seconds() == 86700.0  # type: ignore[operator]


def test_unknown_contest_window_is_not_promoted(classifier: MarketClassifier) -> None:
    """No ``event_start_time`` means discovery did not fetch it, not "long horizon".

    Promoting on a guess is the failure; staying ``CRYPTO`` and saying why is the
    correct degradation.
    """
    result = classifier.classify(
        _market(
            question="Bitcoin Up or Down - 8:50AM-8:55AM ET",
            tag_ids=("21",),
            start_date=NOW - timedelta(hours=24),
            end_date=NOW + timedelta(minutes=5),
        )
    )
    assert result.category is MarketCategory.CRYPTO
    assert "contest window unknown" in result.rationale


def test_long_horizon_crypto_stays_crypto(classifier: MarketClassifier) -> None:
    result = classifier.classify(
        _market(
            question="Will Bitcoin hit $150k by December 2026?",
            tag_ids=("21",),
            start_date=NOW,
            event_start_time=NOW,
            end_date=NOW + timedelta(days=200),
        )
    )
    assert result.category is MarketCategory.CRYPTO


def test_window_not_time_remaining_decides(classifier: MarketClassifier) -> None:
    """Time remaining cannot answer this: every market is short-dated an hour before
    it settles. Only the market's own window separates the two."""
    result = classifier.classify(
        _market(
            question="Will Bitcoin hit $150k?",
            tag_ids=("21",),
            start_date=NOW - timedelta(days=300),
            end_date=NOW + timedelta(minutes=10),
        )
    )
    assert result.category is MarketCategory.CRYPTO


def test_crypto_without_dates_is_not_promoted(classifier: MarketClassifier) -> None:
    result = classifier.classify(_market(question="Bitcoin?", tag_ids=("21",)))
    assert result.category is MarketCategory.CRYPTO


# --- Threshold behaviour -------------------------------------------------
def test_raising_the_threshold_demotes_keyword_only_verdicts() -> None:
    """Keyword confidence sits just above the default threshold on purpose, so
    tightening the threshold at all stops the system trusting text alone."""
    strict = MarketClassifier(Thresholds(classification_min_confidence=Decimal("0.90")))
    result = strict.classify(_market(question="Who wins the presidential election?"))
    assert result.category is MarketCategory.UNKNOWN

    default = MarketClassifier(Thresholds())
    assert (
        default.classify(_market(question="Who wins the presidential election?")).category
        is MarketCategory.POLITICS
    )


def test_unknown_carries_its_reason(classifier: MarketClassifier) -> None:
    """Rejections are journalled, and a rejection without a reason cannot tell a
    well-calibrated gate from one that never fires."""
    result = classifier.classify(_market(question="Unmatched question"))
    assert result.rationale
    assert result.category is MarketCategory.UNKNOWN


# --- Venue-seeded tag map ------------------------------------------------
async def test_sports_tags_are_learned_from_the_venue() -> None:
    """``get_sports()`` publishes each league with its tag ids, so a league the venue
    adds becomes classifiable without a release."""

    class _FakeClient:
        async def get_sports(self) -> list[object]:
            from types import SimpleNamespace

            return [
                SimpleNamespace(sport="cricpsl", tags="1,100639,517,103805"),
                SimpleNamespace(sport="badworld", tags="1,102880,999111"),
                SimpleNamespace(sport="nfl", tags="1,100639,555"),
            ]

    classifier = MarketClassifier(Thresholds())
    added = await classifier.load_sports_tags(_FakeClient())
    assert added >= 2

    result = classifier.classify(_match(tag_ids=("103805",)))
    assert result.category is MarketCategory.CRICKET


async def test_generic_tags_are_never_mapped_to_a_sport() -> None:
    """Generic ids appear on every league, so mapping them to whichever sport was
    iterated first would poison the whole map."""
    from types import SimpleNamespace

    class _FakeClient:
        async def get_sports(self) -> list[object]:
            return [SimpleNamespace(sport="cricpsl", tags="1,100639,517")]

    classifier = MarketClassifier(Thresholds())
    await classifier.load_sports_tags(_FakeClient())
    result = classifier.classify(_match(tag_ids=("1", "100639")))
    assert result.category is MarketCategory.OTHER_SPORTS


async def test_venue_failure_degrades_coverage_not_classification() -> None:
    """A venue hiccup must not stop classification: the static map still works."""

    class _Broken:
        async def get_sports(self) -> list[object]:
            raise RuntimeError("gamma down")

    classifier = MarketClassifier(Thresholds())
    assert await classifier.load_sports_tags(_Broken()) == 0
    assert classifier.classify(_match(tag_ids=("517",))).category is MarketCategory.CRICKET


def test_an_equity_up_or_down_market_is_not_crypto(classifier: MarketClassifier) -> None:
    """**§92.** ``up-or-down`` is a *format* tag, not an asset-class one, and the venue
    has extended the format to equities.

    Measured live: with ``102127`` mapped to CRYPTO, Apple, Microsoft, Amazon, Google,
    Meta, Tesla, Nvidia, Netflix and more all classified as **CRYPTO at 0.95
    confidence**. Their windows are 23,400s so nothing promoted them to BTC_5M today --
    but the moment the venue lists a short-dated equity window, that routes a stock to a
    model that prices barriers against a Chainlink *crypto* TWAP.
    """
    apple = _market(
        question="Will AAPL close up or down on September 14?",
        tags=("aapl", "equities", "up-or-down", "daily", "finance", "stocks"),
        tag_ids=("102680", "102676", "102127", "102281", "120", "103665"),
        event_start_time=NOW,
        end_date=NOW + timedelta(seconds=23_400),
    )
    verdict = classifier.classify(apple)
    assert verdict.category is not MarketCategory.CRYPTO
    assert verdict.category is not MarketCategory.BTC_5M


def test_a_short_dated_equity_window_still_never_reaches_the_crypto_model(
    classifier: MarketClassifier,
) -> None:
    """The case that makes the one above urgent rather than tidy: the same market with a
    five-minute window must still not become BTC_5M."""
    apple_5m = _market(
        question="Will AAPL be up or down?",
        tags=("aapl", "equities", "up-or-down"),
        tag_ids=("102680", "102676", "102127"),
        event_start_time=NOW,
        end_date=NOW + timedelta(minutes=5),
    )
    assert classifier.classify(apple_5m).category is not MarketCategory.BTC_5M


def test_a_crypto_up_down_market_is_still_reached(classifier: MarketClassifier) -> None:
    """The fix must not cost the markets it was protecting. ``crypto-prices`` (1312) and
    ``crypto`` (21) were carried by 68 of 68 live crypto up/down events and by none of
    the equity ones, so they are the tags that actually answer the question."""
    btc = _market(
        question="Bitcoin Up or Down?",
        tags=("up-or-down", "crypto-prices", "crypto", "btc", "5M"),
        tag_ids=("102127", "1312", "102169", "21", "102892"),
        event_start_time=NOW,
        end_date=NOW + timedelta(minutes=5),
    )
    assert classifier.classify(btc).category is MarketCategory.BTC_5M


def test_the_venue_does_publish_a_cadence_tag(classifier: MarketClassifier) -> None:
    """**Retracts part of §80**, which recorded that ``102892`` "appears on none of the
    live up/down events" and concluded the cadence is not published as a tag at all.

    Measured again: it appears on 48 of 68 live crypto up/down events, and on exactly
    those whose slug says ``5m`` -- 48 of 48, no disagreement. The earlier reading came
    from the same run whose sweep was returning stale windows, so it was measuring the
    wrong sample rather than the wrong field.
    """
    from deepflow.pipeline.classifier import CATEGORY_TAG_IDS

    assert "102892" in CATEGORY_TAG_IDS[MarketCategory.BTC_5M]
