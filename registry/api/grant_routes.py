"""Grant issuance and JWKS routes.

Endpoints (``/api/grants`` and ``/api/jwks``):

- ``POST /api/grants``  mint a delegation grant. Console caller only; the grant
  is issued for the caller (``sub``) or, for an agent caller, on its own behalf.
  ``act`` is set when an agent principal issues for its owner.
- ``GET  /api/jwks``    public verification key set for the requested audiences.

Issuance is gated by ``GRANT_PLANE_ENABLED`` and requires the caller's tenant to
have an ``isolation_tier`` (else ``tenant_tier_unresolved``) and a registered,
``active`` principal (else ``tenant``/principal errors). The tenant and status
checks live here so the endpoint is the one place that turns principal-registry
state into a refused/issued grant.

Change: establish-agent-principal-plane (task group 2).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from registry.auth.grant import (
    GRANT_MAX_TTL_SECONDS,
    GrantConfigError,
    GrantSigner,
)
from registry.auth.dependencies import nginx_proxied_auth
from registry.core.config import settings
from registry.schemas.principal import PrincipalStatus
from registry.services.principal_service import (
    PrincipalNotFound,
    TenantTierUnresolved,
    get_principal_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Grants"])


class GrantIssueRequest(BaseModel):
    """Body for POST /api/grants.

    ``sub`` defaults to the authenticated caller. ``aud`` is a single target
    service (no multi-audience grants). ``run`` scopes the calls to one agent
    run/step. ``budget`` is an optional per-grant cap; absent means the tenant's
    remaining quota is the only bound.
    """

    aud: str = Field(..., min_length=1, description="single target service")
    run: str = Field(..., min_length=1, description="run/turn identifier")
    scope: list[str] = Field(default_factory=list)
    tenant: str | None = Field(default=None, description="defaults to caller's tenant")
    sub: str | None = Field(default=None, description="defaults to the caller principal")
    act: str | None = Field(default=None, description="who sub acts for (agent path)")
    budget: dict | None = None
    ttl_seconds: int = Field(default=1800, ge=1, le=GRANT_MAX_TTL_SECONDS)


class GrantIssueResponse(BaseModel):
    token: str
    aud: str
    exp: int


class _SignerCache:
    """Lazy, per-process signer (derivation is cheap but avoid re-reading env)."""

    _instance: GrantSigner | None = None

    def get(self) -> GrantSigner:
        if self._instance is None:
            self._instance = GrantSigner.from_settings()
        return self._instance

    def reset(self) -> None:
        self._instance = None


_SIGNER_CACHE = _SignerCache()


def reset_grant_signer() -> None:
    _SIGNER_CACHE.reset()


def _require_plane_enabled() -> None:
    if not settings.grant_plane_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="grant plane is not enabled",
        )


@router.post("/grants", response_model=GrantIssueResponse)
async def issue_grant(
    payload: GrantIssueRequest,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> GrantIssueResponse:
    _require_plane_enabled()
    if not user_context or not user_context.get("username"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    svc = await get_principal_service()
    sub = payload.sub or user_context["username"]
    tenant = payload.tenant or user_context.get("tenant_id")

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "tenant_unresolved", "message": "caller has no tenant binding"},
        )

    # A grant is only issued to a registered, active principal in a tenant whose
    # tier is resolved. This is the single point that turns registry state into
    # issue/refuse.
    try:
        principal = await svc.get(sub)
    except PrincipalNotFound:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "principal_unregistered", "message": f"{sub!r} is not registered"},
        )
    if principal.status is not PrincipalStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "principal_not_active",
                "message": f"principal status is {principal.status.value}",
            },
        )
    if principal.tenant_id != tenant:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "tenant_mismatch", "message": "principal not in stated tenant"},
        )

    try:
        await svc.resolve_isolation_tier(tenant)
    except TenantTierUnresolved as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "tenant_tier_unresolved", "message": str(exc)},
        )

    # act may only be set for an agent principal acting for its owner.
    act = payload.act
    if act is not None:
        if principal.type.value != "agent":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="act is only valid for agent principals",
            )
        if principal.owner_principal_id != act:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="act must be the agent's owner",
            )

    try:
        signer = _SIGNER_CACHE.get()
    except GrantConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"grant signing unavailable: {exc}",
        )

    try:
        token = signer.sign(
            sub=sub,
            aud=payload.aud,
            scope=payload.scope,
            tenant=tenant,
            run=payload.run,
            budget=payload.budget,
            ttl_seconds=payload.ttl_seconds,
            act=act,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    # exp is echoed for the caller's convenience.
    import jwt as _jwt

    claims = _jwt.decode(token, options={"verify_signature": False})
    logger.info(
        "Issued grant sub=%s aud=%s tenant=%s run=%s", sub, payload.aud, tenant, payload.run
    )
    return GrantIssueResponse(token=token, aud=payload.aud, exp=int(claims["exp"]))


@router.get("/jwks")
async def get_jwks(aud: Annotated[list[str] | None, Query()] = None) -> dict:
    """Public verification keys for the requested audiences (all if omitted)."""
    _require_plane_enabled()
    try:
        signer = _SIGNER_CACHE.get()
    except GrantConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"grant signing unavailable: {exc}",
        )
    auds = aud or []
    if not auds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="at least one ?aud= is required (no public key dump)",
        )
    return signer.jwks(auds)


__all__ = ["router", "reset_grant_signer"]
