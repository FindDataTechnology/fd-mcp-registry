"""Per-owner monthly call-grant enforcement on the MCP proxy path (ecosystem-bridge 3.3).

Free-tier admission gate for community patch-key callers, mirroring the
preflight quota probe's shape (env-driven, ships inert, JSONL-audited).
Placement: AFTER scope authorization and the sub2api preflight — a genuine
403 or an INSUFFICIENT_BALANCE must never be masked by a grant verdict.

Semantics:

- counts ``tools/call`` requests only — initialize / tools/list handshakes
  must not burn the grant (a single conversation makes dozens);
- one monthly pool per OWNER (``sub``): every key the caller mints shares it
  (keys are free, a per-key grant would multiply N-fold);
- applies only to patch-key callers whose groups include a grant group
  (default ``community``) — B2B wire customers (sub2api-mapped) and internal
  users are untouched;
- refusal: HTTP 402 ``GRANT_EXHAUSTED``, zero outbound work, counter NOT
  charged; month rolls over naturally by the ``YYYY-MM`` window key;
- counter store unreachable: HTTP 503 ``GRANT_UNAVAILABLE`` — fail closed,
  same rule as the billing preflight's strict mode.

Environment (restart-applied; tests monkeypatch freely):

- ``CALL_GRANT_ENABLED``       (default false — ships inert)
- ``CALL_GRANT_MONTHLY_LIMIT`` (default 5000)
- ``CALL_GRANT_GROUPS``        (default "community"; comma-separated)
- ``CALL_GRANT_AUDIT_PATH``    (default logs/audit/call_grant.jsonl)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

#: JSON-RPC method that consumes the grant (everything else rides free).
COUNTED_METHOD = "tools/call"

_PATCH_KEY_MARKERS = ("patch-key",)


def _env_truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _grant_enabled() -> bool:
    return _env_truthy(os.getenv("CALL_GRANT_ENABLED"), default=False)


def _monthly_limit() -> int:
    try:
        return max(0, int((os.getenv("CALL_GRANT_MONTHLY_LIMIT") or "").strip() or 5000))
    except ValueError:
        return 5000


def _grant_groups() -> frozenset[str]:
    raw = (os.getenv("CALL_GRANT_GROUPS") or "community").strip()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _tier_gates() -> dict[str, str]:
    """Paid-tier server map: ``server -> tier name`` (upgradeable refusals).

    ``CALL_GRANT_TIER_SERVERS`` as ``law-bench:paid,other:pro`` — servers
    listed here refuse grant-group callers with 402 ``TIER_REQUIRED`` naming
    the tier, BEFORE scope authorization (the caller has no scope for these
    servers by construction; this turns an opaque 403 into an upgrade path).
    """
    raw = (os.getenv("CALL_GRANT_TIER_SERVERS") or "").strip()
    gates: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        server, _, tier = chunk.partition(":")
        if server.strip() and tier.strip():
            gates[server.strip()] = tier.strip()
    return gates


def is_tier_gated(claims: dict[str, Any], registered_server: str) -> str | None:
    """Tier name when this caller faces an upgradeable refusal; None otherwise.

    Applies to patch-key callers whose groups include a grant group and whose
    target server is tier-gated. Non-grant callers (wire customers, internal)
    return None — their lane is untouched.
    """
    if not _grant_enabled():
        return None
    if not _is_patch_key_caller(claims):
        return None
    groups = {str(g) for g in (claims.get("groups") or [])}
    if not groups & _grant_groups():
        return None
    return _tier_gates().get(registered_server)


def _audit_path() -> str:
    return (os.getenv("CALL_GRANT_AUDIT_PATH") or "logs/audit/call_grant.jsonl").strip()


def _audit(event: dict[str, Any]) -> None:
    """Append one JSONL line; audit failures never break the gate path."""
    try:
        os.makedirs(os.path.dirname(_audit_path()) or ".", exist_ok=True)
        with open(_audit_path(), "a", encoding="utf8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _body_counts(body: bytes | None) -> bool:
    """Does this JSON-RPC body contain at least one ``tools/call``?

    Parsing mirrors ``_authorize_forwarded_mcp_body``: a body that cannot be
    parsed does not count (the scope authorization already rejected it).
    """
    if not body:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    entries = payload if isinstance(payload, list) else [payload]
    return any(isinstance(e, dict) and e.get("method") == COUNTED_METHOD for e in entries)


def _is_patch_key_caller(claims: dict[str, Any]) -> bool:
    auth_method = str(claims.get("auth_method") or claims.get("method") or "").lower()
    client_id = str(claims.get("client_id") or "").lower()
    return any(marker in (auth_method, client_id) for marker in _PATCH_KEY_MARKERS)


async def enforce_call_grant(claims: dict[str, Any], request_body: bytes | None) -> None:
    """Admission gate; raises HTTPException on refusal, returns silently on allow."""
    if not _grant_enabled() or _monthly_limit() <= 0:
        return
    if not _is_patch_key_caller(claims):
        return
    groups = {str(g) for g in (claims.get("groups") or [])}
    if not groups & _grant_groups():
        return  # wire-customer / internal lanes: not the free-tier population
    if not _body_counts(request_body):
        return

    owner = str(claims.get("sub") or "")
    if not owner:
        return  # no identity to charge: nothing to enforce (validated upstream)

    from registry.services.call_grant_service import get_call_grant_service

    try:
        service = await get_call_grant_service()
        verdict = await service.check_and_consume(owner, _monthly_limit())
    except Exception as exc:  # noqa: BLE001 - store down: fail closed, audited
        _audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "grant_store_unavailable",
            "owner": owner,
            "error": str(exc),
        })
        raise HTTPException(
            status_code=503,
            detail={
                "error": "GRANT_UNAVAILABLE",
                "message": "额度服务暂不可用，已拒绝（fail-closed）；稍后重试。",
            },
        ) from exc

    if not verdict.allowed:
        _audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "grant_exhausted",
            "owner": owner,
            "month": verdict.month,
            "used": verdict.used,
            "limit": verdict.limit,
        })
        raise HTTPException(
            status_code=402,
            detail={
                "error": "GRANT_EXHAUSTED",
                "message": (
                    f"本月免费额度已用尽（{verdict.limit} 次/月，已用 {verdict.used}）；"
                    f"{verdict.month} 窗口，次月自动重置。"
                ),
                "month": verdict.month,
                "limit": verdict.limit,
            },
        )
