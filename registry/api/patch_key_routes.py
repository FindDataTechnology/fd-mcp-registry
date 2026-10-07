"""Console API for per-user long-lived API keys ("patch keys").

Endpoints (all scoped to the CALLER's own keys -- a user can only mint, list,
and revoke keys for the identity the console session authenticated):

- ``POST   /api/patch-keys``        mint a key; plaintext returned once
- ``GET    /api/patch-keys``        list the caller's keys (metadata only)
- ``DELETE /api/patch-keys/{id}``   revoke a key (irreversible, next call 401)

Authentication follows the console convention: ``nginx_proxied_auth`` (cookie
session in the browser, signed internal token behind nginx) plus the CSRF
gate on mutating verbs, exactly like ``/api/iam/user-groups``.

The minted key carries the caller's mint-time groups; the auth server's
/validate then resolves scope names per request, so the key behaves like the
user holding a JWT that never expires. See ``registry.services.patch_key_service``.

Tracked by the wire-platform-v1 fork (task 2.1).
"""

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from registry.audit.context import set_audit_action
from registry.auth.csrf import verify_csrf_token_flexible
from registry.auth.dependencies import nginx_proxied_auth
from registry.schemas.patch_key import (
    PatchKeyCreate,
    PatchKeyCreated,
    PatchKeyInfo,
    PatchKeyListResponse,
)
from registry.services.patch_key_service import (
    PatchKeyAlreadyRevoked,
    PatchKeyNotFound,
    PatchKeyQuotaExceeded,
    get_patch_key_service,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/patch-keys", tags=["Patch Keys"])

_RESOURCE_TYPE: str = "patch_key"


def _require_user(user_context: dict | None) -> dict:
    """Any authenticated console user may manage their OWN keys (no admin gate)."""
    if not user_context or not user_context.get("username"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user_context


async def _get_service():
    """Indirection so tests can patch the service lookup (mirrors iam routes)."""
    return await get_patch_key_service()


@router.post("", response_model=PatchKeyCreated, status_code=status.HTTP_201_CREATED)
async def mint_patch_key(
    payload: PatchKeyCreate,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> PatchKeyCreated:
    """Mint a non-expiring API key for the authenticated user.

    The response body contains the plaintext key exactly once; it is not
    stored server-side (only its SHA-256 hash) and cannot be retrieved again.
    """
    ctx = _require_user(user_context)
    service = await _get_service()
    try:
        created = await service.mint_key(
            username=ctx["username"],
            groups=list(ctx.get("groups") or []),
            name=payload.name,
            email=ctx.get("email") or None,
            provider=ctx.get("auth_method") or None,
            egress_user=ctx.get("egress_user") or None,
        )
    except PatchKeyQuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    set_audit_action(
        request,
        "create",
        _RESOURCE_TYPE,
        resource_id=created.info.key_id,
        description=f"Minted patch key '{payload.name}' (hash-stored, shown once)",
    )
    # SECURITY: never log `created.key` -- plaintext appears only in the response.
    return created


@router.get("", response_model=PatchKeyListResponse)
async def list_patch_keys(
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> PatchKeyListResponse:
    """List the caller's keys. Metadata only: no plaintext, no hash.

    When the call-grant gate is enabled (ecosystem-bridge 3.3), the response
    also carries the OWNER's monthly grant usage — one pool across all keys.
    Store hiccups degrade to ``grant: null`` (visibility, never a hard fail).
    """
    ctx = _require_user(user_context)
    service = await _get_service()
    items = await service.list_keys_for_user(ctx["username"])

    grant_view = None
    if (os.getenv("CALL_GRANT_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from registry.schemas.patch_key import PatchKeyGrantView
            from registry.services.call_grant_service import (
                current_month,
                get_call_grant_service,
            )

            limit = max(0, int((os.getenv("CALL_GRANT_MONTHLY_LIMIT") or "").strip() or 5000))
            grant_service = await get_call_grant_service()
            month = current_month()
            grant_view = PatchKeyGrantView(
                month=month,
                used=await grant_service.usage(ctx["username"], month=month),
                limit=limit,
            )
        except Exception:  # noqa: BLE001 - visibility is best-effort
            logger.warning("call-grant usage lookup failed for %s", ctx["username"], exc_info=True)
    return PatchKeyListResponse(total=len(items), items=items, grant=grant_view)


@router.delete("/{key_id}", response_model=PatchKeyInfo)
async def revoke_patch_key(
    key_id: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
    _csrf: Annotated[None, Depends(verify_csrf_token_flexible)] = None,
) -> PatchKeyInfo:
    """Revoke one of the caller's keys. Effective immediately (next call 401).

    Revocation is one-way: there is no re-activate. A key that belongs to a
    different user is reported as 404 (no cross-user existence oracle).
    """
    ctx = _require_user(user_context)
    service = await _get_service()
    try:
        info = await service.revoke_key(key_id=key_id, username=ctx["username"])
    except PatchKeyNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patch key not found")
    except PatchKeyAlreadyRevoked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Patch key already revoked"
        )
    set_audit_action(
        request,
        "delete",
        _RESOURCE_TYPE,
        resource_id=key_id,
        description=f"Revoked patch key '{info.name}' (irreversible)",
    )
    return info
