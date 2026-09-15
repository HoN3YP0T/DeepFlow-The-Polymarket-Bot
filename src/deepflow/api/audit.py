"""The audit trail for dashboard actions. Section 29.

Every control writes a row here **before** it acts, and that ordering is the point. A
record written afterwards is missing exactly the actions that mattered most: the ones that
crashed the process, hung, or half-succeeded. "We tried to stop trading and something went
wrong" is the single most important line in an incident review, and it only exists if the
attempt is recorded before the attempt is made.

A failure to record therefore **refuses the action**. This is the one place in the codebase
where losing a row is not survivable: everywhere else -- snapshots, predictions, journal
entries -- a lost write costs history, and the comments there say so. An unlogged
emergency stop costs the ability to say who stopped trading and when.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deepflow.adapters.persistence.models import AuditRow
from deepflow.api.auth import Principal
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.logging import get_logger

log = get_logger(__name__)


class AuditLog:
    """Append-only record of who did what through the dashboard."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        clock: Clock | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()

    async def record(
        self,
        principal: Principal,
        action: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Write one row, or raise.

        Raising is deliberate -- see the module docstring. The caller must not proceed with
        an action it could not record.
        """
        if self._sessions is None:
            # No database configured. Refusing is the only honest answer: the action would
            # otherwise happen with no trace, which is the state this class exists to
            # prevent.
            raise RuntimeError("audit log unavailable: refusing to act without a record")

        async with self._sessions() as session:
            await session.execute(
                insert(AuditRow).values(
                    actor=principal.subject,
                    action=action,
                    detail=dict(detail or {}) | {"role": principal.role.value},
                    occurred_at=self._clock.now(),
                )
            )
            await session.commit()
        log.info("audit.recorded", actor=principal.subject, action=action)

    async def recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first, for the dashboard's own audit panel.

        The trail is readable from the thing it audits on purpose: an operator who cannot
        see what was done last has to ask someone, and during an incident that is the
        slowest possible path to the answer.
        """
        if self._sessions is None:
            return []
        async with self._sessions() as session:
            result = await session.execute(
                select(AuditRow).order_by(AuditRow.id.desc()).limit(limit)
            )
            return [
                {
                    "id": row.id,
                    "actor": row.actor,
                    "action": row.action,
                    "detail": row.detail,
                    "occurred_at": row.occurred_at,
                }
                for row in result.scalars()
            ]
