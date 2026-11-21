"""Console API for principal and tenant registration.

Endpoints:

- ``POST   /api/principals/tenants``           upsert a tenant (admin)
- ``GET    /api/principals/tenants/{id}``       read a tenant
- ``POST   /api/principals``                    register a principal (admin)
- ``GET    /api/principals``                    list principals in the caller's tenant
- ``POST   /api/principals/{id}/status``        transition status (admin)
- ``GET    /api/principals/tenants/{id}/tier``  resolve a tenant's isolation tier

Authorization: registration and status transitions are admin-only (they create
and mutate identities). Listing is tenant-scoped: a non-admin caller only sees
principals in their own tenant, so the read face never leaks cross-tenant
principals. Mirrors the patch-key console conventions (nginx_proxied_auth +
CSRF on mutating verbs + audit action).

Change: establish-agent-principal-plane (task group 1).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from registry.audit.context import set_audit_action
from registry.auth.csrf import verify_csrf_token_flexible
from registry.auth.dependencies import nginx_proxied_auth
from registry.schemas.principal import (
    Principal,
    PrincipalCreate,
    PrincipalStatusUpdate,
    Tenant,
    TenantUpsert,
)
from registry.services.principal_service import (
    PrincipalNotFound,
    PrincipalStatusTransitionError,
    TenantNotFound,
    TenantTierUnresolved,
    get_principal_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/principals", tags=["Principals"])

_RESOURCE_TYPE: str = "principal"


def _require_user(user_context: dict | None) -> dict:
    if not user_context or not user_context.get("username"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user_context


def _require_admin(user_context: dict | None) -> dict:
    ctx = _require_user(user_context)
    if not ctx.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="principal/tenant mutation is admin-only",
        )
    return ctx


async def _get_service():
    return await get_principal_service()


# ----- tenants (declared before /{id} routes to avoid path shadowing) -------


@router.post("/tenants", response_model=Tenant, status_code=status.HTTP_201_CREATED)
async def upsert_tenant(
    payload: TenantUpsert,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> Tenant:
    _require_admin(user_context)
    service = await _get_service()
    tenant = await service.upsert_tenant(payload)
    set_audit_action(
        request,
        "upsert",
        "tenant",
        resource_id=tenant.id,
        description=f"Upserted tenant '{tenant.id}' tier={tenant.isolation_tier}",
    )
    return tenant


@router.get("/tenants/{tenant_id}", response_model=Tenant)
async def get_tenant(
    tenant_id: str,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> Tenant:
    _require_user(user_context)
    service = await _get_service()
    try:
        return await service.get_tenant(tenant_id)
    except TenantNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")


@router.get("/tenants/{tenant_id}/tier")
async def get_tenant_tier(
    tenant_id: str,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Resolve a tenant's isolation tier. Missing tier is a refusal, not a default."""
    _require_user(user_context)
    service = await _get_service()
    try:
        tier = await service.resolve_isolation_tier(tenant_id)
    except TenantTierUnresolved as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "tenant_tier_unresolved", "message": str(exc)},
        )
    return {"tenant_id": tenant_id, "isolation_tier": tier}


# ----- principals -----------------------------------------------------------


@router.post("", response_model=Principal, status_code=status.HTTP_201_CREATED)
async def register_principal(
    payload: PrincipalCreate,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> Principal:
    _require_admin(user_context)
    service = await _get_service()
    try:
        principal = await service.register(payload)
    except TenantNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    set_audit_action(
        request,
        "create",
        _RESOURCE_TYPE,
        resource_id=principal.id,
        description=f"Registered {principal.type.value} principal '{principal.id}'",
    )
    return principal


@router.get("", response_model=list[Principal])
async def list_principals(
    tenant_id: str | None = None,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> list[Principal]:
    """List principals. Non-admins are pinned to their own tenant."""
    ctx = _require_user(user_context)
    service = await _get_service()
    if ctx.get("is_admin"):
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="admin listing requires tenant_id (no cross-tenant dump)",
            )
        return await service.list_for_tenant(tenant_id)
    own_tenant = ctx.get("tenant_id")
    if not own_tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller has no tenant binding",
        )
    return await service.list_for_tenant(own_tenant)


@router.post("/{principal_id}/status", response_model=Principal)
async def set_principal_status(
    principal_id: str,
    payload: PrincipalStatusUpdate,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> Principal:
    _require_admin(user_context)
    service = await _get_service()
    try:
        principal = await service.set_status(principal_id, payload.status)
    except PrincipalNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Principal not found")
    except PrincipalStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    set_audit_action(
        request,
        "update",
        _RESOURCE_TYPE,
        resource_id=principal_id,
        description=f"Set principal '{principal_id}' status={principal.status.value}",
    )
    return principal


__all__ = ["router"]
