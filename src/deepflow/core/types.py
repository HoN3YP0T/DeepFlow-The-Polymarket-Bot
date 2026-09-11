"""Primitive type aliases.

Distinct ``NewType`` aliases for identifiers that are all strings at runtime.
Mixing a ``ConditionId`` with a ``ClobTokenId`` is a real bug class in this
domain, so the type checker is asked to keep them apart.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NewType

# --- Identifiers ----------------------------------------------------------
ConditionId = NewType("ConditionId", str)
"""On-chain condition id identifying a market (0x-prefixed hex)."""

EventId = NewType("EventId", str)
"""Polymarket event id grouping one or more related markets."""

ClobTokenId = NewType("ClobTokenId", str)
"""CLOB asset id for a single outcome token (the thing you actually trade)."""

WalletAddress = NewType("WalletAddress", str)
OrderId = NewType("OrderId", str)
PositionId = NewType("PositionId", str)
SignalId = NewType("SignalId", str)

ClientOrderKey = NewType("ClientOrderKey", str)
"""Locally generated idempotency key, stable across retries of one intent."""

# --- Quantities -----------------------------------------------------------
# Money and prices are Decimal everywhere. Float rounding on a 0.97 probability
# market is the difference between positive and negative expected value.
Usdc = NewType("Usdc", Decimal)
Price = NewType("Price", Decimal)
"""Outcome token price in USDC, bounded (0, 1)."""

Probability = NewType("Probability", Decimal)
"""Calibrated probability in [0, 1]."""

Shares = NewType("Shares", Decimal)
BasisPoints = NewType("BasisPoints", int)

ZERO = Decimal(0)
ONE = Decimal(1)
