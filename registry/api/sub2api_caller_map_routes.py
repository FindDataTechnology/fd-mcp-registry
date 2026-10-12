"""Console API for the pre-flight caller map (add-customer-onboarding-automation 3.1).

Endpoints (all admin-gated — the caller map is onboarding infrastructure,
not a per-user surface):

- ``GET    /api/sub2api-caller-map``            list all mappings
- ``PUT    /api/sub2api-caller-map``            upsert one mapping
- ``DELETE /api/sub2api-caller-map/{username}`` remove one mapping

Authentication follows the console convention (``nginx_proxied_auth`` cookie
session / signed token behind nginx) plus the CSRF gate on mutating verbs,
exactly like ``/api/patch-keys``; the admin check mirrors
``management_routes._require_admin``. Written by the wire onboarding flow:
cell-manager calls PUT with its admin patch key right after provisioning a
customer, so the quota pre-flight covers the new caller from its first
entrance call (no .env edit, no restart).
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from registry.audit.context import set_audit_action
from registry.auth.csrf import verify_csrf_token_flexible
from registry.auth.dependencies import nginx_proxied_auth
from registry.services.caller_map_service import (
    get_caller_map_service,
    reset_caller_map_service_singleton,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sub2api-caller-map", tags=["Sub2API Caller Map"])

_RESOURCE_TYPE: str = "sub2api_caller_map"


def _require_admin(user_context: dict | None) -> dict:
    """Only admins manage the caller map (mirrors management_routes)."""
    if not user_context or not user_context.get("username"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not user_context.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )
    return user_context


async def _get_service():
    """Indirection so tests can patch the service lookup (mirrors iam routes)."""
    return await get_caller_map_service()


class CallerMapEntry(BaseModel):
    """PUT body: one username -> sub2api account mapping."""

    username: str = Field(..., min_length=1, max_length=256)
    value: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="sub2api user id, or 'key:<sk-...>' for key-shaped probes",
    )

    @field_validator("username", "value")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


@router.get("")
async def list_caller_map(
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict[str, Any]:
    """List the whole map (admin; read-only diagnostic / onboarding verify)."""
    _require_admin(user_context)
    service = await _get_service()
    mapping = await service.get_map()
    return {"total": len(mapping), "items": [{"username": k, "value": v} for k, v in sorted(mapping.items())]}


@router.put("", status_code=status.HTTP_200_OK)
async def upsert_caller_map(
    payload: CallerMapEntry,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> dict[str, Any]:
    """Upsert one mapping; effective for the pre-flight after its TTL window
    (PREFLIGHT_MAP_TTL, default 30s) — no restart, no reload."""
    _require_admin(user_context)
    service = await _get_service()
    try:
        await service.upsert(username=payload.username, value=payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    set_audit_action(
        request,
        "upsert",
        _RESOURCE_TYPE,
        resource_id=payload.username,
        description=f"Mapped entrance caller to sub2api account (value not logged verbatim)",
    )
    return {"ok": True, "username": payload.username}


@router.delete("/{username}", status_code=status.HTTP_200_OK)
async def delete_caller_map(
    username: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> dict[str, Any]:
    """Remove one mapping; 404 when absent (idempotent delete is PUT's job)."""
    _require_admin(user_context)
    service = await _get_service()
    removed = await service.delete(username)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    set_audit_action(
        request,
        "delete",
        _RESOURCE_TYPE,
        resource_id=username,
        description="Removed entrance caller mapping",
    )
    return {"ok": True, "username": username}


# Re-export for tests that need to reset the singleton between bindings.
__all__ = [
    "router",
    "CallerMapEntry",
    "reset_caller_map_service_singleton",
]
