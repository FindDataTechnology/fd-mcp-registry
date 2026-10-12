"""Per-owner monthly call-grant counters (add-ecosystem-bridge 3.3).

``call_grant_usage`` MongoDB collection: one document per (owner, calendar
month) counting admitted ``tools/call`` requests. The owner is the patch-key
owner's username (``sub``): ALL of a caller's keys share one monthly pool —
keys are free to mint, so a per-key grant would multiply N-keys-fold.

Deliberately a two-step check-then-increment: the benign race (two concurrent
admits at limit-1 both pass, landing at limit+1) is accepted for a free-tier
gate; the atomic single-round alternatives need upsert filters that cannot
distinguish "no document yet" from "already over limit".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # motor/documentdb stay lazy: auth-server runs also import
    from motor.motor_asyncio import AsyncIOMotorDatabase  # noqa: F401

COLLECTION_NAME = "call_grant_usage"


def current_month(now: datetime | None = None) -> str:
    """Grant window key: ``YYYY-MM`` in UTC (matches the settle cadence)."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


@dataclass(frozen=True)
class GrantVerdict:
    """One admission decision: allowed with the running count, or exhausted."""

    allowed: bool
    used: int
    limit: int
    month: str


class CallGrantService:
    """Check-and-consume against the per-owner monthly counter."""

    def __init__(self, db: Any) -> None:
        self._collection = db[COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        await self._collection.create_index(
            [("owner", 1), ("month", 1)], unique=True
        )

    async def _current_count(self, owner: str, month: str) -> int:
        doc = await self._collection.find_one({"owner": owner, "month": month})
        return int(doc.get("count", 0)) if doc else 0

    async def check_and_consume(self, owner: str, limit: int, *, month: str | None = None) -> GrantVerdict:
        """Admit one call when under the limit and charge the counter.

        Refusal does NOT increment (a refused call consumed nothing); the
        benign overshoot race under concurrency is documented above.
        """
        month = month or current_month()
        used = await self._current_count(owner, month)
        if used >= limit:
            return GrantVerdict(allowed=False, used=used, limit=limit, month=month)
        await self._collection.update_one(
            {"owner": owner, "month": month},
            {"$inc": {"count": 1}, "$setOnInsert": {"owner": owner, "month": month}},
            upsert=True,
        )
        return GrantVerdict(allowed=True, used=used + 1, limit=limit, month=month)

    async def usage(self, owner: str, *, month: str | None = None) -> int:
        """Read-only count (owner visibility face)."""
        return await self._current_count(owner, month or current_month())


_singleton: CallGrantService | None = None


async def get_call_grant_service() -> CallGrantService:
    """Module-level singleton bound to the active DocumentDB client.

    Mirrors ``get_patch_key_service`` so the auth server's proxy path and the
    registry's owner-facing routes share one service/collection instance.
    """
    global _singleton
    if _singleton is None:
        from registry.repositories.documentdb.client import get_documentdb_client

        db = await get_documentdb_client()
        _singleton = CallGrantService(db)
    return _singleton


def reset_call_grant_singleton() -> None:
    """Test hook: drop the singleton so a fake service can take its place."""
    global _singleton
    _singleton = None


def doc_shape() -> dict[str, Any]:  # pragma: no cover - documentation helper
    return {"owner": "<username>", "month": "YYYY-MM", "count": 0}
