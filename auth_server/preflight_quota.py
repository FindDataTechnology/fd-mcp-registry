"""Pre-forward sub2api quota preflight probe ("patch-preflight").

wire-platform-v1 task 2.2. Inserts a billing-plane quota check on the MCP
forward path (the ``/mcp-proxy/{server}`` hop), AFTER the auth-server has
authenticated and scope-authorized the caller and BEFORE any outbound work
(egress token vend, upstream forward). The goal is zero-cost verification
that the caller's associated sub2api account is inside its balance/spend
window, so an exhausted account is refused before the gateway spends a
third-party token or an upstream tool call is executed (and thus before any
metered cost is incurred).

Three states (spec ``wire-facade-metering`` — 转发前余额预检 fail-closed):

- sufficient -> allow the forward;
- insufficient -> HTTP 402 with error code ``INSUFFICIENT_BALANCE`` and a
  Chinese explanation (no forward, no metering);
- billing plane unreachable -> mode-dependent:
  ``PREFLIGHT_MODE=strict`` (default) refuses with HTTP 503 /
  ``BILLING_UNAVAILABLE`` (fail closed); ``PREFLIGHT_MODE=postpaid`` allows
  the forward and logs/audits the degradation (design.md's 后置结算 fallback).

Caller mapping (v1 keeps this deliberately dumb): the registry has no
sub2api user directory, so ``SUB2API_CALLER_MAP`` is a JSON object mapping
the gateway-side caller identity to a sub2api query target::

    {"<username or key_id>": "<sub2api user id or sk- key>"}

The lookup key tried against the VERIFIED mcp-proxy token claims (``sub``,
then ``egress_user``) — for patch-key (task 2.1) callers ``sub`` is the
owning username, so one map row covers both the user's JWT and their
long-lived keys. Callers absent from the map SKIP the preflight entirely
(internal users are unaffected); only mapped (external/customer) callers are
probed.

Map VALUE disambiguation: ``key:<value>`` or a value starting with ``sk-``
is probed as a sub2api API key (``GET {SUB2API_BASE}/v1/models`` — sub2api
builds the model list locally but runs the full balance/group/quota gate, a
zero-cost liveness+billing probe; probed live 2026-10-02/03 by the wanxing
line); anything else (optionally ``user:<id>``) is probed as a sub2api user
id via the admin API (``GET {SUB2API_BASE}/api/v1/admin/users/{id}``,
``x-api-key``-authenticated, envelope ``{code:0,message,data}``).

Verdict doctrine: any USABLE answer from the billing plane is authoritative
— 2xx (or a positive admin read) allows; 402 / recognized
insufficient-balance codes / non-active account states refuse with 402.
Only the absence of a usable answer (network error, timeout, >=500 from the
plane itself, rejected admin credential, unparseable garbage) counts as
"billing unavailable" and enters the strict/postpaid mode switch.

Result caching: per-caller verdicts are cached in-process for
``PREFLIGHT_CACHE_TTL`` seconds (default 30) so a session's request burst
does not hammer sub2api; refusal verdicts (402/503) are cached for a fixed
5s negative window only, so a top-up or a billing-plane recovery takes
effect quickly. ``PREFLIGHT_CACHE_TTL=0`` disables caching.

Audit: rejection events (and postpaid degraded passes) are appended as one
JSON object per line to ``PREFLIGHT_AUDIT_PATH`` (default
``logs/audit/preflight_quota.jsonl``), fields
``ts/caller/server/result/reason/mode/target_kind``. Chosen over the
MongoDB ``AuditLogger`` stream deliberately: the preflight must keep
auditing exactly when MongoDB is down (the same incident class that trips
fail-closed would silently drop Mongo audit records), and a JSONL tail is
trivially shippable. The write is best-effort — an audit I/O failure NEVER
fails or un-blocks the request path — and every audit line is mirrored to
the module logger so `docker logs` always shows it. No key material is ever
written: only ``target_kind`` ("key"/"user"), never the target value.

All configuration is read from the environment on every call (the same
pattern as ``_read_mcp_filter_enabled``), so flags flip at container
restart without code changes and tests can monkeypatch ``os.environ``
freely. ``PREFLIGHT_ENABLED`` defaults to FALSE — the feature ships inert
and is gray-released by the deploy switches (task 2.3's A/B requirement):

- ``PREFLIGHT_ENABLED``       (default false)
- ``SUB2API_BASE``            (e.g. https://router.finddatatech.cloud)
- ``SUB2API_ADMIN_KEY``       admin API key (``x-api-key``), user-id probes
- ``SUB2API_CALLER_MAP``      JSON object, see above — **override layer**: entries
                               here win over the ``sub2api_caller_map`` Mongo
                               collection (add-customer-onboarding-automation
                               3.2; retires once every live caller is in Mongo)
- ``PREFLIGHT_MAP_TTL``       seconds, default 30; Mongo map re-read cadence
- ``PREFLIGHT_UNMAPPED``      ``pass`` (default) | ``warn`` | ``deny`` — what
                               happens to a caller found in NEITHER layer.
                               ``deny`` fails closed EXCEPT for admin-semantic
                               callers (``mcp-registry-admin`` scope/group:
                               platform credentials bill to the platform
                               account, not a customer balance), which keep
                               the skip-and-pass behavior.
- ``PREFLIGHT_MODE``          ``strict`` (default) | ``postpaid``
- ``PREFLIGHT_CACHE_TTL``     seconds, default 30; 0 disables caching
- ``PREFLIGHT_AUDIT_PATH``    JSONL sink, default logs/audit/preflight_quota.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

# Bind the client class at import time rather than calling
# ``httpx.AsyncClient(...)`` through the module: the auth-server's own tests
# (and this module's) patch the AsyncClient attribute on their respective
# namespaces, and a module-local binding keeps the probe's mock surface
# independent from the /mcp-proxy upstream-forward mock surface.
_HttpxAsyncClient = httpx.AsyncClient

logger = logging.getLogger("auth_server.preflight_quota")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Probe budget. sub2api is an in-cluster/nearby service; 5s matches the
#: wanxing line's probe timeout. Beyond this the plane counts as UNAVAILABLE.
PROBE_TIMEOUT_SECONDS = 5.0

#: Refusal verdicts are cached for this long only (spec: 402/503 不缓存或仅
#: 缓存 5s) so top-ups and recoveries take effect quickly while a hard-down
#: caller still gets bounded probe pressure.
NEGATIVE_CACHE_TTL_SECONDS = 5.0

#: Bound on the in-process verdict cache. Cleared wholesale on overflow:
#: simplest policy that cannot leak memory; the entries are cheap to re-probe.
_MAX_CACHE_ENTRIES = 4096

#: sub2api error-body ``code`` values that mean "account cannot pay" (matched
#: case-insensitively; the /v1/models gateway returns its own string codes).
_INSUFFICIENT_CODES = frozenset(
    {
        "insufficient_balance",
        "insufficient_quota",
        "insufficient_user_quota",
        "quota_exceeded",
        "balance_not_enough",
        "insufficient_balance_or_quota",
    }
)

#: readUser ``status`` values that mean the account must not be served.
_BLOCKED_USER_STATUSES = frozenset(
    {"disabled", "banned", "locked", "suspended", "inactive"}
)

#: Verdict constants.
_ALLOW = "allow"
_INSUFFICIENT = "insufficient"
_UNAVAILABLE = "unavailable"

_INSUFFICIENT_ERROR_CODE = "INSUFFICIENT_BALANCE"
_UNAVAILABLE_ERROR_CODE = "BILLING_UNAVAILABLE"

_INSUFFICIENT_MESSAGE_ZH = (
    "额度不足：该账户的余额或消费窗额度已耗尽，请充值后重试"
    "（如认为有误请联系平台管理员）"
)
_UNAVAILABLE_MESSAGE_ZH = "计费服务暂时不可用，请稍后重试（fail-closed 保护中）"


class PreflightConfigError(RuntimeError):
    """Raised (only) by the config-reader helpers' tests — never on the
    request path; the request path degrades per the doctrine above instead."""


# ---------------------------------------------------------------------------
# Configuration (env read per call; JSON map parse memoized by raw value)
# ---------------------------------------------------------------------------


def _env_truthy(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _preflight_enabled() -> bool:
    return _env_truthy(os.getenv("PREFLIGHT_ENABLED"), default=False)


def _sub2api_base() -> str:
    return (os.getenv("SUB2API_BASE") or "").strip().rstrip("/")


def _admin_key() -> str:
    return (os.getenv("SUB2API_ADMIN_KEY") or "").strip()


def _preflight_mode() -> str:
    mode = (os.getenv("PREFLIGHT_MODE") or "strict").strip().lower()
    return mode if mode in ("strict", "postpaid") else "strict"


def _cache_ttl() -> float:
    try:
        raw = os.getenv("PREFLIGHT_CACHE_TTL")
        if raw is None or raw.strip() == "":
            return 30.0
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _map_ttl() -> float:
    """Mongo caller-map re-read cadence (add-customer-onboarding-automation 3.2).

    Same parsing discipline as ``_cache_ttl``: an onboarding write becomes
    effective within this bounded window (default 30s), no restart, no reload.
    """
    try:
        raw = os.getenv("PREFLIGHT_MAP_TTL")
        if raw is None or raw.strip() == "":
            return 30.0
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _unmapped_policy() -> str:
    """pass | warn | deny for callers absent from BOTH map layers (3.3)."""
    policy = (os.getenv("PREFLIGHT_UNMAPPED") or "pass").strip().lower()
    return policy if policy in ("pass", "warn", "deny") else "pass"


def _is_admin_caller(claims: dict) -> bool:
    """Admin-semantic caller (platform credential): exempt from the customer
    balance gate — its metering attributes to the platform track (dual-
    credential attribution), so the unmapped-deny policy never bites it."""
    if not isinstance(claims, dict):
        return False
    marker = "mcp-registry-admin"
    scopes = claims.get("scopes")
    groups = claims.get("groups")
    if isinstance(scopes, list) and marker in scopes:
        return True
    if isinstance(groups, list) and marker in groups:
        return True
    return False


#: Hard ceiling on one Mongo map refresh. A datastore that cannot answer in
#: two seconds is DOWN for our purposes; the env override layer carries the
#: request and the error-cache window throttles retries.
_MAP_READ_TIMEOUT = 2.0


# Mongo caller-map snapshot cache (3.2): {expiry-monotonic, map}. Refreshed at
# _map_ttl cadence; a read error is cached as empty for a SHORT window so a
# flapping datastore does not hammer the collection, and the env override
# layer keeps the last-known rows usable in the meantime.
_map_cache: tuple[float, dict[str, str]] = (0.0, {})


async def _mongo_caller_map() -> dict[str, str]:
    """Read the ``sub2api_caller_map`` collection behind the TTL cache.

    Fail-open by design (mirrors the env map's philosophy): on datastore
    error/timeout the layer contributes nothing and the ERROR line surfaces
    it — ``PREFLIGHT_ENABLED`` remains the kill switch and mapped-in-env
    callers keep working. The read is bounded by ``_MAP_READ_TIMEOUT`` so a
    down datastore costs at most a couple of seconds on one request per
    short error-cache window, never the motor client's default 30s
    server-selection wait on every refresh.
    """
    global _map_cache
    now = time.monotonic()
    if now < _map_cache[0]:
        return _map_cache[1]
    mapping: dict[str, str] = {}
    try:
        import asyncio

        from registry.services.caller_map_service import get_caller_map_service

        service = await get_caller_map_service()
        mapping = await asyncio.wait_for(service.get_map(), timeout=_MAP_READ_TIMEOUT)
        _map_cache = (now + _map_ttl(), mapping)
        return mapping
    except Exception as exc:  # noqa: BLE001 - fail-open to env-only layer
        logger.error(
            "sub2api_caller_map read failed (%s); falling back to env-only "
            "caller map for up to %.0fs",
            type(exc).__name__,
            min(_map_ttl(), 5.0),
        )
        _map_cache = (now + min(_map_ttl(), 5.0), {})
        return {}


def _reset_caller_map_cache() -> None:
    """Test hook: drop the Mongo map snapshot so the next read re-fetches."""
    global _map_cache
    _map_cache = (0.0, {})


def _audit_path() -> str:
    return (os.getenv("PREFLIGHT_AUDIT_PATH") or "logs/audit/preflight_quota.jsonl").strip()


# Memoized parse of SUB2API_CALLER_MAP keyed on the raw env string, so a
# per-request JSON decode is not paid and env changes (tests, config reload)
# are still picked up on the very next call.
_caller_map_memo: tuple[str, dict[str, str]] = ("", {})


def _caller_map() -> dict[str, str]:
    global _caller_map_memo
    raw = (os.getenv("SUB2API_CALLER_MAP") or "").strip()
    if raw == _caller_map_memo[0]:
        return _caller_map_memo[1]
    if not raw:
        _caller_map_memo = (raw, {})
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A garbage map must be LOUD but must not take the gateway down:
        # treat as empty (every caller unmapped -> skip -> pass) and let the
        # ERROR line + missing audit traffic surface the misconfig. Fail-open
        # here is deliberate: PREFLIGHT_ENABLED is itself the kill switch.
        logger.error(
            "SUB2API_CALLER_MAP is set but not valid JSON (%s); treating as "
            "empty (preflight skipped for all callers) — fix the env value",
            exc,
        )
        _caller_map_memo = (raw, {})
        return {}
    if not isinstance(parsed, dict):
        logger.error(
            "SUB2API_CALLER_MAP must be a JSON object of "
            "{caller: sub2api-user-id-or-key}; treating as empty"
        )
        _caller_map_memo = (raw, {})
        return {}
    normalized = {
        str(k).strip(): str(v).strip()
        for k, v in parsed.items()
        if str(k).strip() and str(v).strip()
    }
    _caller_map_memo = (raw, normalized)
    return normalized


# ---------------------------------------------------------------------------
# Caller -> target resolution
# ---------------------------------------------------------------------------


async def _resolve_target(claims: dict) -> tuple[str, str] | None:
    """Resolve the verified proxy-token claims to a (caller, map-value) pair.

    Two-layer map (3.2): the ``sub2api_caller_map`` Mongo collection is the
    system of record (written by onboarding); ``SUB2API_CALLER_MAP`` env
    entries OVERRIDE it per-key during the migration. Returns ``None`` when
    the caller is in neither layer (-> the unmapped policy).
    """
    caller_map = {**await _mongo_caller_map(), **_caller_map()}
    if not caller_map:
        return None
    # Claim precedence: `sub` is the username for JWT AND patch-key callers
    # (task 2.1's validator returns the owning username as `sub`); the
    # canonical egress user is the fallback for IdP-sub-keyed deployments.
    for claim in ("sub", "egress_user"):
        value = claims.get(claim) if isinstance(claims, dict) else None
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if candidate in caller_map:
                return candidate, caller_map[candidate]
    return None


def _parse_target(value: str) -> tuple[str, str]:
    """Split a map value into (target_kind, target).

    ``key:<v>`` / ``sk-*`` -> ("key", v) — probed via GET /v1/models with the
    key as a Bearer token (zero-cost full billing gate). Anything else ->
    ("user", id) — probed via the admin readUser endpoint.
    """
    v = value.strip()
    lowered = v.lower()
    if lowered.startswith("key:"):
        return "key", v[4:].strip()
    if lowered.startswith("user:"):
        return "user", v[5:].strip()
    if lowered.startswith("sk-"):
        return "key", v
    return "user", v


# ---------------------------------------------------------------------------
# Verdict cache (in-process, monotonic expiry, bounded)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[str, str, float]] = {}
# (verdict, reason, expires_at-monotonic). Guarded by a lock that is held
# ONLY around dict reads/writes — never across probe I/O.
_cache_lock = None


def _cache_lock_get():
    # Lazily create the asyncio.Lock so importing this module never requires
    # a running event loop (tests import it at collection time).
    global _cache_lock
    if _cache_lock is None:
        import asyncio

        _cache_lock = asyncio.Lock()
    return _cache_lock


async def _cache_get(key: str) -> tuple[str, str] | None:
    async with _cache_lock_get():
        entry = _cache.get(key)
        if entry is None:
            return None
        verdict, reason, expires_at = entry
        if time.monotonic() >= expires_at:
            _cache.pop(key, None)
            return None
        return verdict, reason


async def _cache_put(key: str, verdict: str, reason: str, ttl: float) -> None:
    if ttl <= 0:
        return
    async with _cache_lock_get():
        if len(_cache) >= _MAX_CACHE_ENTRIES:
            # Wholesale clear on overflow: simplest policy that cannot leak;
            # entries are cheap to re-probe (one bounded GET per caller).
            _cache.clear()
        _cache[key] = (verdict, reason, time.monotonic() + ttl)


def reset_preflight_cache() -> None:
    """Test/ops helper: drop all cached verdicts and the Mongo map snapshot."""
    _cache.clear()
    _reset_caller_map_cache()


# ---------------------------------------------------------------------------
# Audit (JSONL, best-effort, mirrored to the logger)
# ---------------------------------------------------------------------------


def _write_audit_event(
    *,
    caller: str,
    server: str,
    result: str,
    reason: str,
    mode: str,
    target_kind: str,
) -> None:
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "mcp_preflight_quota",
        "caller": caller,
        "server": server,
        "result": result,
        "reason": reason,
        "mode": mode,
        "target_kind": target_kind,
    }
    line = json.dumps(event, ensure_ascii=False)
    # Mirror first: `docker logs` always shows the event even if the JSONL
    # append fails (read-only fs, full disk) — the audit must never be ONLY
    # in the file.
    logger.warning("preflight audit: %s", line)
    try:
        path = _audit_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # Best-effort by design: never fail (or un-block) the request over
        # the audit sink; the mirrored WARNING above keeps the loss loud.
        logger.error("preflight audit JSONL write failed (%s); event logged above", exc)


# ---------------------------------------------------------------------------
# sub2api probes (all I/O lives here; every caller-visible verdict above)
# ---------------------------------------------------------------------------


def _insufficient_from_body(doc: Any) -> str | None:
    """sub2api /v1/models error bodies carry the gateway's own string
    ``code``; recognize the "cannot pay" family (case-insensitive)."""
    if not isinstance(doc, dict):
        return None
    code = doc.get("code")
    if isinstance(code, str) and code.strip().lower() in _INSUFFICIENT_CODES:
        return code.strip()
    # Some builds nest {error: {code, message}} (OpenAI-style).
    err = doc.get("error")
    if isinstance(err, dict):
        inner = err.get("code")
        if isinstance(inner, str) and inner.strip().lower() in _INSUFFICIENT_CODES:
            return inner.strip()
    return None


async def _probe_key(base: str, target: str) -> tuple[str, str]:
    """GET {base}/v1/models with the mapped sub2api key as a Bearer token.

    The model list is built locally (no upstream spend) but the full billing
    gate (balance/group/quota) runs, making this a zero-cost account-state
    probe. 2xx allows; 402/insufficient-code refuses; every other usable
    HTTP answer is the plane's authoritative NO for this account (invalid
    key, group denied, rate-limited) and refuses with the code surfaced.
    """
    url = f"{base}/v1/models"
    try:
        async with _HttpxAsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {target}"})
    except (httpx.HTTPError, OSError) as exc:
        return _UNAVAILABLE, f"probe unreachable: {type(exc).__name__}: {exc}"

    if response.status_code >= 500:
        return _UNAVAILABLE, f"billing plane server error (HTTP {response.status_code})"
    if response.status_code < 300:
        return _ALLOW, "billing gate passed"

    try:
        doc = response.json()
    except ValueError:
        doc = None

    code = _insufficient_from_body(doc)
    if response.status_code == 402 or code:
        return _INSUFFICIENT, f"sub2api code={code or 'INSUFFICIENT_BALANCE'} (HTTP {response.status_code})"
    if 300 <= response.status_code < 400:
        # Unfollowed redirect from the billing base: a misconfigured
        # SUB2API_BASE, not an account verdict.
        return _UNAVAILABLE, f"unexpected redirect (HTTP {response.status_code}) from billing plane"
    # 401/403/429/...: the plane answered and refused THIS account/key.
    raw_code = doc.get("code") if isinstance(doc, dict) else None
    return _INSUFFICIENT, (
        f"billing gate refused (HTTP {response.status_code}, code={raw_code or 'unknown'})"
    )


async def _probe_user(base: str, target: str) -> tuple[str, str]:
    """Admin readUser probe: GET /api/v1/admin/users/{id} with x-api-key.

    Envelope {code:0, message, data}: code 0 + positive balance + non-blocked
    status allows; code 0 with balance<=0 / blocked status refuses; a
    business miss (unknown user) refuses; a rejected ADMIN credential or a
    plane-side 5xx is a plane/config problem, i.e. UNAVAILABLE (mode switch).
    """
    if not _admin_key():
        return _UNAVAILABLE, "SUB2API_ADMIN_KEY not configured for user-id probe"
    url = f"{base}/api/v1/admin/users/{target}"
    try:
        async with _HttpxAsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"x-api-key": _admin_key()})
    except (httpx.HTTPError, OSError) as exc:
        return _UNAVAILABLE, f"probe unreachable: {type(exc).__name__}: {exc}"

    if response.status_code >= 500:
        return _UNAVAILABLE, f"billing plane server error (HTTP {response.status_code})"
    # A rejected admin credential is OUR misconfiguration, not the caller's
    # account verdict — route it to the mode switch, not to 402.
    if response.status_code in (401, 403):
        return _UNAVAILABLE, f"admin credential rejected (HTTP {response.status_code})"
    try:
        doc = response.json()
    except ValueError:
        return _UNAVAILABLE, f"unparseable billing response (HTTP {response.status_code})"

    body_code = doc.get("code") if isinstance(doc, dict) else None
    data = doc.get("data") if isinstance(doc, dict) else None
    if body_code not in (0, None) or not isinstance(data, dict):
        # Business answer: e.g. user not found under the mapped id.
        return _INSUFFICIENT, f"readUser refused (code={body_code}, HTTP {response.status_code})"

    try:
        balance = float(data.get("balance", 0) or 0)
    except (TypeError, ValueError):
        balance = 0.0
    status = str(data.get("status") or "").strip().lower()
    if balance <= 0:
        return _INSUFFICIENT, f"balance={balance}"
    if status in _BLOCKED_USER_STATUSES:
        return _INSUFFICIENT, f"user status={status}"
    return _ALLOW, f"balance={balance}, status={status or 'unknown'}"


async def _probe(base: str, target_kind: str, target: str) -> tuple[str, str]:
    if target_kind == "key":
        return await _probe_key(base, target)
    return await _probe_user(base, target)


# ---------------------------------------------------------------------------
# The hook: enforce-or-allow
# ---------------------------------------------------------------------------


def _reject(status_code: int, error_code: str, message_zh: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": error_code, "message": message_zh},
        headers={"Retry-After": "2"} if status_code == 503 else None,
    )


async def enforce_mcp_proxy_preflight(
    claims: dict,
    server_name: str,
) -> None:
    """Pre-forward quota preflight for the /mcp-proxy hop.

    Returns silently when the forward may proceed; raises ``HTTPException``
    (402 ``INSUFFICIENT_BALANCE`` / 503 ``BILLING_UNAVAILABLE`` / 402
    ``CALLER_NOT_MAPPED`` under the deny policy) when it must not. Call this
    AFTER scope authorization so a genuine 403 can never be masked by a
    billing verdict, and BEFORE any egress/upstream work so a refused request
    costs nothing.

    Fast no-op paths (no I/O, no log spam): feature disabled; unmapped caller
    under the default ``pass`` policy (internal users are unaffected by design).
    """
    if not _preflight_enabled():
        return

    resolved = await _resolve_target(claims)
    if resolved is None:
        # Unmapped caller: neither the Mongo map nor the env override knows a
        # sub2api identity for them (add-customer-onboarding-automation 3.3).
        # Platform credentials (admin semantics) always pass — their metering
        # attributes to the platform track, not a customer balance.
        if _is_admin_caller(claims):
            logger.debug(
                "preflight: unmapped admin-semantic caller; skipping (server=%s)",
                server_name,
            )
            return
        policy = _unmapped_policy()
        if policy == "deny":
            _write_audit_event(
                caller=(claims.get("sub") or claims.get("egress_user") or "unknown"),
                server=server_name,
                result="rejected_caller_not_mapped",
                reason="no sub2api identity in either map layer",
                mode=_preflight_mode(),
                target_kind="none",
            )
            logger.warning(
                "preflight: refusing unmapped caller=%s server=%s "
                "(PREFLIGHT_UNMAPPED=deny — onboarding gap, not a balance issue)",
                claims.get("sub") or claims.get("egress_user"),
                server_name,
            )
            raise _reject(
                402,
                "CALLER_NOT_MAPPED",
                "该调用者未完成计费开户映射（配置缺口），请联系平台完成开户",
            )
        if policy == "warn":
            logger.warning(
                "preflight: unmapped caller=%s server=%s (PREFLIGHT_UNMAPPED=warn; "
                "allowing — fix the onboarding gap)",
                claims.get("sub") or claims.get("egress_user"),
                server_name,
            )
            return
        logger.debug(
            "preflight: caller not in caller map; skipping (server=%s)",
            server_name,
        )
        return
    caller, map_value = resolved
    target_kind, target = _parse_target(map_value)
    mode = _preflight_mode()
    cache_key = f"{caller}\x00{target_kind}\x00{target}"

    cached = await _cache_get(cache_key)
    if cached is not None:
        verdict, reason = cached
        if verdict == _ALLOW:
            return
        if verdict == _INSUFFICIENT:
            raise _reject(402, _INSUFFICIENT_ERROR_CODE, _INSUFFICIENT_MESSAGE_ZH)
        # Unavailable verdict: same mode switch as a fresh probe.
        if mode == "postpaid":
            logger.warning(
                "preflight: billing unavailable for caller=%s (cached; %s); "
                "PREFLIGHT_MODE=postpaid -> allowing forward",
                caller,
                reason,
            )
            return
        raise _reject(503, _UNAVAILABLE_ERROR_CODE, _UNAVAILABLE_MESSAGE_ZH)

    base = _sub2api_base()
    if not base:
        verdict, reason = _UNAVAILABLE, "SUB2API_BASE not configured"
    else:
        verdict, reason = await _probe(base, target_kind, target)

    if verdict == _ALLOW:
        await _cache_put(cache_key, verdict, reason, _cache_ttl())
        return

    if verdict == _INSUFFICIENT:
        await _cache_put(cache_key, verdict, reason, NEGATIVE_CACHE_TTL_SECONDS)
        _write_audit_event(
            caller=caller,
            server=server_name,
            result="rejected_insufficient_balance",
            reason=reason,
            mode=mode,
            target_kind=target_kind,
        )
        logger.warning(
            "preflight: refusing caller=%s server=%s (%s)",
            caller,
            server_name,
            reason,
        )
        raise _reject(402, _INSUFFICIENT_ERROR_CODE, _INSUFFICIENT_MESSAGE_ZH)

    # Billing plane unavailable.
    if mode == "postpaid":
        # Degraded allowance (design.md 后置结算 fallback): forward, but make
        # the decision visible in logs + audit so ops can reconcile later.
        await _cache_put(cache_key, verdict, reason, NEGATIVE_CACHE_TTL_SECONDS)
        _write_audit_event(
            caller=caller,
            server=server_name,
            result="degraded_postpaid_pass",
            reason=reason,
            mode=mode,
            target_kind=target_kind,
        )
        logger.warning(
            "preflight: billing unavailable for caller=%s server=%s (%s); "
            "PREFLIGHT_MODE=postpaid -> allowing forward",
            caller,
            server_name,
            reason,
        )
        return

    await _cache_put(cache_key, verdict, reason, NEGATIVE_CACHE_TTL_SECONDS)
    _write_audit_event(
        caller=caller,
        server=server_name,
        result="rejected_billing_unavailable",
        reason=reason,
        mode=mode,
        target_kind=target_kind,
    )
    logger.error(
        "preflight: billing unavailable for caller=%s server=%s (%s); "
        "PREFLIGHT_MODE=strict -> refusing (fail-closed)",
        caller,
        server_name,
        reason,
    )
    raise _reject(503, _UNAVAILABLE_ERROR_CODE, _UNAVAILABLE_MESSAGE_ZH)
