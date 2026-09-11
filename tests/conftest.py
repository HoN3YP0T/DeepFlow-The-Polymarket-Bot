"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deepflow.config.thresholds import RiskLimits
from deepflow.core.clock import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    """Deterministic clock. Freshness logic is time-dependent, so tests never
    use wall-clock time."""
    return ManualClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()
