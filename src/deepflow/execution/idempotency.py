"""Idempotency keys. Section 19.

The key is derived from the *intent*, not from the attempt. Two submissions of
the same intent produce the same key, so a retry can be recognised as a repeat
rather than becoming a second order.

The window term is what makes this work in practice: without it, a legitimate
re-entry into the same market at the same price an hour later would collide
with the original and be silently suppressed. The window is coarse enough that
a retry seconds later collides, and fine enough that a genuine later trade does
not.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from deepflow.core.enums import OrderSide
from deepflow.core.types import ClientOrderKey, ClobTokenId

#: Bucket width for the time component, in seconds.
DEFAULT_WINDOW_SECONDS = 60


def build_client_key(
    *,
    token_id: ClobTokenId,
    side: OrderSide,
    price: Decimal,
    size: Decimal,
    epoch_seconds: float,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    salt: str = "",
) -> ClientOrderKey:
    """Deterministic key for one order intent.

    ``salt`` distinguishes intentionally repeated orders inside one window --
    a deliberate scale-in, for instance -- which must be an explicit act by the
    caller rather than something that happens by accident.
    """
    bucket = int(epoch_seconds // window_seconds)
    material = f"{token_id}|{side}|{price:f}|{size:f}|{bucket}|{salt}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return ClientOrderKey(digest)
