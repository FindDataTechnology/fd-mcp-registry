"""Unit tests for principal + tenant registration (task group 1).

The Motor collections are mocked so the lifecycle logic (agent owner required,
revoked terminal, owner-suspend cascade with no auto-restore, no-default tier)
is exercised without a live MongoDB. Routes are covered via the service, which
holds all the behavior.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.schemas.principal import (
    IsolationTier,
    PrincipalCreate,
    PrincipalStatus,
    PrincipalType,
    TenantUpsert,
)
from registry.services.principal_service import (
    PrincipalNotFound,
    PrincipalService,
    PrincipalStatusTransitionError,
    TenantTierUnresolved,
)


def _cursor(items):
    c = MagicMock()
    c.limit = MagicMock(return_value=c)
    c.__aiter__ = lambda self: _async_iter(items)
    return c


def _async_iter(items):
    async def gen():
        for i in items:
            yield i

    return gen()


def _make_service(principals_docs=None, tenants_docs=None):
    db = MagicMock()
    pcol = MagicMock()
    tcol = MagicMock()
    pcol.create_index = AsyncMock()
    tcol.create_index = AsyncMock()
    pcol.insert_one = AsyncMock()
    tcol.insert_one = AsyncMock()
    pcol.update_one = AsyncMock()
    pcol.update_many = AsyncMock()
    tcol.update_one = AsyncMock()
    pcol.find = MagicMock(return_value=_cursor(principals_docs or []))
    db.__getitem__ = MagicMock(side_effect=lambda name: pcol if name == "principals" else tcol)
    svc = PrincipalService(db)
    svc._p = pcol
    svc._t = tcol
    return svc, pcol, tcol


TENANT_DOC = {"id": "t1", "name": "T1", "isolation_tier": "shared"}


@pytest.mark.asyncio
async def test_agent_requires_owner():
    with pytest.raises(ValueError):
        PrincipalCreate(id="a1", type=PrincipalType.AGENT, tenant_id="t1")


@pytest.mark.asyncio
async def test_register_agent_rejects_missing_owner_principal():
    svc, pcol, tcol = _make_service()
    tcol.find_one = AsyncMock(return_value=TENANT_DOC)
    pcol.find_one = AsyncMock(return_value=None)  # owner does not exist
    with pytest.raises(ValueError):
        await svc.register(
            PrincipalCreate(
                id="a1",
                type=PrincipalType.AGENT,
                tenant_id="t1",
                owner_principal_id="ghost",
            )
        )


@pytest.mark.asyncio
async def test_revoked_is_terminal():
    svc, pcol, tcol = _make_service()
    pcol.find_one = AsyncMock(
        return_value={
            "id": "u1",
            "type": "user",
            "tenant_id": "t1",
            "status": "revoked",
            "owner_principal_id": None,
        }
    )
    with pytest.raises(PrincipalStatusTransitionError):
        await svc.set_status("u1", PrincipalStatus.ACTIVE)
    pcol.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_suspending_owner_cascades_to_agents():
    svc, pcol, tcol = _make_service()
    pcol.find_one = AsyncMock(
        side_effect=[
            {
                "id": "u1",
                "type": "user",
                "tenant_id": "t1",
                "status": "active",
                "owner_principal_id": None,
            },
            {
                "id": "u1",
                "type": "user",
                "tenant_id": "t1",
                "status": "suspended",
                "owner_principal_id": None,
            },
        ]
    )
    await svc.set_status("u1", PrincipalStatus.SUSPENDED)
    pcol.update_many.assert_awaited_once()
    call = pcol.update_many.await_args
    assert call.args[0]["owner_principal_id"] == "u1"
    assert call.args[0]["type"] == "agent"


@pytest.mark.asyncio
async def test_unsuspend_does_not_touch_agents():
    svc, pcol, tcol = _make_service()
    pcol.find_one = AsyncMock(
        side_effect=[
            {
                "id": "u1",
                "type": "user",
                "tenant_id": "t1",
                "status": "suspended",
                "owner_principal_id": None,
            },
            {
                "id": "u1",
                "type": "user",
                "tenant_id": "t1",
                "status": "active",
                "owner_principal_id": None,
            },
        ]
    )
    await svc.set_status("u1", PrincipalStatus.ACTIVE)
    pcol.update_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_tier_unresolved_when_missing():
    svc, pcol, tcol = _make_service()
    tcol.find_one = AsyncMock(return_value={"id": "t1", "name": "T1", "isolation_tier": None})
    with pytest.raises(TenantTierUnresolved):
        await svc.resolve_isolation_tier("t1")


@pytest.mark.asyncio
async def test_tier_resolved_when_set():
    svc, pcol, tcol = _make_service()
    tcol.find_one = AsyncMock(return_value=TENANT_DOC)
    assert await svc.resolve_isolation_tier("t1") == "shared"


@pytest.mark.asyncio
async def test_upsert_tenant_preserves_created_at():
    svc, pcol, tcol = _make_service()
    from datetime import UTC, datetime

    created = datetime(2026, 1, 1, tzinfo=UTC)
    tcol.find_one = AsyncMock(return_value={"id": "t1", "created_at": created})
    out = await svc.upsert_tenant(TenantUpsert(id="t1", isolation_tier=IsolationTier.SHARED))
    assert out.created_at == created


@pytest.mark.asyncio
async def test_get_missing_principal_raises():
    svc, pcol, tcol = _make_service()
    pcol.find_one = AsyncMock(return_value=None)
    with pytest.raises(PrincipalNotFound):
        await svc.get("nope")
