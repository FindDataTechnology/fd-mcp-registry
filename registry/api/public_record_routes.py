"""Public, anonymous per-record endpoints for ARD catalog dereferencing.

The ARD catalog (``/.well-known/ai-catalog.json``) is crawled anonymously, so
each entry's ``url`` must be fetchable without auth. The existing per-record
endpoints (``/api/servers/{path}/server.json`` etc.) all require auth, so this
module adds parallel public endpoints under ``/api/public`` that:

1. Return ONLY public + enabled records.
2. Return 404 for anything not public+enabled (private/group/disabled/missing),
   so existence of non-public records is never disclosed.
3. Strip internal fields (backend URLs, auth schemes, group lists, owner) from
   the response body.

See issue #1294 (Blocker B1, SEC-1) and ``.scratchpad/issue-1294/lld.md``.

The aggregate summary endpoint (``/api/public/summary``) serves the product
landing page: total counts plus an operator-curated featured list. It reuses
the same public+enabled filter and never reveals non-public records — a
featured path that is not currently public+enabled is silently dropped.
"""

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Path

from ..repositories.factory import (
    get_agent_repository,
    get_server_repository,
    get_skill_repository,
)
from ..services.ard_mapping import _sanitize_name
from ..services.canonical_export import redact_backend_urls, to_canonical
from ..utils.credential_encryption import strip_credentials_from_dict

logger = logging.getLogger(__name__)

router = APIRouter()

# Landing-page curation asset (registry-product-home): a JSON file mapping
# entity kinds to featured record paths. Resolved at request time so operators
# can update it without an image rebuild; absent file or malformed content
# degrades to "no featured entries" rather than failing the endpoint.
_FEATURED_JSON_PATH = os.environ.get(
    "PUBLIC_FEATURED_JSON",
    "/app/public-featured/featured.json",
)

# Stored routing, DNS-pin, disable-state, and credential metadata is internal.
# ``is_proxied`` and ``proxy_client_url`` are intentionally not in this set: they
# are safe client-facing discovery fields and do not reveal the backend target.
_PUBLIC_PROXY_INTERNAL_FIELDS = frozenset(
    {
        "proxy_target_url",
        "proxy_streaming",
        "proxy_resolved_ips",
        "proxy_target_host",
        "proxy_disabled_reason",
        "custom_headers",
        "custom_headers_encrypted",
        "custom_header_names",
        "custom_header_overridable_names",
        "custom_headers_updated_at",
    }
)

# Fields that must never appear in an anonymous agent record body.
_AGENT_SENSITIVE_FIELDS = _PUBLIC_PROXY_INTERNAL_FIELDS | frozenset(
    {
        "security",
        "security_schemes",
        "allowed_groups",
        "registered_by",
        "auth_credential_encrypted",
        "sync_metadata",
        "ans_metadata",
        "_identity_url_normalized",
        "rating_details",
    }
)

# Fields that must never appear in an anonymous skill record body.
_SKILL_SENSITIVE_FIELDS = _PUBLIC_PROXY_INTERNAL_FIELDS | frozenset(
    {
        "auth_credential_encrypted",
        "auth_scheme",
        "auth_header_name",
        "credential_updated_at",
        "allowed_groups",
        "owner",
        "_identity_url_normalized",
        "rating_details",
    }
)


def _strip_fields(
    value: Any,
    sensitive: frozenset[str],
) -> Any:
    """Return a recursive copy with sensitive keys removed at every depth."""
    if isinstance(value, dict):
        return {
            key: _strip_fields(item, sensitive)
            for key, item in value.items()
            if key not in sensitive
        }
    if isinstance(value, list):
        return [_strip_fields(item, sensitive) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_fields(item, sensitive) for item in value)
    return value


@router.get("/public/servers/{leaf:path}/server.json")
async def get_public_server(
    leaf: str = Path(..., description="Sanitized server name from the catalog URN"),
) -> dict[str, Any]:
    """Public canonical server.json for a public + enabled MCP server.

    Matches by the same sanitized leaf used in the catalog entry's URN, so the
    URL resolves regardless of how the server path is stored. Only public +
    enabled servers are considered; anything else returns 404.
    """
    target = leaf.strip("/")
    repo = get_server_repository()
    records = await repo.find_with_filter({"is_enabled": True, "visibility": "public"}, limit=None)
    for path, record in records.items():
        if _sanitize_name(path) == target:
            safe_record = strip_credentials_from_dict({**record, "path": path})
            canonical, _ = to_canonical(safe_record)
            # Anonymous callers never receive backend URLs or per-user rating PII,
            # including copies nested inside preserved upstream metadata.
            redacted = redact_backend_urls(canonical)
            return _strip_fields(redacted, frozenset({"rating_details"}))
    raise HTTPException(status_code=404, detail="Server not found")


