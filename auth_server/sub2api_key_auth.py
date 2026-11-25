"""Authenticate a sub2api ``sk-`` 词元 to its holder (capability line-entry-unification).

A b2b customer holds a sub2api API key (``sk-...``). The registry gateway can
serve that customer directly if it can answer "which sub2api user owns this key,
and is their account usable". This module does exactly that, reusing the same
billing-plane probes ``preflight_quota`` already uses:

  * ``GET {SUB2API_BASE}/v1/models`` with the key as a Bearer runs the full
    billing gate at zero upstream spend: 2xx = usable, >=500 / connect failure =
    the plane is unavailable (caller may retry, NOT an invalid key), any other
    reachable refusal = the key/account is refused (invalid, over quota, group
    gated).
  * ``GET {SUB2API_BASE}/api/v1/admin/users?search=<key>`` returns the key's
    holder; exactly one hit resolves it, zero or several do not (cannot prove
    the holder -> fail closed).

Fail-closed semantics (mirroring paas's ``verifyLineCaller``): a degraded plane
raises :class:`Sub2ApiUnavailable` (caller maps to 503, never 401 — a 401 would
send a customer to re-paste a working key), an unusable/ambiguous key raises
:class:`Sub2ApiKeyRejected` (caller maps to 401/402).
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 5.0


class Sub2ApiUnavailable(RuntimeError):
    """The billing plane could not answer (unreachable / 5xx) — retryable."""


class Sub2ApiKeyRejected(RuntimeError):
    """The billing plane answered and refused this key/account — terminal."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # Billing/quota refusals map to 402; auth refusals to 401. The caller
        # chooses; None lets it default to 401.
        self.status = status


def _base() -> str:
    return (os.getenv("SUB2API_BASE") or "").strip().rstrip("/")


def _admin_key() -> str:
    return (os.getenv("SUB2API_ADMIN_KEY") or "").strip()


async def _probe_liveness(base: str, key: str) -> None:
    """Raise Sub2ApiKeyRejected / Sub2ApiUnavailable unless the key is usable."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}
            )
    except (httpx.HTTPError, OSError) as exc:
        raise Sub2ApiUnavailable(f"probe unreachable: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 500:
        raise Sub2ApiUnavailable(f"billing plane server error (HTTP {resp.status_code})")
    if resp.status_code < 300:
        return
    # Reachable refusal. A billing gate is 402 no matter which status the plane
    # picked to express it: sub2api answers an exhausted account with HTTP 403
    # and code INSUFFICIENT_BALANCE, and mapping that to 401 sends the customer
    # to re-paste a key that works. Everything else (401 invalid, 403 group,
    # 429 rate) is a refusal the caller maps to 401. Never a pass, never
    # "unavailable".
    code = None
    try:
        doc = resp.json()
        if isinstance(doc, dict):
            code = doc.get("code")
    except ValueError:
        pass
    if resp.status_code == 402 or str(code or "").upper() == "INSUFFICIENT_BALANCE":
        raise Sub2ApiKeyRejected(f"insufficient balance (HTTP {resp.status_code}, code={code})", status=402)
    raise Sub2ApiKeyRejected(
        f"billing gate refused (HTTP {resp.status_code}, code={code or 'unknown'})"
    )


async def _resolve_holder(base: str, key: str) -> dict:
    """Return the unique ``{userId, email}`` holder of ``key`` or raise."""
    admin = _admin_key()
    if not admin:
        # Without the admin key nobody can answer "whose key is this" -> the
        # plane is effectively unavailable for this purpose.
        raise Sub2ApiUnavailable("SUB2API_ADMIN_KEY not configured")
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{base}/api/v1/admin/users",
                params={"search": key, "page": 1, "page_size": 50},
                headers={"x-api-key": admin},
            )
    except (httpx.HTTPError, OSError) as exc:
        raise Sub2ApiUnavailable(f"holder lookup unreachable: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 500:
        raise Sub2ApiUnavailable(f"holder lookup server error (HTTP {resp.status_code})")
    if resp.status_code >= 300:
        raise Sub2ApiUnavailable(f"holder lookup refused (HTTP {resp.status_code})")
    try:
        items = ((resp.json() or {}).get("data") or {}).get("items") or []
    except ValueError as exc:
        raise Sub2ApiUnavailable("holder lookup returned non-JSON") from exc
    if len(items) != 1:
        # Zero (stale/foreign key) or several (ambiguous) -> cannot prove holder.
        raise Sub2ApiKeyRejected(f"key resolves to {len(items)} holders (need exactly 1)")
    holder = items[0]
    uid = holder.get("id")
    if uid is None:
        raise Sub2ApiKeyRejected("holder has no id")
    return {"userId": str(uid), "email": holder.get("email") or ""}


async def resolve_sk_caller(key: str) -> dict:
    """Authenticate a sub2api ``sk-`` key to its holder.

    Returns ``{"sub2api_user_id", "email"}`` on success.

    Raises:
        Sub2ApiUnavailable: billing plane unreachable / degraded (map to 503).
        Sub2ApiKeyRejected: key unusable or holder not uniquely resolvable
            (map to 401/402).

    Args:
        key: The bearer value (already stripped of the ``Bearer `` scheme).
    """
    base = _base()
    if not base:
        raise Sub2ApiUnavailable("SUB2API_BASE not configured")
    # Liveness first (the billing gate is the authoritative "usable"), then the
    # holder. Both fail closed.
    await _probe_liveness(base, key)
    holder = await _resolve_holder(base, key)
    return {"sub2api_user_id": holder["userId"], "email": holder["email"]}
