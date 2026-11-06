"""Spine projection rows <-> legacy Mongo document shapes (tasks 2.2/2.3).

The spine store's column set is the registration truth (facet
``supply-store.js``). The registry's upper layers consume legacy Mongo
document dicts (``ServerRepositoryBase``) and ``SkillCard`` models
(``SkillRepositoryBase``) and must keep doing so unchanged — this module is
the single, tested translation point.

Conventions (documented for the migrator, task 3.1):

- The spine primary key IS the legacy ``_id``: mcp ``id`` = entry path with a
  leading slash (e.g. ``/law-bench``); skills ``name`` = the skill path.
  Read side re-derives ``path`` and the card fields from it.
- Spine ``display_name`` = legacy mcp ``server_name`` / skill card ``name``.
- Spine ``groups`` = legacy ``allowed_groups``.
- Spine ``provider_organization`` = legacy ``provider.organization``.
- Spine timestamps are epoch ms; legacy mcp documents carry naive-UTC ISO
  strings (``datetime.utcnow().isoformat()``) and skill cards timezone-aware
  datetimes. Converted both ways here.
- Legacy top-level fields the spine does not columnize (routing, proxy,
  health, scan state, asset ``id``...) are carried inside the spine
  ``metadata`` JSON under their own names. Read side promotes the fixed
  allowlists below back to the top level; write side demotes them back into
  ``metadata``. Keys outside the allowlists stay inside ``metadata`` (e.g.
  ``credential_ref`` / ``annotations`` / ``surface``) and are never promoted.
- Skill ``author``/``version`` live in legacy ``metadata`` (SkillMetadata) and
  in spine columns; mapped accordingly (``metadata.extra`` keeps the rest).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Epoch-ms <-> ISO helpers ----------------------------------------------------


def epoch_ms_to_naive_iso(value: Any) -> str | None:
    """Spine epoch ms -> naive-UTC ISO string (legacy mcp timestamp shape)."""
    ts = _to_epoch_ms(value)
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts / 1000, tz=UTC).replace(tzinfo=None)
    return dt.isoformat()


def epoch_ms_to_aware_iso(value: Any) -> str | None:
    """Spine epoch ms -> timezone-aware ISO string (skill card shape)."""
    ts = _to_epoch_ms(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat()


def iso_to_epoch_ms(value: Any) -> int | None:
    """Legacy timestamp (ISO string or epoch ms) -> epoch ms, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)  # already ms as a numeric string
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _to_epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        parsed = iso_to_epoch_ms(value)
        return parsed


def _as_bool(value: Any) -> bool:
    """Spine is_enabled (0/1 int, bool, or null) -> bool."""
    if isinstance(value, str):
        return value.lower() == "true" or value == "1"
    return bool(value)


# MCP: legacy fields carried inside spine metadata ----------------------------

MCP_METADATA_PROMOTIONS: frozenset[str] = frozenset(
    {
        # Asset identity / lineage (#1276)
        "id",
        "version",
        "registered_by",
        "source_created_at",
        "source_updated_at",
        "external_tags",
        "provider",
        "status",
        # Routing / transport
        "transport",
        "supported_transports",
        "headers",
        "mcp_endpoint",
        "sse_endpoint",
        "oauth_client_id",
        "append_mcp_path",
        "identity_forwarding",
        # Gateway proxy + auth (registry-operational state)
        "is_proxied",
        "proxy_target_url",
        "proxy_streaming",
        "proxy_resolved_ips",
        "proxy_target_host",
        "proxy_disabled_reason",
        "custom_header_names",
        "custom_header_overridable_names",
        "custom_headers",
        "custom_headers_encrypted",
        "auth_credential_encrypted",
        "auth_header_name",
        "auth_scheme",
        "auth_provider",
        "local_runtime",
        "is_active",
        # Health / scan / tool-management state
        "health_status",
        "last_health_check",
        "security_scan",
        "tool_overrides",
        "sync_metadata",
        "mcp_server_version",
        # Self-reported tool surface (add-self-reported-tool-surface D2/D4;
        # fix-registry-probe-and-selfreport §2): the report route reads the
        # token hash back with include_credentials=True and stamps
        # last_surface_report_at on every accepted report — without these
        # promotions the replica read drops all three, so every restart
        # self-report 403s (hash unreachable) and the freshness marker
        # silently never persists. surface_report_token_hash is already a
        # SHA-256 digest, and the recursive credential strip
        # (_token_free_projection) removes it from API responses wherever it
        # nests, including under metadata.
        "surface_source",
        "surface_report_token_hash",
        "last_surface_report_at",
    }
)

