"""Principal + tenant registration service (MongoDB).

Behavior contracts live in the change specs (principal-registry). This module
implements them:

- three types share one ``principals`` collection; ``type`` is an explicit field,
  not inferred from an id prefix;
- an ``agent`` MUST have an owner; suspending an owner cascades ``suspended`` to
  its agents, and restoring the owner does NOT auto-restore them;
- ``revoked`` is terminal — any attempt to move a revoked principal back is
  refused;
- a tenant's ``isolation_tier`` has NO default; a principal in a tenant without
  one is refused at grant-issuance time with ``tenant_tier_unresolved``.

Change: establish-agent-principal-plane (task group 1).
"""

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from registry.schemas.principal import (
    Principal,
    PrincipalCreate,
    PrincipalStatus,
    PrincipalType,
    Tenant,
    TenantUpsert,
)

logger = logging.getLogger(__name__)

PRINCIPALS_COLLECTION: str = "principals"
TENANTS_COLLECTION: str = "tenants"


class PrincipalNotFound(Exception):
    """No principal with the given id."""


class PrincipalTypeImmutable(Exception):
    """Attempt to change a principal's type after registration."""


class PrincipalStatusTransitionError(Exception):
    """Illegal lifecycle transition (e.g. revoking → active)."""


class TenantNotFound(Exception):
    """No tenant with the given id."""


class TenantTierUnresolved(Exception):
    """Tenant has no isolation_tier; grant issuance must be refused."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PrincipalService:
    """CRUD + lifecycle for principals and tenants."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._principals = db[PRINCIPALS_COLLECTION]
        self._tenants = db[TENANTS_COLLECTION]

    async def ensure_indexes(self) -> None:
        await self._principals.create_index("id", unique=True)
        await self._principals.create_index("tenant_id")
        await self._principals.create_index("owner_principal_id")
        await self._tenants.create_index("id", unique=True)
        logger.info(
            "Ensured indexes on %s / %s", PRINCIPALS_COLLECTION, TENANTS_COLLECTION
        )

    # ----- tenants ---------------------------------------------------------

    async def upsert_tenant(self, body: TenantUpsert) -> Tenant:
        now = _utcnow()
        existing = await self._tenants.find_one({"id": body.id})
        doc: dict[str, Any] = {
            "id": body.id,
            "name": body.name,
            "isolation_tier": body.isolation_tier.value
            if body.isolation_tier is not None
            else None,
            "billing_binding": body.billing_binding,
            "entitlements": body.entitlements,
            "updated_at": now,
        }
        if existing is None:
            doc["created_at"] = now
            await self._tenants.insert_one(dict(doc))
        else:
            await self._tenants.update_one({"id": body.id}, {"$set": doc})
            doc["created_at"] = existing.get("created_at", now)
        return Tenant(**doc)

    async def get_tenant(self, tenant_id: str) -> Tenant:
        doc = await self._tenants.find_one({"id": tenant_id})
        if doc is None:
            raise TenantNotFound(tenant_id)
        return Tenant(**doc)

    async def resolve_isolation_tier(self, tenant_id: str) -> str:
        """Return the tenant's tier, or raise TenantTierUnresolved.

        No default: an unconfigured tier is a refusal, never a silent ``shared``.
        """
        doc = await self._tenants.find_one({"id": tenant_id})
        if doc is None:
            raise TenantTierUnresolved(f"tenant {tenant_id!r} not registered")
        tier = doc.get("isolation_tier")
        if not tier:
            raise TenantTierUnresolved(f"tenant {tenant_id!r} has no isolation_tier")
        return tier

    # ----- principals ------------------------------------------------------

    async def register(
        self, body: PrincipalCreate, *, tenant_id: str | None = None
    ) -> Principal:
        tenant = tenant_id or body.tenant_id
        await self.get_tenant(tenant)  # must exist
        if body.type is PrincipalType.AGENT:
            owner = await self._principals.find_one({"id": body.owner_principal_id})
            if owner is None:
                raise ValueError(
                    f"owner_principal_id {body.owner_principal_id!r} does not exist"
                )
        now = _utcnow()
        doc: dict[str, Any] = {
            "id": body.id,
            "type": body.type.value,
            "tenant_id": tenant,
            "owner_principal_id": body.owner_principal_id,
            "status": PrincipalStatus.ACTIVE.value,
            "display_name": body.display_name,
            "created_at": now,
            "updated_at": now,
        }
        await self._principals.insert_one(dict(doc))
        return Principal(**doc)

    async def get(self, principal_id: str) -> Principal:
        doc = await self._principals.find_one({"id": principal_id})
        if doc is None:
            raise PrincipalNotFound(principal_id)
        return Principal(**doc)

    async def set_status(self, principal_id: str, new_status: PrincipalStatus) -> Principal:
        current = await self.get(principal_id)
        if current.status is PrincipalStatus.REVOKED:
            raise PrincipalStatusTransitionError(
                "revoked is terminal; it cannot be moved to another state"
            )
        now = _utcnow()
        await self._principals.update_one(
            {"id": principal_id},
            {"$set": {"status": new_status.value, "updated_at": now}},
        )
        # Suspending a owner cascades to its agents. Restoring does NOT reverse it.
        if new_status is PrincipalStatus.SUSPENDED:
            await self._principals.update_many(
                {
                    "owner_principal_id": principal_id,
                    "type": PrincipalType.AGENT.value,
                    "status": {"$ne": PrincipalStatus.REVOKED.value},
                },
                {"$set": {"status": PrincipalStatus.SUSPENDED.value, "updated_at": now}},
            )
        return await self.get(principal_id)

    def assert_type_unchanged(self, current: Principal, declared_type: PrincipalType) -> None:
        """Guard for any write path that re-declares type."""
        if current.type is not declared_type:
            raise PrincipalTypeImmutable(
                f"principal {current.id!r} type is {current.type.value}, cannot become "
                f"{declared_type.value}"
            )

    async def list_for_tenant(self, tenant_id: str, *, limit: int = 500) -> list[Principal]:
        cursor = self._principals.find({"tenant_id": tenant_id}).limit(limit)
        return [Principal(**d) async for d in cursor]


_singleton: PrincipalService | None = None


async def get_principal_service() -> PrincipalService:
    """Module singleton bound to the active DocumentDB client."""
    global _singleton
    if _singleton is None:
        from registry.repositories.documentdb.client import get_documentdb_client

        db = await get_documentdb_client()
        _singleton = PrincipalService(db)
    return _singleton


def reset_principal_service_singleton() -> None:
    global _singleton
    _singleton = None
