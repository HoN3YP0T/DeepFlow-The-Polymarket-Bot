"""Smart-money engine: size is a detection trigger, never a score.

The failure this suite exists to prevent is following whales. A large wallet is not an
informed one, and a system that conflates the two sizes its trades with borrowed
confidence. So the tests check the two halves separately — what gets *noticed*, and what
gets *counted* — and pin the one hard rule: a wallet that loses money can never clear the
threshold, however large or unanimous its position.

The calibration case is real and measured live (§75): a wallet that won 85 of its last
100 resolved positions while losing 964 USDC. A hit rate is not skill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.thresholds import SmartMoneyThresholds
from deepflow.core.domain import SmartMoneyEntry, WalletPerformance
from deepflow.core.enums import OrderSide
from deepflow.core.types import ConditionId, WalletAddress
from deepflow.engines.smart_money import (
    LOSING_WALLET_CEILING,
    UNPROVEN_SCORE,
    SmartMoneyEngine,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
CONDITION = ConditionId("0xcond")
GOOD = WalletAddress("0xgood")
BAD = WalletAddress("0xbad")
UNKNOWN = WalletAddress("0xunknown")


def _entry(
    *,
    wallet: WalletAddress = GOOD,
    notional: str = "25000",
    is_exit: bool = False,
) -> SmartMoneyEntry:
    return SmartMoneyEntry(
        wallet=wallet,
        side=OrderSide.SELL if is_exit else OrderSide.BUY,
        outcome_label="Yes",
        notional_usdc=Decimal(notional),
        entry_price=Decimal("0.92"),
        market_probability_at_entry=Decimal("0.92"),
        observed_at=NOW,
        is_exit=is_exit,
    )


def _performance(
    wallet: WalletAddress,
    *,
    pnl: str | None = "60000",
    volume: str | None = "500000",
    portfolio: str | None = "50000",
    markets: int | None = 300,
    closed: int | None = 100,
    winning: int | None = 75,
) -> WalletPerformance:
    return WalletPerformance(
        wallet=wallet,
        realized_pnl_usdc=Decimal(pnl) if pnl is not None else None,
        volume_usdc=Decimal(volume) if volume is not None else None,
        portfolio_value_usdc=Decimal(portfolio) if portfolio is not None else None,
        markets_traded=markets,
        closed_positions=closed,
        winning_positions=winning,
        trade_count=900,
        first_seen_at=NOW,
    )


class _Intel:
    def __init__(
        self,
        *,
        trades: list[SmartMoneyEntry] | None = None,
        performances: dict[WalletAddress, WalletPerformance] | None = None,
        trades_raise: Exception | None = None,
        performance_raise: Exception | None = None,
    ) -> None:
        self.trades = trades or []
        self.performances = performances or {}
        self.trades_raise = trades_raise
        self.performance_raise = performance_raise
        self.performance_calls: list[WalletAddress] = []

    async def list_market_trades(
        self, condition_id: ConditionId, *, since: datetime | None = None, limit: int = 200
    ) -> list[SmartMoneyEntry]:
        if self.trades_raise:
            raise self.trades_raise
        return self.trades

    async def list_market_holders(
        self, condition_id: ConditionId, *, limit: int = 100
    ) -> list[object]:
        return []

    async def get_wallet_performance(
        self, wallet: WalletAddress, *, since: datetime | None = None
    ) -> WalletPerformance | None:
        self.performance_calls.append(wallet)
        if self.performance_raise:
            raise self.performance_raise
        return self.performances.get(wallet)


def _engine(intel: _Intel, **overrides: object) -> SmartMoneyEngine:
    return SmartMoneyEngine(intel, SmartMoneyThresholds(**overrides))  # type: ignore[arg-type]


# --- Scoring: the hard rules ---------------------------------------------
@pytest.mark.asyncio
async def test_a_losing_wallet_can_never_clear_the_threshold() -> None:
    """The one hard rule. No amount of experience, volume or conviction promotes a
    wallet that has lost money — otherwise a large loser gates a trade."""
    intel = _Intel(performances={BAD: _performance(BAD, pnl="-50000", markets=5000)})
    score = await _engine(intel).score_wallet(BAD)
    assert score <= LOSING_WALLET_CEILING
    assert score < SmartMoneyThresholds().min_smart_score


@pytest.mark.asyncio
async def test_the_measured_case_a_high_hit_rate_that_loses_money() -> None:
    """Measured live: 85 of 100 resolved positions won, cumulative realized PnL -964.
    Many small wins against a few large losses is the exact profile a high-probability
    strategy must not copy."""
    intel = _Intel(
        performances={
            BAD: _performance(BAD, pnl="-964", closed=100, winning=85, markets=243),
        }
    )
    assert await _engine(intel).score_wallet(BAD) <= LOSING_WALLET_CEILING


@pytest.mark.asyncio
async def test_an_unknown_wallet_is_unproven_not_average() -> None:
    """Averaging an unknown wallet would let a fresh account with one large trade clear
    the threshold."""
    assert await _engine(_Intel()).score_wallet(UNKNOWN) == UNPROVEN_SCORE


@pytest.mark.asyncio
async def test_an_unmeasured_pnl_is_capped_like_a_loss() -> None:
    """Missing evidence about the only thing that matters is not neutral evidence."""
    intel = _Intel(performances={GOOD: _performance(GOOD, pnl=None)})
    assert await _engine(intel).score_wallet(GOOD) <= LOSING_WALLET_CEILING


@pytest.mark.asyncio
async def test_a_profitable_experienced_wallet_scores_well() -> None:
    intel = _Intel(performances={GOOD: _performance(GOOD)})
    assert await _engine(intel).score_wallet(GOOD) >= SmartMoneyThresholds().min_smart_score


@pytest.mark.asyncio
async def test_a_thin_win_rate_earns_nothing() -> None:
    """Ten coin flips produce a 70% hit rate often enough. Withheld, not estimated."""
    intel = _Intel(performances={GOOD: _performance(GOOD, closed=5, winning=5)})
    thin = await _engine(intel).score_wallet(GOOD)

    intel_full = _Intel(performances={GOOD: _performance(GOOD, closed=100, winning=100)})
    proven = await _engine(intel_full).score_wallet(GOOD)
    assert thin < proven


@pytest.mark.asyncio
async def test_a_coin_flip_hit_rate_earns_nothing() -> None:
    """50% on binary outcomes is what no information looks like."""
    intel = _Intel(performances={GOOD: _performance(GOOD, closed=100, winning=50)})
    flip = await _engine(intel).score_wallet(GOOD)
    intel_edge = _Intel(performances={GOOD: _performance(GOOD, closed=100, winning=80)})
    assert flip < await _engine(intel_edge).score_wallet(GOOD)


@pytest.mark.asyncio
async def test_size_alone_does_not_score() -> None:
    """A huge book with no realized profit is a whale, not a signal."""
    intel = _Intel(
        performances={
            BAD: _performance(BAD, pnl="0", volume="10000000", portfolio="5000000", markets=1000)
        }
    )
    assert await _engine(intel).score_wallet(BAD) < SmartMoneyThresholds().min_smart_score


# --- Detection ------------------------------------------------------------
@pytest.mark.asyncio
async def test_trades_below_the_floor_are_not_noticed() -> None:
    intel = _Intel(trades=[_entry(notional="100")], performances={GOOD: _performance(GOOD)})
    signal = await _engine(intel).analyse(CONDITION)
    assert signal.score == 0
    assert signal.entries == ()
    assert intel.performance_calls == []  # nothing notable, so nothing was scored


@pytest.mark.asyncio
async def test_a_notable_trade_from_an_unscored_wallet_is_recorded_but_not_credited() -> None:
    """"Twelve large trades, none from a wallet with a record" is a materially different
    market from "no large trades", and the rationale has to say which."""
    intel = _Intel(trades=[_entry(wallet=UNKNOWN)], performances={})
    signal = await _engine(intel).analyse(CONDITION)
    assert signal.score == 0
    assert signal.entries == ()
    assert "none from a wallet scoring" in signal.rationale


@pytest.mark.asyncio
async def test_exits_are_tracked_separately_from_entries() -> None:
    """A scored wallet unwinding a position we are about to enter is a stronger message
    than a new wallet entering."""
    intel = _Intel(
        trades=[_entry(), _entry(is_exit=True)],
        performances={GOOD: _performance(GOOD)},
    )
    signal = await _engine(intel).analyse(CONDITION)
    assert len(signal.entries) == 1
    assert len(signal.exits) == 1
    assert "1 exiting" in signal.rationale


@pytest.mark.asyncio
async def test_each_wallet_is_scored_once_however_often_it_trades() -> None:
    """A score costs several Data API calls; scoring per trade multiplies them for no
    new information."""
    intel = _Intel(
        trades=[_entry(), _entry(), _entry()],
        performances={GOOD: _performance(GOOD)},
    )
    await _engine(intel).analyse(CONDITION)
    assert intel.performance_calls == [GOOD]


@pytest.mark.asyncio
async def test_a_cluster_of_scored_wallets_corroborates_without_multiplying() -> None:
    """Several wallets on the same side is corroboration, not a multiplier — the bonus
    is small and capped."""
    other = WalletAddress("0xgood2")
    single = _Intel(trades=[_entry()], performances={GOOD: _performance(GOOD)})
    lone = await _engine(single, min_wallets_for_cluster=2).analyse(CONDITION)

    pair = _Intel(
        trades=[_entry(), _entry(wallet=other)],
        performances={GOOD: _performance(GOOD), other: _performance(other)},
    )
    clustered = await _engine(pair, min_wallets_for_cluster=2).analyse(CONDITION)
    assert clustered.score > lone.score
    assert clustered.score - lone.score <= 10
    assert clustered.contributing_wallets == 2


@pytest.mark.asyncio
async def test_the_market_score_is_the_best_wallet_not_the_mean() -> None:
    """Averaging a strong record against weaker ones destroys the signal being looked
    for."""
    weak = WalletAddress("0xweak")
    intel = _Intel(
        trades=[_entry(), _entry(wallet=weak)],
        performances={
            GOOD: _performance(GOOD),
            weak: _performance(weak, pnl="60000", markets=300, closed=100, winning=72),
        },
    )
    signal = await _engine(intel, min_wallets_for_cluster=99).analyse(CONDITION)
    best = max(
        await _engine(intel).score_wallet(GOOD),
        await _engine(intel).score_wallet(weak),
    )
    assert signal.score == best


# --- Degradation ----------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unavailable_trade_feed_degrades_to_no_evidence() -> None:
    """Wallet intelligence is one weighted feature, never a gate. A broken Data API must
    not block a decision the price and the model can make on their own."""
    intel = _Intel(trades_raise=RuntimeError("data api down"))
    signal = await _engine(intel).analyse(CONDITION)
    assert signal.score == 0
    assert signal.rationale == "trade feed unavailable"


@pytest.mark.asyncio
async def test_one_unscoreable_wallet_does_not_suppress_the_others() -> None:
    intel = _Intel(
        trades=[_entry()],
        performances={GOOD: _performance(GOOD)},
        performance_raise=RuntimeError("stats down"),
    )
    signal = await _engine(intel).analyse(CONDITION)
    assert signal.score == 0  # unscoreable is unproven
    assert "none from a wallet scoring" in signal.rationale


# --- The cap --------------------------------------------------------------
def test_aggregate_weight_is_hard_capped() -> None:
    """Copying whales is not a strategy: blind following inherits their entry price,
    horizon and hedges, none of which are visible from here. Even unanimous, well-scored
    wallets move a probability by at most this much."""
    thresholds = SmartMoneyThresholds()
    engine = SmartMoneyEngine(_Intel(), thresholds)  # type: ignore[arg-type]
    enormous = tuple(_entry(notional="10000000") for _ in range(20))
    assert engine._aggregate_weight(enormous) == thresholds.max_weight_in_signal
    assert engine._aggregate_weight(()) == Decimal(0)


def test_timing_credit_is_never_awarded() -> None:
    """It cannot be earned from the available data — the Data API offers no join between
    a trade and the market's subsequent path — so it is a named zero rather than a
    proxy that would credit every wallet 10 unearned points."""
    from deepflow.engines.smart_money import (
        WEIGHT_CONVICTION,
        WEIGHT_EXPERIENCE,
        WEIGHT_REALIZED_PNL,
        WEIGHT_TIMING,
        WEIGHT_WIN_RATE,
    )

    awarded = WEIGHT_REALIZED_PNL + WEIGHT_WIN_RATE + WEIGHT_EXPERIENCE + WEIGHT_CONVICTION
    assert awarded + WEIGHT_TIMING == 100
    assert awarded == 90
