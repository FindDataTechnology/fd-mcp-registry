"""Mongo-backed budget counter for the hot-path pre-check.

A process-local counter is correct only within one gateway replica. Production
runs multiple, so the pre-check reads a shared, cluster-wide counter. Mongo is
already a hard dependency (registry + auth_server share the client) and the
replay store proves the fail-closed discipline; this reuses both.

The counter is a DERIVED index over the durable ledger, not a second source of
truth: it answers "how many calls/rows this run or tenant has used in the current
window" fast, and is reconciled against the PG ledger's aggregation at settle
time. If the counter store is unreachable, ``available()`` is False and the
caller fails closed (503), matching the spec's budget rule.

Change: establish-agent-principal-plane (task group 3).
"""

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

COUNTER_COLLECTION: str = "ledger_budget_counters"


class MongoBudgetCounter:
    """Atomic per-scope counters in MongoDB."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db[COUNTER_COLLECTION]

    async def ensure_indexes(self) -> None:
        await self._col.create_index("scope", unique=True)

    async def available(self) -> bool:
        try:
            await self._col.database.command("ping")
            return True
        except Exception as exc:  # noqa: BLE001 - any failure => fail closed
            logger.warning("budget counter store unavailable: %s", exc)
            return False

    async def peek(self, scope: str) -> dict[str, int]:
        doc = await self._col.find_one({"scope": scope})
        if not doc:
            return {}
        return {
            "calls": int(doc.get("calls", 0)),
            "rows": int(doc.get("rows", 0)),
            "tokens": int(doc.get("tokens", 0)),
        }

    async def consume(
        self, scope: str, *, rows: int = 0, calls: int = 1, tokens: int = 0
    ) -> None:
        from pymongo import ReturnDocument

        await self._col.find_one_and_update(
            {"scope": scope},
            {
                "$inc": {"calls": calls, "rows": rows, "tokens": tokens},
                "$set": {"updated_at": datetime.now(UTC)},
                "$setOnInsert": {"scope": scope},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )


_mongo_counter: MongoBudgetCounter | None = None


async def get_mongo_budget_counter() -> MongoBudgetCounter:
    global _mongo_counter
    if _mongo_counter is None:
        from registry.repositories.documentdb.client import get_documentdb_client

        db = await get_documentdb_client()
        _mongo_counter = MongoBudgetCounter(db)
    return _mongo_counter


def reset_mongo_budget_counter() -> None:
    global _mongo_counter
    _mongo_counter = None