SKILL_METADATA_PROMOTIONS: frozenset[str] = frozenset(
    {
        "id",
        "skill_md_url",
        "skill_md_raw_url",
        "skill_md_content",
        "owner",
        "registry_name",
        "compatibility",
        "requirements",
        "target_agents",
        "allowed_tools",
        "resource_manifest",
        "auth_scheme",
        "auth_credential_encrypted",
        "auth_header_name",
        "credential_updated_at",
        "health_status",
        "last_checked_time",
        "num_stars",
        "rating_details",
        "content_version",
        "content_updated_at",
        "content_integrity",
        "status",
        "source_created_at",
        "source_updated_at",
        "external_tags",
        "is_proxied",
        "proxy_target_url",
        "proxy_streaming",
        "custom_header_names",
        "custom_header_overridable_names",
        "custom_headers_encrypted",
        "sync_metadata",
    }
)

# Skill card ``name`` pattern is lowercase-alphanumeric + hyphens; derived
# from the path's last segment (the facet bridge's pathToName convention).


def _derive_skill_name(path: str) -> str:
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    return leaf or path


# MCP: spine row -> legacy Mongo document -------------------------------------


def mcp_row_to_doc(row: dict[str, Any]) -> dict[str, Any]:
    """Assemble the legacy Mongo document dict from one spine mcp row.

    The returned dict carries ``_id`` (the path). Repository read methods pop
    it into ``path`` exactly like ``DocumentDBServerRepository`` does, so
    callers see the identical shape.
    """
    metadata = row.get("metadata")
    meta_dict: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    doc: dict[str, Any] = {}
    # Promote carried legacy fields first; mapped column values overlay below
    # so the columnar registration truth always wins.
    for key in MCP_METADATA_PROMOTIONS:
        if key in meta_dict:
            doc[key] = meta_dict[key]
    doc["_id"] = row.get("id")
    if "server_name" not in doc or doc.get("server_name") is None:
        doc["server_name"] = row.get("display_name")
    else:
        # A promoted legacy value wins only when the spine column is absent.
        doc["server_name"] = row.get("display_name") or doc.get("server_name")
    doc["description"] = row.get("description")
    doc["proxy_pass_url"] = row.get("proxy_pass_url")
    doc["num_tools"] = row.get("num_tools")
    doc["tool_list"] = row.get("tool_list")
    doc["tags"] = row.get("tags")
    doc["license"] = row.get("license")
    doc["deployment"] = row.get("deployment")
    doc["metadata"] = meta_dict
    doc["visibility"] = row.get("visibility")
    doc["allowed_groups"] = row.get("groups")
    doc["is_enabled"] = _as_bool(row.get("is_enabled"))
    registered_at = epoch_ms_to_naive_iso(row.get("created_at"))
    if registered_at is not None:
        doc["registered_at"] = registered_at
    updated_at = epoch_ms_to_naive_iso(row.get("updated_at"))
    if updated_at is not None:
        doc["updated_at"] = updated_at
    if row.get("updated_by") is not None:
        doc["updated_by"] = row.get("updated_by")
    return doc


def mcp_doc_to_upsert_entry(doc: dict[str, Any]) -> dict[str, Any]:
    """Map a legacy server document to a spine upsert entry (gate body).

    Non-columnar legacy fields are demoted into ``metadata`` (the inverse of
    :func:`mcp_row_to_doc`). Gate-stamped fields (``created_at`` /
    ``updated_at`` / ``updated_by``) are dropped — the write gate owns them.
    """
    server_id = doc.get("path") or doc.get("_id") or ""
    metadata_out: dict[str, Any] = dict(doc.get("metadata") or {})
    for key in MCP_METADATA_PROMOTIONS:
        if key in doc:
            metadata_out[key] = doc[key]
    provider = doc.get("provider")
    provider_organization = doc.get("provider_organization")
    if provider_organization is None and isinstance(provider, dict):
        provider_organization = provider.get("organization")
    entry: dict[str, Any] = {
        "id": server_id,
        "display_name": doc.get("server_name"),
        "description": doc.get("description"),
        "proxy_pass_url": doc.get("proxy_pass_url"),
        "num_tools": doc.get("num_tools"),
        "tool_list": doc.get("tool_list"),
        "tags": doc.get("tags"),
        "license": doc.get("license"),
        "provider_organization": provider_organization,
        "deployment": doc.get("deployment"),
        "metadata": metadata_out,
        "visibility": doc.get("visibility"),
        "groups": doc.get("allowed_groups"),
        "is_enabled": bool(doc.get("is_enabled", False)),
    }
    return {key: value for key, value in entry.items() if value is not None or key in _NULLABLE_MCP_ENTRY}


