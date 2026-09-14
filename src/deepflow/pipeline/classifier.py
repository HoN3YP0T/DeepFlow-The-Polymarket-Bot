"""Market classification. Section 3.

Rule-based, in three tiers of decreasing trust:

1. **Venue tag ids.** Structured, numerically stable, and maintained by the venue
   itself. ``get_sports()`` publishes 465 leagues with their tag ids, so the
   sports half of the mapping can be *seeded from the venue* rather than
   hand-written -- see :meth:`MarketClassifier.load_sports_tags`.
2. **Keyword match** over the question and resolution text, used only when tags
   cannot place a market. Weaker by construction: "Will Arsenal's owner sell?" is
   a football keyword match and not a football market.
3. **``fee_type``** as a corroborating signal only. The venue exposes
   ``politics_fees`` / ``sports_fees_v2`` / ``crypto_fees_v2``, which is a real
   category hint, but it is coarse and often absent, so it can raise confidence
   in an existing verdict and never create one.

The important property is not accuracy, it is calibration at the low end. A
classifier that confidently mislabels a cricket market as football routes it to a
model whose state variables are meaningless for it. Anything below the configured
confidence threshold becomes ``UNKNOWN``, which is never traded -- so an honest
abstention costs one skipped market, and a confident error costs a position.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Final

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import Classification, Market
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: Venue tag ids that place a market in a category outright. Ids rather than
#: slugs: ``get_sports()`` joins on ids, and a slug rename would silently stop
#: matching where an id would not.
CATEGORY_TAG_IDS: Final[dict[MarketCategory, frozenset[str]]] = {
    MarketCategory.FOOTBALL: frozenset({"100350"}),  # soccer
    MarketCategory.CRICKET: frozenset({"517"}),
    MarketCategory.TENNIS: frozenset({"124"}),
    MarketCategory.BADMINTON: frozenset({"102880"}),
    MarketCategory.POLITICS: frozenset({"2", "144", "264", "1101", "101206"}),
    MarketCategory.GEOPOLITICS: frozenset({"100265", "101253", "366"}),
    # ``102127`` (``up-or-down``) and ``1312`` (``crypto-prices``) are carried by **all
    # 71** live up/down events, measured; they identify the *family*, not the cadence, and
    # the family spans 5m, 15m and 4h windows. So they map to CRYPTO and the window
    # arithmetic promotes to BTC_5M -- see ``_apply_short_dated``.
    MarketCategory.CRYPTO: frozenset({"21", "100328", "102127", "1312"}),
    # Deliberately empty. ``102892`` was recorded here as "the venue's own 5M cadence tag"
    # and appears on none of the live up/down events (§80); the cadence is not published as
    # a tag at all, which is why it has to be derived from the window. Left as an empty set
    # rather than deleted so the tag route stays visible as the thing that cannot answer
    # this question.
    MarketCategory.BTC_5M: frozenset(),
    MarketCategory.OTHER_SPORTS: frozenset({"64", "65"}),  # esports
}

#: Tag ids that say "this is a sports market" without saying which sport. Used to
#: route an unrecognised league to OTHER_SPORTS rather than to UNKNOWN, which is a
#: more honest verdict: we know what kind of market it is and have no model for it.
GENERIC_SPORTS_TAG_IDS: Final[frozenset[str]] = frozenset({"1", "100639"})

#: Keyword seeds per category, used only when tags cannot place a market.
#: Deliberately data, not code: the geopolitical set in particular must grow
#: without a release, and hardcoding today's conflicts guarantees the classifier
#: is stale by next quarter.
CATEGORY_SIGNALS: Final[dict[MarketCategory, tuple[str, ...]]] = {
    MarketCategory.FOOTBALL: ("football", "soccer", "premier league", "la liga", "uefa", "fifa"),
    MarketCategory.CRICKET: ("cricket", "odi", "t20", "test match", "ipl", "wicket"),
    MarketCategory.TENNIS: ("tennis", "atp", "wta", "wimbledon", "us open", "roland garros"),
    MarketCategory.BADMINTON: ("badminton", "bwf", "shuttlecock"),
    MarketCategory.BTC_5M: ("btc", "bitcoin"),
    # The venue runs the short-dated cadence on at least BTC, ETH, XRP and SOL, and
    # "xrp" matched nothing here until a live XRP market classified as UNKNOWN -- the
    # window bug had been masking the gap by never promoting any of them.
    MarketCategory.CRYPTO: (
        "crypto",
        "ethereum",
        "solana",
        "token",
        "xrp",
        "ripple",
        "dogecoin",
        "up or down",
    ),
    MarketCategory.POLITICS: ("election", "president", "senate", "parliament", "nominee"),
    MarketCategory.GEOPOLITICS: ("sanctions", "treaty", "summit", "diplomatic"),
    MarketCategory.WAR_CONFLICT: ("strike", "invasion", "offensive", "military action"),
    MarketCategory.CEASEFIRE: ("ceasefire", "truce", "armistice", "peace deal"),
}

#: ``fee_type`` prefix -> the categories it corroborates.
FEE_TYPE_CATEGORIES: Final[dict[str, frozenset[MarketCategory]]] = {
    "politics": frozenset({MarketCategory.POLITICS, MarketCategory.GEOPOLITICS}),
    "sports": frozenset(
        {
            MarketCategory.FOOTBALL,
            MarketCategory.CRICKET,
            MarketCategory.TENNIS,
            MarketCategory.BADMINTON,
            MarketCategory.OTHER_SPORTS,
        }
    ),
    "crypto": frozenset({MarketCategory.CRYPTO, MarketCategory.BTC_5M}),
}

#: Confidence awarded by each tier. A tag match is near-certain; a keyword match
#: sits deliberately just above the 0.75 default threshold so that a keyword hit
#: alone is enough to trade only while the threshold stays at its default, and
#: raising the threshold at all demotes keyword-only verdicts to UNKNOWN.
TAG_CONFIDENCE: Final = Decimal("0.95")
KEYWORD_CONFIDENCE: Final = Decimal("0.78")
GENERIC_SPORTS_CONFIDENCE: Final = Decimal("0.85")
FEE_TYPE_BONUS: Final = Decimal("0.03")
FEE_TYPE_PENALTY: Final = Decimal("0.35")
"""Withheld when ``fee_type`` contradicts the verdict. Large on purpose: the
venue disagreeing with us about a market's category is a reason to stand down,
not to shade a number."""

#: Below this separation between the top two candidates, the market is UNKNOWN
#: however high the leader scores. Two categories fitting equally well means the
#: market is ambiguous, and a coin flip between two models is worse than neither.
MIN_SEPARATION: Final = Decimal("0.10")

#: Categories whose engines model **in-play match state**. A market in one of these
#: must be an actual game, not merely about the sport -- see :func:`_is_match`.
MATCH_CATEGORIES: Final[frozenset[MarketCategory]] = frozenset(
    {
        MarketCategory.FOOTBALL,
        MarketCategory.CRICKET,
        MarketCategory.TENNIS,
        MarketCategory.BADMINTON,
    }
)

#: Precedence for breaking a tie between *related* categories, most specific first.
#: A market tagged both ``politics`` and ``geopolitics`` is genuinely both, and
#: calling that ambiguous would refuse to trade a market we understand perfectly.
#: A tie between unrelated categories is different -- that means a tagging problem
#: or a market we have misread -- and stays UNKNOWN.
RELATED_PRECEDENCE: Final[tuple[tuple[MarketCategory, ...], ...]] = (
    (MarketCategory.GEOPOLITICS, MarketCategory.POLITICS),
    (MarketCategory.BTC_5M, MarketCategory.CRYPTO),
    (MarketCategory.CEASEFIRE, MarketCategory.WAR_CONFLICT, MarketCategory.GEOPOLITICS),
)

#: Sub-hourly expiry is what makes a short-dated crypto market a different
#: modelling problem -- a barrier crossing rather than a forecast -- rather than a
#: faster version of one.
SHORT_DATED_CRYPTO_SECONDS: Final = 3600


def _is_match(market: Market) -> bool:
    """Whether this market prices an actual game.

    Evidence the venue gives for a real fixture: a ``sports_market_type``
    (moneyline / spreads / totals) or a scheduled ``game_start_time``. Every match
    market sampled live carried both; awards and milestone markets carried neither
    while still being tagged with the sport.
    """
    return bool(market.sports_market_type or market.game_start_time)


def _break_related_tie(first: MarketCategory, second: MarketCategory) -> MarketCategory | None:
    """Resolve a tie between related categories, or ``None`` if they are unrelated.

    ``None`` is the important return: a tie between, say, CRICKET and POLITICS is
    not a close call, it is a sign the tags or our reading of them are wrong, and
    the safe response is to abstain rather than pick.
    """
    for group in RELATED_PRECEDENCE:
        if first in group and second in group:
            return group[min(group.index(first), group.index(second))]
    return None


class MarketClassifier:
    """Assigns a category and a confidence to a discovered market."""

    def __init__(self, thresholds: Thresholds) -> None:
        self._thresholds = thresholds
        self._tag_to_category: dict[str, MarketCategory] = {
            tag_id: category for category, tag_ids in CATEGORY_TAG_IDS.items() for tag_id in tag_ids
        }

    async def load_sports_tags(self, public_client: Any) -> int:
        """Extend the tag map from the venue's own league metadata.

        ``get_sports()`` returns each league with a comma-separated list of tag
        ids, so a new league the venue adds becomes classifiable without a
        release. Only leagues whose sport we can name are mapped; the rest are left
        to the generic sports tags, which is the honest outcome -- knowing a market
        is sport and not which sport is different from not knowing what it is.

        Returns the number of tag ids added. Failure is survivable: the static map
        still works, so a venue hiccup degrades coverage rather than stopping
        classification.
        """
        prefixes = {
            "cric": MarketCategory.CRICKET,
            "bad": MarketCategory.BADMINTON,
            "tennis": MarketCategory.TENNIS,
            "atp": MarketCategory.TENNIS,
            "wta": MarketCategory.TENNIS,
        }
        added = 0
        try:
            sports = await public_client.get_sports()
        except Exception:
            log.warning("classifier.sports_tags_unavailable", exc_info=True)
            return 0

        for sport in sports:
            name = str(getattr(sport, "sport", "") or "").lower()
            category = next(
                (cat for prefix, cat in prefixes.items() if name.startswith(prefix)), None
            )
            if category is None:
                continue
            for tag_id in str(getattr(sport, "tags", "") or "").split(","):
                tag_id = tag_id.strip()
                # Generic ids appear on every league, so mapping them to whichever
                # sport happened to be iterated first would poison the whole map.
                if not tag_id or tag_id in GENERIC_SPORTS_TAG_IDS:
                    continue
                if tag_id not in self._tag_to_category:
                    self._tag_to_category[tag_id] = category
                    added += 1

        log.info("classifier.sports_tags_loaded", leagues=len(sports), tag_ids_added=added)
        return added

    def classify(self, market: Market) -> Classification:
        """Classify ``market``."""
        scores: dict[MarketCategory, Decimal] = {}
        rationale: list[str] = []

        tag_hits = self._tag_hits(market)
        for category, matched in tag_hits.items():
            scores[category] = TAG_CONFIDENCE
            rationale.append(f"tag {matched}->{category.value}")

        if not scores:
            for category, matched in self._keyword_hits(market).items():
                scores[category] = KEYWORD_CONFIDENCE
                rationale.append(f"keyword {matched!r}->{category.value}")

        if not scores and self._is_generic_sports(market):
            # Known to be sport, unknown which. OTHER_SPORTS has no engine, so this
            # still will not trade -- but it lands in a category that says why.
            return Classification(
                category=MarketCategory.OTHER_SPORTS,
                confidence=GENERIC_SPORTS_CONFIDENCE,
                rationale="generic sports tag, league unrecognised",
                matched_signals=("sports",),
            )

        if not scores:
            return self.unknown("no tag or keyword matched")

        scores = self._apply_fee_type(market, scores, rationale)
        scores = self._apply_short_dated_crypto(market, scores, rationale)

        # A sport tag says what the market is about, not that it is a game. Of 20
        # sport-tagged markets sampled live, none carried a market type -- they were
        # awards and season milestones. Handing one to a model that reads score and
        # clock would feed it state the market cannot have.
        if not _is_match(market):
            demoted = MATCH_CATEGORIES.intersection(scores)
            if demoted:
                for category in demoted:
                    del scores[category]
                rationale.append(
                    f"no match evidence; {', '.join(sorted(c.value for c in demoted))} "
                    "not treated as a game"
                )
                if not scores:
                    return Classification(
                        category=MarketCategory.OTHER_SPORTS,
                        confidence=GENERIC_SPORTS_CONFIDENCE,
                        rationale="; ".join(rationale),
                        matched_signals=tuple(rationale),
                    )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, best_score = ranked[0]

        if len(ranked) > 1 and best_score - ranked[1][1] < MIN_SEPARATION:
            resolved = _break_related_tie(best, ranked[1][0])
            if resolved is None:
                return self.unknown(
                    f"ambiguous between {best.value} and {ranked[1][0].value} "
                    f"({best_score} vs {ranked[1][1]})"
                )
            best = resolved
            best_score = scores[resolved]
            rationale.append(f"tie resolved to {resolved.value} by specificity")

        if best_score < self._thresholds.classification_min_confidence:
            return self.unknown(
                f"best candidate {best.value} scored {best_score}, below "
                f"{self._thresholds.classification_min_confidence}"
            )

        return Classification(
            category=best,
            confidence=min(best_score, Decimal(1)),
            rationale="; ".join(rationale),
            matched_signals=tuple(rationale),
        )

    # --- Tiers ------------------------------------------------------------
    def _tag_hits(self, market: Market) -> dict[MarketCategory, str]:
        hits: dict[MarketCategory, str] = {}
        for tag_id in market.tag_ids:
            category = self._tag_to_category.get(tag_id)
            if category is not None and category not in hits:
                hits[category] = tag_id
        return hits

    def _keyword_hits(self, market: Market) -> dict[MarketCategory, str]:
        """Whole-word keyword matches over question and resolution text.

        Word-bounded deliberately: a substring search puts every market containing
        "president" into politics, and matches "ipl" inside "multiple".
        """
        haystack = " ".join(
            part.lower() for part in (market.question, market.resolution_text or "") if part
        )
        hits: dict[MarketCategory, str] = {}
        for category, keywords in CATEGORY_SIGNALS.items():
            for keyword in keywords:
                if re.search(rf"\b{re.escape(keyword)}\b", haystack):
                    hits.setdefault(category, keyword)
                    break
        return hits

    @staticmethod
    def _is_generic_sports(market: Market) -> bool:
        return bool(GENERIC_SPORTS_TAG_IDS.intersection(market.tag_ids))

    def _apply_fee_type(
        self,
        market: Market,
        scores: dict[MarketCategory, Decimal],
        rationale: list[str],
    ) -> dict[MarketCategory, Decimal]:
        """Corroborate or contradict using the venue's own fee category.

        Never creates a verdict -- it is too coarse, and absent on many markets --
        but the venue disagreeing with us is worth acting on.
        """
        raw = (market.fee_type or "").lower()
        if not raw:
            return scores

        expected = next(
            (cats for prefix, cats in FEE_TYPE_CATEGORIES.items() if raw.startswith(prefix)),
            None,
        )
        if expected is None:
            return scores

        adjusted: dict[MarketCategory, Decimal] = {}
        for category, score in scores.items():
            if category in expected:
                adjusted[category] = score + FEE_TYPE_BONUS
            else:
                adjusted[category] = score - FEE_TYPE_PENALTY
                rationale.append(f"fee_type {raw!r} contradicts {category.value}")
        return adjusted

    def _apply_short_dated_crypto(
        self,
        market: Market,
        scores: dict[MarketCategory, Decimal],
        rationale: list[str],
    ) -> dict[MarketCategory, Decimal]:
        """Promote a crypto market with a sub-hourly window to ``BTC_5M``.

        The distinguishing feature is not the asset but the horizon: over minutes,
        drift is negligible and the whole probability is a barrier problem against a
        reference price. A keyword match on "bitcoin" alone cannot tell those apart,
        and pricing a 5-minute strike market with a general crypto model is not
        approximately right -- it is a different question.

        The window comes from :meth:`Market.contest_window_seconds`, which measures
        ``end_date - event_start_time``. This used to measure from ``start_date``,
        and the difference is not academic: a live ``btc-updown-5m`` market opens
        about 24 hours before the five minutes it settles on, so the old arithmetic
        returned ~86,200s where the truth is 300s. **Every short-dated crypto market
        on the venue was therefore classified as plain ``CRYPTO``** -- and the same
        arithmetic, used in a scan, is what produced the recorded finding that no
        short-dated crypto markets existed at all. Twenty were open when that was
        corrected, across BTC, ETH, XRP and SOL.

        A market whose ``event_start_time`` was never fetched is left as ``CRYPTO``
        rather than guessed at: the field is only reachable through ``list_events``,
        so its absence means "discovery did not look", and a promotion on an unknown
        window is what this method exists to avoid.
        """
        if MarketCategory.CRYPTO not in scores:
            return scores

        window_seconds = market.contest_window_seconds()
        if window_seconds is None:
            rationale.append("contest window unknown (event_start_time not fetched)")
            return scores
        if not 0 < window_seconds <= SHORT_DATED_CRYPTO_SECONDS:
            return scores

        promoted: dict[MarketCategory, Decimal] = {
            category: score
            for category, score in scores.items()
            if category is not MarketCategory.CRYPTO
        }
        promoted[MarketCategory.BTC_5M] = scores[MarketCategory.CRYPTO]
        rationale.append(f"window {int(window_seconds)}s -> short-dated crypto")
        return promoted

    @staticmethod
    def unknown(reason: str) -> Classification:
        """The safe fallback. Never auto-traded."""
        return Classification(
            category=MarketCategory.UNKNOWN,
            confidence=Decimal(0),
            rationale=reason,
        )
