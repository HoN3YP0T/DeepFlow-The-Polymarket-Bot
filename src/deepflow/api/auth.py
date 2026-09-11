"""Dashboard authentication. Section 29.

The dashboard exposes Emergency Stop, Cancel All Orders and Close All
Positions. An unauthenticated dashboard is a remote kill switch for anyone who
can reach the port, so authentication is required in every mode that touches
the venue (enforced in ``config.settings``).

Destructive controls require a second, explicit confirmation beyond being
authenticated, and every use is written to the audit log with the actor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    VIEWER = "VIEWER"
    """Read-only."""
    OPERATOR = "OPERATOR"
    """May pause, resume and adjust thresholds."""
    ADMIN = "ADMIN"
    """May arm live mode and trigger emergency actions."""


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    role: Role

    def can(self, required: Role) -> bool:
        order = (Role.VIEWER, Role.OPERATOR, Role.ADMIN)
        return order.index(self.role) >= order.index(required)


def create_token(principal: Principal, *, secret: str, ttl_seconds: int) -> str:
    raise NotImplementedError("auth.create_token")


def verify_token(token: str, *, secret: str) -> Principal:
    raise NotImplementedError("auth.verify_token")