@router.get("/public/agents/{leaf:path}")
async def get_public_agent(
    leaf: str = Path(..., description="Sanitized agent name from the catalog URN"),
) -> dict[str, Any]:
    """Public agent card for a public + enabled A2A agent."""
    target = leaf.strip("/")
    repo = get_agent_repository()
    records = await repo.find_with_filter({"is_enabled": True, "visibility": "public"}, limit=None)
    for path, record in records.items():
        if _sanitize_name(path) == target:
            return _strip_fields(record, _AGENT_SENSITIVE_FIELDS)
    raise HTTPException(status_code=404, detail="Agent not found")


@router.get("/public/skills/{leaf:path}")
async def get_public_skill(
    leaf: str = Path(..., description="Sanitized skill name from the catalog URN"),
) -> dict[str, Any]:
    """Public skill card for a public + enabled local skill."""
    target = leaf.strip("/")
    repo = get_skill_repository()
    skills = await repo.list_filtered(
        include_disabled=False,
        visibility="public",
        registry_name="local",
    )
    for skill in skills:
        if _sanitize_name(skill.path) == target:
            body = skill.model_dump(mode="json")
            return _strip_fields(body, _SKILL_SENSITIVE_FIELDS)
    raise HTTPException(status_code=404, detail="Skill not found")


# Fields allowed on a featured card in the public summary. An allowlist (not a
# strip-list) guarantees no internal field can leak by default when the record
# shape gains new keys.
_FEATURED_CARD_FIELDS = ("name", "displayName", "description", "tags")

# Featured-card entry: {"kind": "servers"|"agents"|"skills", "path": "<stored path>"}.
def _load_featured_config() -> list[dict[str, str]]:
    """Read the curated featured list; malformed or absent asset degrades to [].."""
    try:
        with open(_FEATURED_JSON_PATH) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("public summary: featured asset unreadable (%s): %s", _FEATURED_JSON_PATH, exc)
        return []
    entries = doc.get("featured") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        path = entry.get("path")
        if kind in ("servers", "agents", "skills") and isinstance(path, str) and path:
            out.append({"kind": kind, "path": path})
    return out


async def _public_server_records() -> dict[str, dict[str, Any]]:
    repo = get_server_repository()
    return await repo.find_with_filter({"is_enabled": True, "visibility": "public"}, limit=None)


async def _public_agent_records() -> dict[str, dict[str, Any]]:
    repo = get_agent_repository()
    return await repo.find_with_filter({"is_enabled": True, "visibility": "public"}, limit=None)


async def _public_skill_records() -> list[Any]:
    repo = get_skill_repository()
    return await repo.list_filtered(
        include_disabled=False,
        visibility="public",
        registry_name="local",
    )


def _featured_card(kind: str, path: str, record: dict[str, Any]) -> dict[str, Any]:
    """Project a stored record onto the public card allowlist."""
    display = record.get("display_name") or record.get("name") or path
    card: dict[str, Any] = {"kind": kind, "path": path}
    card["name"] = path
    card["displayName"] = display
    card["description"] = record.get("description") or ""
    tags = record.get("tags")
    card["tags"] = [str(t) for t in tags][:8] if isinstance(tags, list) else []
    return {k: v for k, v in card.items() if k in ("kind", "path", *_FEATURED_CARD_FIELDS)}


@router.get("/public/summary")
async def get_public_summary() -> dict[str, Any]:
    """Aggregate counts + curated featured cards for the product landing page.

    Anonymous by design. Counts cover ONLY public+enabled records; featured
    entries are re-checked against the same predicate at request time and
    silently dropped when they no longer qualify, so the response never
    discloses the existence of private, group-gated, or disabled records.
    """
    servers = await _public_server_records()
    agents = await _public_agent_records()
    skills = await _public_skill_records()

    pools: dict[str, dict[str, dict[str, Any]]] = {
        "servers": servers,
        "agents": agents,
    }
    skill_pool = {s.path: (s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s)) for s in skills}

    featured: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in _load_featured_config():
        kind, path = entry["kind"], entry["path"]
        key = (kind, path)
        if key in seen:
            continue
        seen.add(key)
        if kind == "skills":
            record = skill_pool.get(path)
        else:
            record = pools.get(kind, {}).get(path)
        # Fail-closed: curated entries that are not currently public+enabled are
        # dropped without any existence signal. The DB filter already enforces
        # this predicate; the explicit re-check is defense-in-depth so a filter
        # drift upstream can never leak a private record through a featured card.
        if not isinstance(record, dict):
            continue
        if record.get("visibility") != "public" or record.get("is_enabled") is not True:
            continue
        featured.append(_featured_card(kind, path, record))

    return {
        "counts": {
            "servers": len(servers),
            "agents": len(agents),
            "skills": len(skill_pool),
        },
        "featured": featured,
    }
