"""Idempotency tests. Section 19: a retry must never become a second order."""

from __future__ import annotations

from decimal import Decimal

from deepflow.core.enums import OrderSide
from deepflow.core.types import ClobTokenId
from deepflow.execution.idempotency import build_client_key

TOKEN = ClobTokenId("0xtoken")
D = Decimal


def _key(**overrides: object) -> str:
    base = {
        "token_id": TOKEN,
        "side": OrderSide.BUY,
        "price": D("0.94"),
        "size": D(100),
        "epoch_seconds": 1_700_000_000.0,
    }
    base.update(overrides)
    return build_client_key(**base)  # type: ignore[arg-type]


def test_same_intent_yields_same_key() -> None:
    """The retry case. Two submissions of one intent are one order."""
    assert _key() == _key()


def test_retry_seconds_later_collides() -> None:
    """Within the window, a retry is recognised as the same intent."""
    assert _key() == _key(epoch_seconds=1_700_000_005.0)


def test_different_price_yields_different_key() -> None:
    assert _key() != _key(price=D("0.95"))


def test_different_size_yields_different_key() -> None:
    assert _key() != _key(size=D(101))


def test_different_side_yields_different_key() -> None:
    assert _key() != _key(side=OrderSide.SELL)


def test_different_token_yields_different_key() -> None:
    assert _key() != _key(token_id=ClobTokenId("0xother"))


def test_later_window_yields_different_key() -> None:
    """A genuine re-entry much later must not be suppressed as a duplicate."""
    assert _key() != _key(epoch_seconds=1_700_000_000.0 + 3600)


def test_salt_allows_a_deliberate_repeat() -> None:
    """An intentional scale-in inside one window has to be an explicit act."""
    assert _key() != build_client_key(
        token_id=TOKEN,
        side=OrderSide.BUY,
        price=D("0.94"),
        size=D(100),
        epoch_seconds=1_700_000_000.0,
        salt="scale-in-2",
    )


def test_key_is_stable_and_bounded() -> None:
    key = _key()
    assert len(key) == 32
    assert key == _key()
