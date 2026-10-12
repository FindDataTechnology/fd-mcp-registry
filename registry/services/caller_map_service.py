"""Pre-flight caller map service: entrance identity -> sub2api account.

Maps a wire-entrance caller (the validated ``sub``/``egress_user`` username)
to the sub2api account identifier the quota pre-flight should probe. Rows
live in the shared ``sub2api_caller_map`` MongoDB collection as
``{_id: username, value: "<sub2api user id | key:sk-...>", updated_at}``.

Two consumers share this module (mirrors ``patch_key_service``):

- the registry's console API (``registry.api.sub2api_caller_map_routes``)
  for admin-managed upsert/list/delete, written by the wire onboarding flow
  (cell-manager calls it with its admin patch key — add-customer-onboarding-
  automation task 3.1);
- the auth server's quota pre-flight (``auth_server/preflight_quota.py``)
  which reads the map on every request behind a short TTL cache, with the
  legacy ``SUB2API_CALLER_MAP`` env kept as a per-entry override layer
  (env wins) until it retires (task 3.2).

The value is deliberately an ACCOUNT IDENTIFIER (sub2api user id), never a
customer key: no credential material is duplicated into this collection
(design decision D3, grill 2026-10-08 Q7).
"""

import logging
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

COLLECTION_NAME: str = "sub2api_caller_map"


class CallerMapService:
    """CRUD for the entrance-caller -> sub2api-account mapping."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db[COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        """Idempotent index creation (``_id`` is the username; nothing else
        is queried, the index call exists for symmetry + future list sorts)."""
        await self._collection.create_index("updated_at")
        logger.info("Ensured indexes on %s (updated_at)", COLLECTION_NAME)

    async def get_map(self) -> dict[str, str]:
        """Whole map as ``{username: value}`` (the preflight reads it in one
        round-trip; collections stay small — one row per on-boarded customer).

        A datastore error is raised to the caller: the preflight layer decides
        its own degradation (env-only map), this service does not fail silently.
        """
        cursor = self._collection.find({}, {"value": 1})
        result: dict[str, str] = {}
        async for doc in cursor:
            value = doc.get("value")
            if doc.get("_id") and isinstance(value, str) and value.strip():
                result[str(doc["_id"])] = value.strip()
        return result

    async def upsert(self, *, username: str, value: str) -> None:
        """Create or update one mapping row (idempotent full replace of value)."""
        username = (username or "").strip()
        value = (value or "").strip()
        if not username or not value:
            raise ValueError("caller map upsert needs non-empty username and value")
        await self._collection.update_one(
            {"_id": username},
            {
                "$set": {"value": value, "updated_at": datetime.utcnow()},
            },
            upsert=True,
        )
        logger.info("Caller map upsert username=%s (account=%s)", username, value)

    async def delete(self, username: str) -> bool:
        """Remove one mapping; ``False`` when it was already absent."""
        result = await self._collection.delete_one({"_id": (username or "").strip()})
        return result.deleted_count > 0


_singleton: CallerMapService | None = None


async def get_caller_map_service() -> CallerMapService:
    """Module-level singleton bound to the active DocumentDB client.

    Mirrors ``get_patch_key_service`` so the registry routes and the auth
    server's pre-flight share one service/collection instance.
    """
    global _singleton
    if _singleton is None:
        # Imported lazily: the auth server also reaches this module and must
        # not pay the documentdb import at module import time.
        from registry.repositories.documentdb.client import get_documentdb_client

        db = await get_documentdb_client()
        _singleton = CallerMapService(db)
    return _singleton


def reset_caller_map_service_singleton() -> None:
    """Test hook: drop the singleton so the next getter re-binds to a new db."""
    global _singleton
    _singleton = None
