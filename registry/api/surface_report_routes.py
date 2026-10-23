"""
Self-reported tool surface (add-self-reported-tool-surface).

Services whose HTTP layer requires a per-user Bearer (wire-build-mcp,
wire-ops-mcp, wanxing-ops-mcp, paper-trading) can never be probed for
`tools/list` -- anonymous requests are 401 by design. Their registry entries
therefore kept a stale `tool_list`, and the publish gate read legal tool names
as "unresolved".

The path deliberately avoids `/api/internal/`: nginx hard-404s that whole
prefix (`location ^~ /api/internal/ { return 404; }`) — it is reserved for the
lua subrequest channel and is unreachable from the public edge. This route is
reached by services over the public domain, so it carries its own Bearer gate.

This route lets such a service report its own surface at startup:

    POST /api/surface-report/servers/{path}/tool-list
    Authorization: Bearer <per-entry token>

Auth model (design D2): one token per entry, registry stores only its hash and
the path it is bound to. A token may only write its own path -- sharing one
platform-wide secret would let any holder rewrite another entry's tool_list
(shrink it and the publish gate starts rejecting legal names).

The write itself goes through `apply_tool_surface`, the same function the probe
uses, so the snapshot, the scope rows and the console broadcast cannot drift
apart (design D3).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from registry.health.service import apply_tool_surface, health_service
from registry.services.server_service import server_service

logger = logging.getLogger(__name__)

router = APIRouter()

#: Field on the server entry holding the token hash + bindings.
SURFACE_TOKEN_FIELD = "surface_report_token_hash"


class ToolSurfaceReport(BaseModel):
    """Payload a service posts on startup."""

    tools: list[dict[str, Any]] = Field(..., min_length=0)
    surface_version: str | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_surface_token(server_info: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Mint a token for one entry; returns (plaintext, updated server_info).

    The plaintext is returned exactly once -- callers must hand it to the
    service's deployment (env/secret) and never store it in the registry.
    """
    token = f"srf_{secrets.token_urlsafe(32)}"
    updated = dict(server_info)
    updated[SURFACE_TOKEN_FIELD] = _hash_token(token)
    updated["surface_source"] = "self_report"
    return token, updated


def revoke_surface_token(server_info: dict[str, Any]) -> dict[str, Any]:
    updated = dict(server_info)
    updated.pop(SURFACE_TOKEN_FIELD, None)
    updated["surface_source"] = "probe"
    return updated


def _token_matches(server_info: dict[str, Any], token: str) -> bool:
    expected = server_info.get(SURFACE_TOKEN_FIELD)
    if not expected:
        return False
    return secrets.compare_digest(expected, _hash_token(token))


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        return None
    value = raw[7:].strip()
    return value or None


@router.post("/surface-report/servers/{path:path}/tool-list")
async def report_tool_surface(path: str, payload: ToolSurfaceReport, request: Request):
    """Accept a service's self-reported tool surface.

    Idempotent: reporting the same surface twice is a no-op on the second call
    (``apply_tool_surface`` short-circuits on unchanged input).
    """
    entry_path = "/" + path.lstrip("/")

    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    # include_credentials=True: the token hash is a credential and is stripped
    # from ordinary responses (credential_encryption._SERVER_RESPONSE_SECRET_FIELDS).
    server_info = await server_service.get_server_info(entry_path, include_credentials=True)
    if not server_info:
        raise HTTPException(status_code=404, detail=f"Unknown server path {entry_path}")

    if not _token_matches(server_info, token):
        # Deliberately indistinguishable from a wrong path: a token holder must
        # not learn which entries exist beyond its own.
        raise HTTPException(status_code=403, detail="Token is not valid for this server path")

    tools = [t for t in payload.tools if isinstance(t, dict) and t.get("name")]
    if not tools:
        raise HTTPException(status_code=422, detail="tools must contain at least one named tool")

    surface_info = {"version": payload.surface_version} if payload.surface_version else None
    result = await apply_tool_surface(
        entry_path,
        tools,
        surface_info,
        broadcast=health_service.broadcast_health_update,
    )

    # Freshness marker for the console/ledger: self_report entries have no
    # health probe to date-stamp them (design D4). Field-scoped write — a
    # full-card write here would race the surface write and, because the read
    # is credential-stripped, could look like it clears fields.
    try:
        refreshed = await server_service.get_server_info(entry_path)
        if refreshed is not None:
            refreshed = dict(refreshed)
            refreshed["last_surface_report_at"] = _now_iso()
            await server_service.update_server(
                entry_path, refreshed, updated_fields=["last_surface_report_at"]
            )
    except Exception as e:  # noqa: BLE001 -- the surface write already landed
        logger.warning(
            f"surface report timestamp write failed for {entry_path} "
            f"type={type(e).__name__}"
        )

    logger.info(
        f"Self-reported tool surface for {entry_path}: {len(tools)} tool(s), "
        f"updated={result['updated']} ({result['reason']})"
    )
    return {
        "path": entry_path,
        "num_tools": len(tools),
        "updated": result["updated"],
        "reason": result["reason"],
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
