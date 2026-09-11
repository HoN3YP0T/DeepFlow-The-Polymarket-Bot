"""Settings tests. Section 25/29: live execution is disabled by default."""

from __future__ import annotations

import pytest

from deepflow.config.settings import LIVE_ACK_PHRASE, Settings
from deepflow.core.enums import RunMode
from deepflow.core.errors import LiveModeNotConfirmedError


def test_default_mode_is_paper() -> None:
    settings = Settings(_env_file=None)
    assert settings.mode is RunMode.PAPER
    assert not settings.sends_real_orders


def test_live_requires_confirmation_flag() -> None:
    with pytest.raises(LiveModeNotConfirmedError, match="LIVE_TRADING_CONFIRMED"):
        Settings(_env_file=None, mode=RunMode.LIVE)


def test_live_requires_exact_ack_phrase() -> None:
    with pytest.raises(LiveModeNotConfirmedError, match="LIVE_TRADING_ACK"):
        Settings(
            _env_file=None,
            mode=RunMode.LIVE,
            live_trading_confirmed=True,
            live_trading_ack="yes",
        )


def test_live_requires_credentials() -> None:
    """All three interlocks must agree; a stray env var alone cannot arm it."""
    with pytest.raises(LiveModeNotConfirmedError, match="private key"):
        Settings(
            _env_file=None,
            mode=RunMode.LIVE,
            live_trading_confirmed=True,
            live_trading_ack=LIVE_ACK_PHRASE,
        )


def test_shadow_requires_an_api_secret() -> None:
    """The dashboard exposes kill switches; it is never left open once the
    system is touching the venue."""
    with pytest.raises(LiveModeNotConfirmedError, match="JWT_SECRET"):
        Settings(_env_file=None, mode=RunMode.SHADOW)


def test_paper_mode_needs_no_credentials() -> None:
    settings = Settings(_env_file=None, mode=RunMode.PAPER)
    assert not settings.polymarket.is_authenticated
    assert not settings.sends_real_orders


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = Settings(_env_file=None)
    assert "deepflow:deepflow" not in repr(settings)
