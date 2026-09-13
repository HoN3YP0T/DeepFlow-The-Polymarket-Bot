"""Early exit engine. Section 9.

Continuously reevaluates every open position and returns one of HOLD, ADD,
PARTIAL_EXIT, FULL_EXIT or EMERGENCY_EXIT.

The engine's whole difficulty is one distinction: **noise versus state change.**

A position bought at 94% that ticks to 92% has not changed -- that is spread,
a single impatient seller, or a thin book being thin. Exiting there pays the
spread twice and converts a positive-EV trade into a realized loss, repeatedly.

A position bought at 94% where the underlying event has changed -- a goal back,
a red card, a wicket, a break of serve, a confirmed geopolitical development,
a BTC move through the strike -- is a different position, and the entry
probability is now irrelevant. That is where exits must be immediate and
aggressive.

So the design separates the two inputs. Price-derived signals (velocity,
opposing flow, smart-money exits) are treated as *evidence* and must clear a
noise band scaled to the market's own volatility. State-derived signals (a
verified event) bypass the band entirely and can trigger an emergency exit on
their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from deepflow.config.thresholds import ExitThresholds
from deepflow.core.domain import (
    ExitDecision,
    MarketSnapshot,
    Position,
    ProbabilityEstimate,
    SmartMoneySignal,
)
from deepflow.core.enums import ExitAction
from deepflow.core.logging import get_logger

log = get_logger(__name__)


def _volatility_of(snapshot: MarketSnapshot) -> Decimal | None:
    """The market's realized volatility, as far as the snapshot can say.

    ``price_velocity`` is the closest measured proxy the microstructure engine
    produces; its magnitude is what matters here, not its direction, since the band is
    symmetric. ``None`` stays ``None`` -- an unmeasured volatility means "fall back to
    the configured band", never "zero volatility, so every tick counts".
    """
    velocity = snapshot.microstructure.price_velocity
    return abs(velocity) if velocity is not None else None


def _opposing_flow(micro: object) -> Decimal | None:
    """Flow running against the position, or ``None`` when it was not measured.

    Sign convention: ``flow_imbalance`` is positive when buying dominates, so flow
    *against* a long is the negation. Returned as a positive magnitude so the caller
    never has to remember which way the sign ran.
    """
    imbalance = getattr(micro, "flow_imbalance", None)
    if imbalance is None:
        return None
    return -imbalance if imbalance < 0 else Decimal(0)


def _sub(left: Decimal | None, right: Decimal) -> Decimal | None:
    return None if left is None else left - right


@dataclass(frozen=True, slots=True)
class StateChange:
    """A verified change in the world the position is a bet on.

    Not derived from the book. It comes from the live-game sweep or an event pipeline --
    a goal, a red card, a wicket, a break of serve, a confirmed development, BTC
    crossing the strike -- and it is the only input that may trigger an emergency exit.

    ``against_position`` is what makes it actionable: a goal *for* the outcome we hold
    is a state change too, and exiting on it would sell the winner. The caller decides
    the direction, because only the caller knows which side the position is on; this
    engine must not infer it from a price that may not have moved yet.
    """

    detail: str
    against_position: bool = True


@dataclass(frozen=True, slots=True)
class ExitSignals:
    """Inputs to an exit decision.

    ``state_change_detected`` is deliberately distinct from every price-derived
    field. It is the only input permitted to trigger an emergency exit, because
    it is the only one that reflects the world rather than the order book.
    """

    current_probability: Decimal
    entry_probability: Decimal
    model_probability: Decimal | None
    probability_velocity: Decimal | None = None
    probability_acceleration: Decimal | None = None
    state_change_detected: bool = False
    state_change_detail: str = ""
    opposing_flow: Decimal | None = None
    smart_money_exiting: bool = False
    time_remaining_seconds: int | None = None
    current_net_ev: Decimal | None = None


class ExitEngine:
    """Decides what to do with an open position.

    The precedence order below is the design, not an implementation detail: a verified
    state change must not be able to lose to a price-derived signal, and nothing
    price-derived may reach an emergency exit.
    """

    def __init__(
        self,
        *,
        thresholds: ExitThresholds | None = None,
        noise_band: Decimal | None = None,
    ) -> None:
        self._thresholds = thresholds or ExitThresholds()
        self._noise_band = noise_band if noise_band is not None else self._thresholds.noise_band
        """Default probability move treated as noise. Scaled per market by the
        market's own realized volatility -- a fixed band is too tight on a
        volatile BTC market and far too loose on a settled football market."""

    async def evaluate(
        self,
        *,
        position: Position,
        snapshot: MarketSnapshot,
        estimate: ProbabilityEstimate | None,
        smart_money: SmartMoneySignal | None,
        state_change: StateChange | None = None,
        net_ev: Decimal | None = None,
    ) -> ExitDecision:
        """Decide an action for ``position``.

        Precedence, highest first. Each step returns; none of them accumulate:

        1. **EMERGENCY_EXIT** -- a verified underlying state change that invalidates
           the thesis. Immediate, full, and at market: the thesis is void, so failing
           to get out costs more than crossing the spread, which is the reverse of the
           trade-off everywhere else here.
        2. **FULL_EXIT** -- the model no longer supports the position, or net EV has
           gone negative, *and* the move is outside the noise band.
        3. **PARTIAL_EXIT** -- real deterioration that is not decisive. Reduce, keep
           the thesis.
        4. **ADD** -- the thesis strengthened and the price improved.
        5. **HOLD** -- the default, and where everything inside the band lands.

        A missing current price is a HOLD, not an exit. That asymmetry is deliberate:
        an unreadable book is a reason to look again, and selling into one converts a
        data problem into a realized loss at whatever price happens to be quoted.
        """
        signals = self._signals_from(
            position, snapshot, estimate, smart_money, state_change=state_change, net_ev=net_ev
        )
        if signals is None:
            return self.hold(position, "no closing price available; cannot mark the position")

        # 1. State change. Checked first and with no band, because it is the only
        # input that reflects the world rather than the order book.
        if signals.state_change_detected:
            detail = signals.state_change_detail or "verified state change"
            return ExitDecision(
                position_id=position.position_id,
                action=ExitAction.EMERGENCY_EXIT,
                exit_score=100,
                fraction=Decimal(1),
                reason=f"state change invalidates the thesis: {detail}",
                triggers=("state_change",),
            )

        band = self.noise_band_for(snapshot)
        score, triggers = self._score(signals, band=band)

        # 2 and 3. Price-derived deterioration, and only outside the band. The band
        # test sits here rather than inside the score so that a position can be scored
        # for the journal even when the answer is HOLD.
        if self._is_noise(signals, volatility=_volatility_of(snapshot)):
            return ExitDecision(
                position_id=position.position_id,
                action=ExitAction.HOLD,
                exit_score=score,
                reason=f"move of {self._adverse_move(signals)} is inside the {band} noise band",
                triggers=triggers,
            )

        # Negative net EV is its own exit condition, not a contribution to a score.
        # At the current price the trade no longer pays after costs, so there is nothing
        # left to be patient about -- and routing it through the score let it come out
        # at 30 against a partial threshold of 40, i.e. a position with no edge held on
        # the strength of a small price move. Scored deterioration is a judgement; this
        # is arithmetic.
        if signals.current_net_ev is not None and signals.current_net_ev < 0:
            return ExitDecision(
                position_id=position.position_id,
                action=ExitAction.FULL_EXIT,
                exit_score=max(score, self._thresholds.full_exit_score),
                fraction=Decimal(1),
                reason=f"net EV is {signals.current_net_ev} at the current price; no edge remains",
                triggers=triggers
                if "negative_net_ev" in triggers
                else (*triggers, "negative_net_ev"),
            )

        if score >= self._thresholds.full_exit_score:
            return ExitDecision(
                position_id=position.position_id,
                action=ExitAction.FULL_EXIT,
                exit_score=score,
                fraction=Decimal(1),
                reason=f"deterioration score {score} outside the {band} band",
                triggers=triggers,
            )

        if score >= self._thresholds.partial_exit_score:
            return ExitDecision(
                position_id=position.position_id,
                action=ExitAction.PARTIAL_EXIT,
                exit_score=score,
                fraction=self._thresholds.partial_exit_fraction,
                reason=f"partial deterioration, score {score}",
                triggers=triggers,
            )

        # 4. ADD. Last, because adding on a deteriorating position is the error this
        # ordering exists to prevent, and because it is a new trade: the caller sends
        # it through the same risk limits and safety gate as an entry.
        if self._should_add(signals):
            gain = _sub(signals.model_probability, signals.current_probability)
            return ExitDecision(
                position_id=position.position_id,
                action=ExitAction.ADD,
                exit_score=score,
                reason=f"model exceeds price by {gain}; thesis strengthened",
                triggers=(*triggers, "model_above_price"),
            )

        # Reached when the band was cleared (or bypassed) but the score is below the
        # partial threshold. The reason names the numbers rather than saying "intact",
        # because this is the row someone reads when asking whether the thresholds are
        # calibrated -- and it must not claim the move was outside the band when what
        # actually got it here was a bypass.
        adverse = self._adverse_move(signals)
        return ExitDecision(
            position_id=position.position_id,
            action=ExitAction.HOLD,
            exit_score=score,
            reason=(
                f"thesis intact; adverse move {adverse} against a {band} band, "
                f"score {score} below the partial threshold"
            ),
            triggers=triggers,
        )

    # --- The noise band ---------------------------------------------------
    def noise_band_for(self, snapshot: MarketSnapshot) -> Decimal:
        """The band this market's own behaviour justifies."""
        return self._band_from(_volatility_of(snapshot), snapshot.time_remaining_seconds)

    def _is_noise(self, signals: ExitSignals, *, volatility: Decimal | None) -> bool:
        """Whether an adverse move is inside the market's own noise band.

        Only the *adverse* move is measured. A position that moved in our favour is
        never noise-tested, because the question the band answers is "should we get
        out", and a favourable move is not a reason to.

        A negative net EV bypasses the band. It is not a price wobble: at the current
        price the trade no longer pays after costs, and holding it because the move was
        small is holding a position with no edge.
        """
        if signals.current_net_ev is not None and signals.current_net_ev < 0:
            return False
        band = self._band_from(volatility, signals.time_remaining_seconds)
        return self._adverse_move(signals) < band

    def _band_from(self, volatility: Decimal | None, remaining: int | None) -> Decimal:
        """The band, from the two inputs that shape it.

        Realized volatility when it is measured, clamped; the configured default when it
        is not. **Not zero when unmeasured** -- that would make every tick significant,
        which is the failure this whole class exists to avoid.

        Tightened inside the late window: with little time left to mean-revert, the same
        adverse move carries more information. A 2-point drop is noise at 60 minutes and
        a warning at 60 seconds.

        The single definition on purpose. It was briefly two -- one reading a snapshot,
        one reading the collected signals -- which is one rename away from a band that
        differs between the decision and the reason recorded for it.
        """
        if volatility is None:
            band = self._noise_band
        else:
            band = min(
                max(
                    abs(volatility) * self._thresholds.volatility_multiple,
                    self._thresholds.min_noise_band,
                ),
                self._thresholds.max_noise_band,
            )
        if remaining is not None and remaining <= self._thresholds.late_window_seconds:
            band *= self._thresholds.late_window_band_multiple
        return band

    @staticmethod
    def _adverse_move(signals: ExitSignals) -> Decimal:
        """How far the price has moved *against* the position, never below zero."""
        move = signals.entry_probability - signals.current_probability
        return move if move > 0 else Decimal(0)

    # --- Scoring ----------------------------------------------------------
    def _score(self, signals: ExitSignals, *, band: Decimal) -> tuple[int, tuple[str, ...]]:
        """A 0-100 deterioration score, and the names of what contributed.

        Deliberately additive and legible rather than a fitted function: this number
        goes in the journal, and a score nobody can decompose after a loss is a score
        nobody will trust enough to act on. The weights are ordered by how much each
        input tells us about the *position* rather than about the book.
        """
        score = 0
        triggers: list[str] = []

        model = signals.model_probability
        if model is not None:
            if model < signals.current_probability:
                # The model disagreeing with the market is the strongest price-derived
                # evidence available: it is the only input with a view of its own.
                shortfall = signals.current_probability - model
                score += min(50, int(shortfall * 100))
                triggers.append("model_below_price")
            if model < signals.entry_probability - band:
                score += 20
                triggers.append("model_below_entry")

        if signals.current_net_ev is not None and signals.current_net_ev < 0:
            score += 30
            triggers.append("negative_net_ev")

        adverse = self._adverse_move(signals)
        if adverse > band:
            score += min(20, int((adverse - band) * 100))
            triggers.append("adverse_move")

        if signals.probability_velocity is not None and signals.probability_velocity < 0:
            score += 10
            triggers.append("falling")

        if signals.smart_money_exiting:
            score += 10
            triggers.append("smart_money_exiting")

        if signals.opposing_flow is not None and signals.opposing_flow > 0:
            score += 5
            triggers.append("opposing_flow")

        return min(100, score), tuple(triggers)

    def _should_add(self, signals: ExitSignals) -> bool:
        """Whether the thesis strengthened enough to justify a second trade.

        Requires the model to exceed the *current* price by a real margin, not merely
        to exceed the entry. Adding because the price fell is averaging down, which is
        the same trade with worse odds unless the model still supports it.
        """
        if signals.model_probability is None:
            return False
        gain = signals.model_probability - signals.current_probability
        return gain >= self._thresholds.add_min_probability_gain

    # --- Inputs -----------------------------------------------------------
    def _signals_from(
        self,
        position: Position,
        snapshot: MarketSnapshot,
        estimate: ProbabilityEstimate | None,
        smart_money: SmartMoneySignal | None,
        *,
        state_change: StateChange | None = None,
        net_ev: Decimal | None = None,
    ) -> ExitSignals | None:
        """Collect the inputs, or ``None`` when the position cannot be marked.

        The current probability is the **bid we could hit**, not the mid: it is the
        price at which this position could actually be closed. On a wide outcome book
        the difference is the gap between a position that looks profitable and one that
        is.
        """
        book = snapshot.book_for(position.token_id)
        current = book.best_bid if book is not None else None
        if current is None:
            return None

        micro = snapshot.microstructure
        return ExitSignals(
            current_probability=current,
            entry_probability=position.entry_probability,
            model_probability=(estimate.calibrated_probability if estimate else None),
            probability_velocity=micro.price_velocity,
            probability_acceleration=micro.price_acceleration,
            opposing_flow=_opposing_flow(micro),
            smart_money_exiting=bool(smart_money and smart_money.exits),
            time_remaining_seconds=snapshot.time_remaining_seconds,
            state_change_detected=bool(state_change and state_change.against_position),
            state_change_detail=state_change.detail if state_change else "",
            current_net_ev=net_ev,
        )

    @staticmethod
    def hold(position: Position, reason: str) -> ExitDecision:
        """The default decision."""
        return ExitDecision(
            position_id=position.position_id,
            action=ExitAction.HOLD,
            exit_score=0,
            reason=reason,
        )
