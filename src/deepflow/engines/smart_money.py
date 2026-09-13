"""Whale and smart-money engine. Sections 11-12.

Two distinct jobs, kept separate on purpose:

* **Detection** -- large trades, new positions, increases, reductions, exits,
  clusters of wallets entering the same side. This is observation.
* **Scoring** -- how much a given wallet's action should count. This is
  judgement, and it is earned from realized performance, ROI, category
  specialization, entry timing and position significance relative to that
  wallet's own book.

Size is a detection trigger, never a score. A wallet is not smart because it is
large; plenty of large wallets are consistently wrong, and following them is
worse than trading nothing because the sizing feels justified.

The output is a weighted feature into the probability and EV engines, capped by
``SmartMoneyThresholds.max_weight_in_signal``. It cannot, on its own, produce a
trade -- blind copying inherits their entry price, their horizon and their
hedges, none of which we can see.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import SmartMoneyThresholds
from deepflow.core.domain import SmartMoneyEntry, SmartMoneySignal, WalletPerformance
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId, WalletAddress
from deepflow.ports.wallet_intel import WalletIntelPort

log = get_logger(__name__)


#: Maximum contribution of each scoring component. They sum to 100.
#:
#: Realized profit dominates deliberately. Measured live on a real wallet: 85 of its
#: last 100 resolved positions won, and its cumulative realized PnL was **-964 USDC**
#: (§75) -- many small wins against a few large losses, which is the exact profile a
#: high-probability strategy must not follow. A hit rate is not skill; money is.
WEIGHT_REALIZED_PNL: Final = 40
WEIGHT_WIN_RATE: Final = 20
WEIGHT_EXPERIENCE: Final = 15
WEIGHT_CONVICTION: Final = 15
WEIGHT_TIMING: Final = 10
"""Unearnable from the available data, and therefore never awarded. Entry timing
relative to the subsequent move needs each trade priced against the market's path
afterwards; the Data API gives trades and a price history but no join between them, and
inventing a proxy would put 10 points of unearned credit on every wallet. Kept as a
named zero so the gap is visible rather than forgotten."""

#: Realized profit that earns the full weight. Above this the component saturates --
#: the difference between a very good wallet and an exceptional one is not what this
#: score is for, and the tail is where survivorship bias lives.
PNL_SATURATION_USDC: Final = Decimal(50_000)

#: Below this many resolved positions a hit rate is noise, so the component is withheld
#: rather than estimated. Ten coin flips produce a 70% "hit rate" often enough.
MIN_CLOSED_FOR_WIN_RATE: Final = 20

#: A wallet with fewer resolved markets than this is unproven, whatever its PnL says.
MIN_MARKETS_FOR_EXPERIENCE: Final = 25

#: Score ceiling for a wallet that has lost money over the window.
#:
#: Below ``SmartMoneyThresholds.min_smart_score`` (70) by construction, so a losing
#: whale can never gate a trade however large or confident its position. This is the
#: one hard rule in the scorer: size is a detection trigger, never a score.
LOSING_WALLET_CEILING: Final = 25

#: Score for a wallet the venue knows nothing about. Unproven is not average.
UNPROVEN_SCORE: Final = 0


class SmartMoneyEngine:
    """Detects notable wallet activity and scores it."""

    def __init__(self, intel: WalletIntelPort, thresholds: SmartMoneyThresholds) -> None:
        self._intel = intel
        self._thresholds = thresholds

    async def analyse(
        self, condition_id: ConditionId, *, since: datetime | None = None
    ) -> SmartMoneySignal:
        """Build the smart-money picture for one market.

        Detection first, then scoring, and the order is the design: a wallet is *noticed*
        because its trade is large and *counted* because its record is good. Conflating
        the two is how a system ends up following whales.

        Exits are tracked as carefully as entries. A scored wallet unwinding a position we
        are about to enter is a stronger message than a new wallet entering, and both go
        to the dashboard.

        Returns a signal scoring **zero** when nothing clears the detection floor, or when
        the trade feed is unavailable. Zero here means "no evidence", and because the
        signal is a weighted feature capped at ``max_weight_in_signal``, no evidence
        moves no probability -- which is the correct behaviour for a market nobody notable
        has touched.
        """
        try:
            trades = await self._intel.list_market_trades(condition_id, since=since)
        except Exception as exc:
            # Wallet intelligence is one weighted feature, never a gate. An unavailable
            # Data API must degrade the signal, not block a decision the price and model
            # can make on their own.
            log.warning(
                "smart_money.trades_unavailable",
                condition_id=str(condition_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            return self._empty(condition_id, "trade feed unavailable")

        notable = tuple(entry for entry in trades if self._is_notable(entry))
        if not notable:
            return self._empty(condition_id, f"no trade above {self._thresholds.min_notional_usdc}")

        scores = await self._score_wallets({entry.wallet for entry in notable})
        credited = tuple(
            entry
            for entry in notable
            if scores.get(entry.wallet, 0) >= self._thresholds.min_smart_score
        )

        entries = tuple(e for e in credited if not e.is_exit)
        exits = tuple(e for e in credited if e.is_exit)
        wallets = {e.wallet for e in credited}

        return SmartMoneySignal(
            condition_id=condition_id,
            score=self._signal_score(credited, scores),
            entries=entries,
            exits=exits,
            combined_notional_usdc=sum((e.notional_usdc for e in credited), start=Decimal(0)),
            contributing_wallets=len(wallets),
            rationale=self._rationale(notable, credited, wallets),
        )

    async def score_wallet(self, wallet: WalletAddress) -> int:
        """SMART_MONEY_SCORE, 0-100. Earned from realized performance.

        Components and why each is weighted as it is:

        * **Realized PnL** (40) -- the only component that measures being *right about
          money*. Saturates, because the difference between very good and exceptional is
          mostly survivorship.
        * **Win rate on resolved positions** (20) -- withheld entirely below
          ``MIN_CLOSED_FOR_WIN_RATE``, because ten coin flips produce a 70% hit rate
          often enough. A rate without profit is explicitly not enough: a wallet winning
          85 of 100 while losing money is common at these prices, and this scorer must
          not reward it.
        * **Experience** (15) -- markets actually traded. Not skill, but thin history
          must not score high.
        * **Conviction** (15) -- the position's size relative to *that wallet's own
          book*. A $50k bet is conviction for one wallet and a rounding error for
          another, which is the only sense in which size is allowed to matter.
        * **Timing** (10) -- never awarded; see ``WEIGHT_TIMING``.

        Two hard behaviours:

        * A wallet the venue knows nothing about scores ``UNPROVEN_SCORE``. Unproven is
          not average, and averaging it would let a fresh wallet with a large trade clear
          the threshold.
        * A wallet that **lost money** over the window is capped at
          ``LOSING_WALLET_CEILING``, which sits below ``min_smart_score`` by
          construction. No amount of experience or conviction promotes a losing wallet.
        """
        performance = await self._performance(wallet)
        if performance is None:
            return UNPROVEN_SCORE

        score = (
            self._pnl_points(performance)
            + self._win_rate_points(performance)
            + self._experience_points(performance)
            + self._conviction_points(performance)
        )

        pnl = performance.realized_pnl_usdc
        if pnl is not None and pnl < 0:
            return min(score, LOSING_WALLET_CEILING)
        if pnl is None:
            # No measured profit is not neutral evidence: it is missing evidence about
            # the only thing that matters, so it is capped the same way a loss is.
            return min(score, LOSING_WALLET_CEILING)
        return min(100, score)

    # --- Components -------------------------------------------------------
    @staticmethod
    def _pnl_points(performance: WalletPerformance) -> int:
        pnl = performance.realized_pnl_usdc
        if pnl is None or pnl <= 0:
            return 0
        fraction = min(Decimal(1), pnl / PNL_SATURATION_USDC)
        return int(fraction * WEIGHT_REALIZED_PNL)

    @staticmethod
    def _win_rate_points(performance: WalletPerformance) -> int:
        """Zero unless enough positions actually resolved.

        Withheld rather than estimated: a hit rate over five positions is a coin-flip
        artifact, and the score it would produce is indistinguishable from a real one.
        """
        closed = performance.closed_positions or 0
        rate = performance.win_rate
        if rate is None or closed < MIN_CLOSED_FOR_WIN_RATE:
            return 0
        # Credit only the part above a coin flip. A 50% hit rate on binary outcomes is
        # what no information looks like, so paying for it would score noise.
        above_chance = max(Decimal(0), rate - Decimal("0.5")) * 2
        return int(above_chance * WEIGHT_WIN_RATE)

    @staticmethod
    def _experience_points(performance: WalletPerformance) -> int:
        markets = performance.markets_traded
        if markets is None:
            return 0
        fraction = min(Decimal(1), Decimal(markets) / Decimal(MIN_MARKETS_FOR_EXPERIENCE))
        return int(fraction * WEIGHT_EXPERIENCE)

    @staticmethod
    def _conviction_points(performance: WalletPerformance) -> int:
        """Volume traded relative to the wallet's own portfolio value.

        The one place size is allowed in, and only as a ratio. ``None`` on either side
        earns nothing rather than a default: an unknown book cannot tell us whether a
        position was a conviction bet.
        """
        volume = performance.volume_usdc
        portfolio = performance.portfolio_value_usdc
        if volume is None or portfolio is None or portfolio <= 0:
            return 0
        turnover = min(Decimal(1), volume / (portfolio * Decimal(10)))
        return int(turnover * WEIGHT_CONVICTION)

    # --- Aggregation ------------------------------------------------------
    def _signal_score(
        self, credited: tuple[SmartMoneyEntry, ...], scores: Mapping[WalletAddress, int]
    ) -> int:
        """Market-level score: the best credited wallet, plus a cluster bonus.

        The **maximum** rather than the mean, because one wallet with a strong record is
        real evidence and averaging it against weaker ones destroys exactly the signal
        being looked for. The cluster bonus is small and capped: several scored wallets
        taking the same side is corroboration, not multiplication.
        """
        if not credited:
            return 0
        best = max(scores.get(entry.wallet, 0) for entry in credited)
        wallets = {entry.wallet for entry in credited}
        if len(wallets) >= self._thresholds.min_wallets_for_cluster:
            best = min(100, best + 5)
        return best

    def _aggregate_weight(self, entries: tuple[SmartMoneyEntry, ...]) -> Decimal:
        """Combined weight, hard-capped by ``max_weight_in_signal``.

        The cap is the point of the method. Copying whales is not a strategy: blind
        following inherits their entry price, their horizon and their hedges, none of
        which are visible from here. Even unanimous, well-scored wallets move a
        probability by at most this much.
        """
        if not entries:
            return Decimal(0)
        notional = sum((e.notional_usdc for e in entries), start=Decimal(0))
        if notional <= 0:
            return Decimal(0)
        floor = self._thresholds.min_notional_usdc or Decimal(1)
        # Log-free and deliberately crude: multiples of the detection floor, one tenth of
        # the cap each. Precision here would be false -- there is no calibration behind
        # any of these numbers yet, and the cap is what makes that safe.
        multiples = notional / floor
        return min(
            self._thresholds.max_weight_in_signal,
            multiples * self._thresholds.max_weight_in_signal / 10,
        )

    # --- Internals --------------------------------------------------------
    async def _score_wallets(self, wallets: set[WalletAddress]) -> dict[WalletAddress, int]:
        """Score each distinct wallet once.

        A market's trade list repeats the same wallets, and each score costs several Data
        API calls; scoring per trade would multiply that by the trade count for no new
        information.
        """
        return {wallet: await self.score_wallet(wallet) for wallet in sorted(wallets)}

    async def _performance(self, wallet: WalletAddress) -> WalletPerformance | None:
        since = datetime.now(UTC) - timedelta(days=self._thresholds.lookback_days)
        try:
            return await self._intel.get_wallet_performance(wallet, since=since)
        except Exception as exc:
            # An unscoreable wallet is unproven, not average. Failing the whole analysis
            # because one lookup broke would suppress evidence from the wallets that did
            # resolve.
            log.warning("smart_money.wallet_unscoreable", wallet=str(wallet), error=str(exc))
            return None

    def _is_notable(self, entry: SmartMoneyEntry) -> bool:
        """Whether an action clears the detection floor."""
        return entry.notional_usdc >= self._thresholds.min_notional_usdc

    @staticmethod
    def _empty(condition_id: ConditionId, reason: str) -> SmartMoneySignal:
        return SmartMoneySignal(condition_id=condition_id, score=0, rationale=reason)

    def _rationale(
        self,
        notable: tuple[SmartMoneyEntry, ...],
        credited: tuple[SmartMoneyEntry, ...],
        wallets: set[WalletAddress],
    ) -> str:
        """Why the score is what it is, in one line for the journal.

        Records the *rejected* count as well, because "twelve large trades, none from a
        wallet with a record" is a materially different market from "no large trades",
        and a rationale that omits it makes a well-calibrated filter look like a dead
        feed.
        """
        if not credited:
            return (
                f"{len(notable)} notable trade(s), none from a wallet scoring "
                f"{self._thresholds.min_smart_score}+"
            )
        exits = sum(1 for entry in credited if entry.is_exit)
        return (
            f"{len(wallets)} scored wallet(s) across {len(credited)} of {len(notable)} "
            f"notable trade(s); {exits} exiting"
        )
