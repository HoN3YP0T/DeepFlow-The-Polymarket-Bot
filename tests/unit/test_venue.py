"""Venue rules, checked against the values Polymarket publishes.

These tests are regression protection against the venue, not against us. Each
expectation is copied from a documented table, so a failure means either the
venue changed its rules or someone "simplified" the arithmetic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from deepflow.adapters.polymarket import venue


# --- Fees -----------------------------------------------------------------
# /trading/fees, "Fee Tables (100 Shares)". Crypto rate 0.07, sports 0.05,
# politics 0.04. Values are the documented USD fees for 100 shares.
@pytest.mark.parametrize(
    ("rate", "price", "expected"),
    [
        # Crypto: peaks at 1.75 on 0.50 and is symmetric about it.
        ("0.07", "0.50", "1.75"),
        ("0.07", "0.30", "1.47"),
        ("0.07", "0.70", "1.47"),
        ("0.07", "0.90", "0.63"),
        ("0.07", "0.10", "0.63"),
        ("0.07", "0.95", "0.3325"),
        # Sports: peaks at 1.25.
        ("0.05", "0.50", "1.25"),
        ("0.05", "0.85", "0.6375"),
        # Politics / finance / tech / mentions: peaks at 1.00.
        ("0.04", "0.50", "1.00"),
        ("0.04", "0.95", "0.19"),
        ("0.04", "0.99", "0.0396"),
        # Geopolitics markets are fee-free.
        ("0", "0.50", "0"),
    ],
)
def test_taker_fee_matches_published_tables(rate: str, price: str, expected: str) -> None:
    fee = venue.taker_fee(shares=Decimal(100), price=Decimal(price), rate=Decimal(rate))
    assert fee == Decimal(expected)


def test_taker_fee_is_symmetric_about_one_half() -> None:
    """The collateral fee at p equals the fee at 1 - p.

    Worth pinning because it is the counter-intuitive half of the fee model: the
    dollar cost of trading a 95c contract is the same as a 5c one, even though
    it is a twentieth of the *relative* cost.
    """
    rate = Decimal("0.05")
    for p in ("0.01", "0.15", "0.37", "0.49"):
        low = venue.taker_fee(shares=Decimal(500), price=Decimal(p), rate=rate)
        high = venue.taker_fee(shares=Decimal(500), price=Decimal(1) - Decimal(p), rate=rate)
        assert low == high


def test_taker_fee_bps_grows_as_price_falls() -> None:
    """In bps of notional the fee is ``rate * (1 - p)``, so cheap contracts cost
    proportionally more. This is the number that enters the EV cost stack."""
    rate = Decimal("0.04")
    at_95 = venue.taker_fee_bps(price=Decimal("0.95"), rate=rate)
    at_50 = venue.taker_fee_bps(price=Decimal("0.50"), rate=rate)
    assert at_95 < at_50
    # rate * (1 - 0.95) = 0.002 -> 20 bps
    assert at_95 == pytest.approx(Decimal(20), abs=Decimal("0.5"))


def test_taker_fee_rounds_to_five_decimals() -> None:
    """Fees round to 5dp; the smallest chargeable fee is 0.00001."""
    fee = venue.taker_fee(shares=Decimal("0.001"), price=Decimal("0.99"), rate=Decimal("0.04"))
    assert -fee.as_tuple().exponent <= venue.FEE_DECIMALS


@pytest.mark.parametrize("price", ["0", "1", "1.5"])
def test_taker_fee_outside_the_open_interval_is_zero(price: str) -> None:
    assert venue.taker_fee(shares=Decimal(100), price=Decimal(price), rate=Decimal("0.05")) == 0


# --- Tick grid ------------------------------------------------------------
def test_precision_table_is_not_derivable_from_the_exponent() -> None:
    """0.005 and 0.001 share a precision row, as do 0.0025 and 0.0001.

    This is the trap the table exists to avoid: computing price decimals from
    the tick's own exponent gives 0.005 three decimals (right) and 0.0025 four
    (right) but by luck, and gets the amount decimals wrong either way.
    """
    assert venue.precision_for_tick(Decimal("0.005")) == (3, 2, 5)
    assert venue.precision_for_tick(Decimal("0.001")) == (3, 2, 5)
    assert venue.precision_for_tick(Decimal("0.0025")) == (4, 2, 6)
    assert venue.precision_for_tick(Decimal("0.0001")) == (4, 2, 6)


def test_unsupported_tick_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported tick size"):
        venue.precision_for_tick(Decimal("0.02"))


def test_every_documented_tick_size_has_a_precision_row() -> None:
    assert set(venue.TICK_SIZES) == set(venue.PRECISION_BY_TICK)


def test_round_price_never_worsens_the_trade() -> None:
    """A BUY rounds down and a SELL rounds up, so snapping to the grid can only
    improve the price. Rounding to nearest would let a sub-tick BUY cross deeper
    into the book than the strategy authorised."""
    tick = Decimal("0.01")
    assert venue.round_price_to_tick(Decimal("0.9267"), tick, side_is_buy=True) == Decimal("0.92")
    assert venue.round_price_to_tick(Decimal("0.9267"), tick, side_is_buy=False) == Decimal("0.93")


def test_round_price_conforms_to_the_grid() -> None:
    for tick in venue.TICK_SIZES:
        price = venue.round_price_to_tick(Decimal("0.87431"), tick, side_is_buy=True)
        assert venue.conforms_to_tick(price, tick), (tick, price)


def test_round_price_stays_inside_the_tradeable_interval() -> None:
    tick = Decimal("0.01")
    assert venue.round_price_to_tick(Decimal("0.0001"), tick, side_is_buy=True) >= venue.MIN_PRICE
    assert venue.round_price_to_tick(Decimal("1.2"), tick, side_is_buy=False) <= venue.MAX_PRICE


def test_shares_round_down() -> None:
    """Down, not nearest: rounding up can push the notional past the collateral
    on hand, which the venue reports as an allowance problem."""
    assert venue.round_shares(Decimal("10.999"), Decimal("0.01")) == Decimal("10.99")


def test_notional_rounding_follows_the_two_step_rule() -> None:
    """Documented worked example: 10 shares at 0.52 encodes as 5.20 / 5200000."""
    tick = Decimal("0.01")
    amount = venue.round_notional(Decimal("10") * Decimal("0.52"), tick)
    assert amount == Decimal("5.2000")
    assert venue.to_amount_integer(amount) == 5_200_000


def test_notional_rounding_does_not_lose_a_unit_to_truncation() -> None:
    """The round-up-then-down step is what stops 5.19999999 becoming 5.1999."""
    tick = Decimal("0.01")  # 4 amount decimals
    assert venue.round_notional(Decimal("5.19999999999"), tick) == Decimal("5.2000")


# --- Exchange selection ---------------------------------------------------
def test_exchange_depends_on_neg_risk() -> None:
    assert venue.exchange_for(negative_risk=False) == venue.CTF_EXCHANGE
    assert venue.exchange_for(negative_risk=True) == venue.NEG_RISK_CTF_EXCHANGE
    assert venue.CTF_EXCHANGE != venue.NEG_RISK_CTF_EXCHANGE


# --- GTD lifetime ---------------------------------------------------------
def test_gtd_expiration_adds_the_security_margin() -> None:
    """The venue expires a GTD order a minute early, so an hour of real life
    needs ``now + 60 + 3600``."""
    expiry = venue.gtd_expiration(now_epoch_seconds=1_000_000, lifetime_seconds=3600)
    assert expiry == 1_000_000 + 60 + 3600


def test_gtd_refuses_a_short_lifetime() -> None:
    """A 10-second working order is not expressible as GTD. Stretching it to the
    venue minimum would leave the order resting ~12x longer than intended, so
    this raises instead."""
    with pytest.raises(ValueError, match="below the venue minimum"):
        venue.gtd_expiration(now_epoch_seconds=1_000_000, lifetime_seconds=10)


def test_gtd_minimum_effective_lifetime_is_two_minutes() -> None:
    assert venue.GTD_MIN_EFFECTIVE_LIFETIME_SECONDS == 120


# --- Failure classification ----------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 404])
def test_client_errors_are_definitive(status: int) -> None:
    assert venue.is_definitive_rejection(status)


@pytest.mark.parametrize("status", [425, 429, 500, 502, 503, 504])
def test_everything_else_is_not_definitive(status: int) -> None:
    """The whole duplicate-order failure mode lives here: anything that is not
    provably a refusal must be treated as uncertain and reconciled, never
    retried."""
    assert not venue.is_definitive_rejection(status)


def test_restart_and_mode_statuses_are_retryable() -> None:
    assert venue.STATUS_ENGINE_RESTARTING in venue.RETRYABLE_STATUSES
    assert venue.STATUS_RATE_LIMITED in venue.RETRYABLE_STATUSES
    assert venue.STATUS_UNAVAILABLE in venue.RETRYABLE_STATUSES
