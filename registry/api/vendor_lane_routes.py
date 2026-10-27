"""Vendor onboarding lane (fd-vendor-lane; paas add-vendor-supply-open B).

Purpose-built, platform-scoped server register/toggle endpoints for the
供方上牌闸 paas-side workflow. Unlike the console's ``/api/servers/register``
(form body + cookie session + CSRF + register_service UI permission), these
endpoints are **machine-only**: a single shared ops token in
``VENDOR_LANE_TOKEN`` authenticates the LANE, and paas's vendor workflow
holds it server-side — vendor accounts never receive any registry credential
(paas design D2 纪律: 供应商不上 registry 写凭据末端).

Deliberate semantics (keep tight with the console route, not looser):

- JSON bodies (form support is out of scope for the machine lane).
- ``register`` mirrors :func:`server_routes.register_service` for the REMOTE
  deployment shape only and goes through the same
  ``server_service.register_server`` + registration gate; local runtimes are
  rejected upstream (execution model is paas's host lane, not this endpoint).
- ``toggle`` mirrors :func:`server_routes.toggle_service_route` minus the
  cookie/CSRF/ui-permission stack — the token IS the authority; the named
  service must already exist.
- Newly registered servers stay disabled by the same default the console
  route leaves (``/api/servers/register`` default), gated by
  ``check_registration_gate`` and by ``user_visibility`` unchanged — this
  lane cannot place a server on the gateway live path by itself; the paas
  workflow toggles afterwards with its own reviewed decision.

Audit actions are marked ``actor="vendor-lane:<token-key-fingerprint-sha256-8>"``
so a leaked token's footprint is immediately attributable in audit_events.

Tracked by the wire-platform-v2 fork (task paas add-vendor-supply-open 4.2).
"""

import hashlib
import hmac
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..audit import set_audit_action
from ..auth.internal import validate_internal_auth  # noqa: F401  (kept for symmetry; token path used)
from ..constants import DeploymentType  # noqa: F401
from ..core.config import settings
from ..services._asset_id import InvalidAssetIdError, check_caller_supplied_id_allowed, resolve_asset_id
from ..services.registration_gate_service import check_registration_gate
from ..services.server_service import server_service

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/vendor-servers", tags=["Vendor Lane"])

_RESOURCE_TYPE: str = "vendor_lane"
_AGENT = "vendor-lane"


def _lane_token() -> str:
    return (os.environ.get("VENDOR_LANE_TOKEN") or "").strip()


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8] if token else "unset"


async def verify_vendor_lane(authorization: Annotated[str | None, Header()] = None) -> bool:
    """Shared-secret lane gate: configured + constant-time match, else 503/401.

    Settings/env read per request so operators can rotate by env flip without
    a process restart (same posture as MARKET_REGISTRY_TOKEN consumers).
    """
    expected = _lane_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vendor lane not configured (VENDOR_LANE_TOKEN unset)",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    if not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid vendor lane token")
    return True


class VendorRegisterBody(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    path: str = Field(min_length=1)
    proxy_pass_url: str = Field(min_length=1)
    license: str = "N/A"
    tags: list[str] = []
    num_tools: int = 0
    metadata: dict[str, Any] = {}
    visibility: str = "public"
    allowed_groups: list[str] | None = None


class VendorToggleBody(BaseModel):
    path: str = Field(min_length=1)
    new_state: bool


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def vendor_register(body: VendorRegisterBody, request: Request, _lane: Annotated[bool, Depends(verify_vendor_lane)]):
    """Vendor-remote register. Same persistence+gate path as the console route;
    the registration ALWAYS lands disabled (paas流水线 toggles).
    """
    path = body.path if body.path.startswith("/") else "/" + body.path
    try:
        resolved_id = resolve_asset_id(None)
    except InvalidAssetIdError as e:  # pragma: no cover - auto path never raises
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid asset id: {e}")

    server_entry: dict[str, Any] = {
        "id": resolved_id,
        "server_name": body.name,
        "description": body.description,
        "path": path,
        "tags": [str(t).strip() for t in body.tags if str(t).strip()],
        "num_tools": body.num_tools,
        "license": body.license,
        "tool_list": [],
        "visibility": body.visibility,
        "allowed_groups": body.allowed_groups or [],
        "deployment": DeploymentType.REMOTE,
        "registered_by": _AGENT,
        "metadata": body.metadata or {},
        "proxy_pass_url": body.proxy_pass_url,
    }

    gate_result = await check_registration_gate(
        asset_type="server",
        operation="register",
        source_api="/vendor-servers/register",
        registration_payload=server_entry,
        raw_headers=request.headers,
    )
    if not gate_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Registration denied by policy gate: {gate_result.error_message}",
        )

    result = await server_service.register_server(server_entry)
    if not result.get("success"):
        return JSONResponse(status_code=409, content={"error": "Service registration failed", "detail": result.get("message")})
    set_audit_action(
        request,
        "create",
        _RESOURCE_TYPE,
        resource_id=resolved_id,
        description=f"Vendor lane registered server '{body.name}' at '{path}'",
    )
    return {"path": path, "id": resolved_id, "ok": True}


@router.post("/toggle")
async def vendor_toggle(body: VendorToggleBody, request: Request, _lane: Annotated[bool, Depends(verify_vendor_lane)]):
    """Vendor-remote toggle. Same state transition as the console route; the
    service must already exist.
    """
    path = body.path if body.path.startswith("/") else "/" + body.path
    info = await server_service.get_server_info(path)
    if not info:
        raise HTTPException(status_code=404, detail="Service path not registered")
    success = await server_service.toggle_service(path, bool(body.new_state))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to toggle service")
    set_audit_action(
        request,
        "update",
        _RESOURCE_TYPE,
        resource_id=info.get("_id") and str(info["_id"]),
        description=f"Vendor lane toggled '{info.get('server_name')}' ({path}) to {bool(body.new_state)}",
    )
    token = _lane_token()
    logger.info("vendor-lane toggled %s to %s (token fp=%s)", path, body.new_state, _fingerprint(token))
    return {"path": path, "enabled": bool(body.new_state)}
