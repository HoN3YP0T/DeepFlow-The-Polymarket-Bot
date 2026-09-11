"""Journal browsing. Section 23."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/journal", tags=["journal"])


@router.get("")
async def list_entries(kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Decision log, filterable by ENTERED / REJECTED / HELD / EXITED."""
    raise NotImplementedError("journal.list_entries")