_NULLABLE_MCP_ENTRY = frozenset(
    {
        "id",
        "description",
        "num_tools",
        "tool_list",
        "tags",
        "license",
        "provider_organization",
        "deployment",
        "metadata",
        "visibility",
        "groups",
    }
)

# Doc field -> spine patchable column for partial updates. Anything absent
# from this map (and from MCP_METADATA_PROMOTIONS) cannot be forwarded and is
# reported, never silently dropped.
_MCP_PATCH_FIELD_MAP: dict[str, str] = {
    "server_name": "display_name",
    "description": "description",
    "proxy_pass_url": "proxy_pass_url",
    "num_tools": "num_tools",
    "tool_list": "tool_list",
    "tags": "tags",
    "license": "license",
    "deployment": "deployment",
    "visibility": "visibility",
    "allowed_groups": "groups",
    "is_enabled": "is_enabled",
    "provider_organization": "provider_organization",
}


def mcp_doc_fields_to_patch_fields(
    doc: dict[str, Any],
    updated_fields: list[str] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Map legacy document fields onto a spine patch body.

    Args:
        doc: The merged legacy document the service layer assembled.
        updated_fields: Field names the caller intends to write (``None`` =
            full-card write; every mappable field is forwarded).

    Returns:
        ``(patch_fields, unmapped)`` where ``patch_fields`` uses spine column
        names (metadata demotion already applied) and ``unmapped`` names the
        requested fields that have no spine representation (reported by the
        caller, never silently dropped).
    """
    wanted = set(updated_fields) if updated_fields is not None else None
    patch: dict[str, Any] = {}
    unmapped: list[str] = []
    metadata_out: dict[str, Any] = {}
    for field, value in doc.items():
        if field in ("path", "_id", "registered_at", "updated_at", "updated_by"):
            continue  # gate-stamped or identity; the spine owns them
        column = _MCP_PATCH_FIELD_MAP.get(field)
        if column is not None:
            if wanted is None or field in wanted:
                patch[column] = value
            continue
        if field == "provider":
            if isinstance(value, dict) and (wanted is None or field in wanted):
                patch["provider_organization"] = value.get("organization")
            elif wanted is not None and field in wanted:
                unmapped.append(field)
            continue
        if field == "metadata":
            continue  # merged below
        if field in MCP_METADATA_PROMOTIONS:
            if wanted is None or field in wanted:
                metadata_out[field] = value
            continue
        if wanted is None or field in wanted:
            unmapped.append(field)
    if metadata_out or (wanted is not None and "metadata" in wanted):
        existing = doc.get("metadata")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(metadata_out)
        patch["metadata"] = merged
    if wanted is not None:
        unmapped.extend(sorted(wanted - set(doc) - {"path"}))
    _report_unmapped("mcp patch", unmapped)
    return patch, unmapped


def _report_unmapped(context: str, unmapped: list[str]) -> None:
    if unmapped:
        logger.warning(
            "Spine facade write (%s): field(s) %s have no spine column; "
            "carried in metadata or dropped by contract — never silent",
            context,
            ", ".join(sorted(set(unmapped))),
        )


# Skill: spine row -> SkillCard dict ------------------------------------------


def skill_row_to_card_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Assemble the SkillCard constructor dict from one spine skill row.

    Legacy top-level card fields carried inside the spine ``metadata`` JSON
    (SKILL_METADATA_PROMOTIONS) are promoted to the top level, except the
    fields that belong to ``SkillMetadata`` (``author``/``version`` -> spine
    columns; everything else folds into ``metadata.extra`` per the registry
    serializer's SkillMetadata fold).
    """
    path = str(row.get("name") or "")
    metadata = row.get("metadata")
    meta_dict: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    card: dict[str, Any] = {}
    for key in SKILL_METADATA_PROMOTIONS:
        if key in meta_dict:
            card[key] = meta_dict[key]
    card["path"] = path
    card["name"] = row.get("display_name") or _derive_skill_name(path)
    card.setdefault("id", str(uuid.uuid5(uuid.NAMESPACE_URL, f"spine-skill:{path}")))
    card["description"] = row.get("description") or ""
    repository_url = row.get("repository_url") or card.get("repository_url")
    if repository_url is not None:
        card["repository_url"] = repository_url
    if "skill_md_url" not in card:
        # SkillCard requires skill_md_url; the migrator carries the legacy
        # value inside metadata. Without it the row is not registrable
        # content and the caller skips it with a loud error.
        fallback = repository_url
        if fallback:
            card["skill_md_url"] = fallback
    tags = row.get("tags")
    card["tags"] = tags if isinstance(tags, list) else []
    visibility = row.get("visibility")
    if visibility is not None:
        card["visibility"] = visibility
    groups = row.get("groups")
    if isinstance(groups, list):
        card["allowed_groups"] = groups
    if row.get("is_enabled") is not None:
        card["is_enabled"] = _as_bool(row.get("is_enabled"))
    card["metadata"] = {
        "author": row.get("author"),
        "version": row.get("version"),
        "extra": dict(meta_dict),
    }
    created_at = epoch_ms_to_aware_iso(row.get("created_at"))
    if created_at is not None:
        card["created_at"] = created_at
    updated_at = epoch_ms_to_aware_iso(row.get("updated_at"))
    if updated_at is not None:
        card["updated_at"] = updated_at
    return card


def skill_card_to_upsert_entry(card_dict: dict[str, Any]) -> dict[str, Any]:
    """Map a legacy skill card dict to a spine upsert entry (gate body)."""
    metadata = card_dict.get("metadata")
    meta_dict: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    extra = meta_dict.get("extra")
    extra_dict: dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
    metadata_out: dict[str, Any] = {**extra_dict}
    for key in SKILL_METADATA_PROMOTIONS:
        if key in card_dict:
            metadata_out[key] = card_dict[key]
    entry: dict[str, Any] = {
        "name": card_dict.get("path") or card_dict.get("name"),
        "display_name": card_dict.get("name"),
        "description": card_dict.get("description"),
        "repository_url": str(card_dict.get("repository_url") or "") or None,
        "author": meta_dict.get("author"),
        "version": meta_dict.get("version"),
        "tags": card_dict.get("tags"),
        "license": card_dict.get("license"),
        "metadata": metadata_out,
        "visibility": card_dict.get("visibility"),
        "groups": card_dict.get("allowed_groups"),
        "is_enabled": bool(card_dict.get("is_enabled", True)),
    }
    return {key: value for key, value in entry.items() if value is not None or key in _NULLABLE_SKILL_ENTRY}


_NULLABLE_SKILL_ENTRY = frozenset(
    {
        "name",
        "description",
        "repository_url",
        "author",
        "version",
        "tags",
        "license",
        "metadata",
        "visibility",
        "groups",
    }
)

_SKILL_PATCH_FIELD_MAP: dict[str, str] = {
    "name": "display_name",
    "description": "description",
    "repository_url": "repository_url",
    "tags": "tags",
    "license": "license",
    "visibility": "visibility",
    "allowed_groups": "groups",
    "is_enabled": "is_enabled",
    "path": None,  # identity — never forwarded
    "id": None,
    "created_at": None,
    "updated_at": None,
}


def skill_updates_to_patch_fields(
    updates: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Map a legacy skill update dict onto a spine patch body.

    Returns:
        ``(patch_fields, unmapped)`` with the same contract as
        :func:`mcp_doc_fields_to_patch_fields`.
    """
    patch: dict[str, Any] = {}
    unmapped: list[str] = []
    metadata_out: dict[str, Any] = {}
    for field, value in updates.items():
        if field in _SKILL_PATCH_FIELD_MAP:
            column = _SKILL_PATCH_FIELD_MAP[field]
            if column is not None:
                patch[column] = value
            continue
        if field == "metadata":
            # SkillMetadata dict: author/version -> columns, rest -> metadata.
            if isinstance(value, dict):
                if "author" in value:
                    patch["author"] = value.get("author")
                if "version" in value:
                    patch["version"] = value.get("version")
                extra = value.get("extra")
                metadata_out.update(dict(extra) if isinstance(extra, dict) else {})
                metadata_out.update(
                    {k: v for k, v in value.items() if k not in ("author", "version", "extra")}
                )
            continue
        if field in SKILL_METADATA_PROMOTIONS:
            metadata_out[field] = value
            continue
        unmapped.append(field)
    if metadata_out:
        patch["metadata"] = metadata_out
    _report_unmapped("skill patch", unmapped)
    return patch, unmapped


def skill_card_updates_to_patch_fields(
    updates: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Alias kept for call-site symmetry with the mcp mapper."""
    return skill_updates_to_patch_fields(updates)


def report_unmapped(context: str, unmapped: list[str]) -> None:
    """Public wrapper so repositories report unmapped fields uniformly."""
    _report_unmapped(context, unmapped)
