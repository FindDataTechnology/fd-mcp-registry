"""Spine-backed server repository — the registry execution-plane adapter.

Reads serve from the local pull-only replica (rows assembled into legacy
Mongo document shapes by :mod:`.mapping`, so upper layers are untouched).
Writes forward to the spine write gate with the acting identity from
:mod:`.actor_context` and raise :class:`SpineGateError` with the gate's
status/code/message verbatim (compat-facade semantics, design D3).

Structural errors (methods that have no spine semantics because the legacy
behaviour is Mongo-operational only) raise :class:`SpineStructuralError`
carrying the ``SUPPLY_LEGACY_DIRECT_WRITE`` code — the legacy direct write
path is retired, never half-open.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...utils.metadata import project_metadata
from ..interfaces import ServerRepositoryBase
from .actor_context import current_actor
from .adapter import SpineAdapter
from .mapping import (
    mcp_doc_fields_to_patch_fields,
    mcp_doc_to_upsert_entry,
    mcp_row_to_doc,
    report_unmapped,
)
from .write_gate import SpineGateError

logger = logging.getLogger(__name__)

#: Error code carried by structurally-refused legacy operations.
SUPPLY_LEGACY_DIRECT_WRITE = "SUPPLY_LEGACY_DIRECT_WRITE"


class SpineStructuralError(Exception):
    """A legacy operation that has no spine-mode semantics, refused structurally.

    Attributes:
        code: Machine-readable code (always ``SUPPLY_LEGACY_DIRECT_WRITE``).
        message: Operator-facing explanation.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.code = SUPPLY_LEGACY_DIRECT_WRITE
        self.message = message


class SpineServerRepository(ServerRepositoryBase):
    """ServerRepositoryBase over the spine read-only replica + write gate.

    Read methods never touch the network: they assemble documents from the
    replica (last-good on pull failure, honestly marked stale). Write methods
    forward to the gate and trigger a best-effort replica resync so
    read-after-write stays true within the same request.
    """

    def __init__(self, adapter: SpineAdapter):
        self._adapter = adapter

    # ── reads ────────────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
    ) -> dict[str, Any] | None:
        """Get server by path, matching either trailing-slash form of ``_id``."""
        for candidate in self._path_variants(path):
            row = self._adapter.replica.get_mcp_row(candidate)
            if row is not None:
                doc = mcp_row_to_doc(row)
                doc["path"] = doc.pop("_id")
                return doc
        return None

    async def list_all(
        self,
        exclude_tool_list: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """List all servers keyed by path (``tool_list`` projectable)."""
        servers: dict[str, dict[str, Any]] = {}
        for row in self._adapter.replica.list_mcp_rows():
            doc = mcp_row_to_doc(row)
            path = doc.pop("_id")
            if exclude_tool_list:
                doc.pop("tool_list", None)
            doc["path"] = path
            servers[path] = doc
        return servers

    async def list_paginated(
        self,
        skip: int = 0,
        limit: int = 100,
        exclude_tool_list: bool = False,
        metadata_paths: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Sorted (by ``_id``) skip/limit page over the replica.

        ``metadata_paths`` prunes the ``metadata`` subdocument in Python (the
        DocumentDB aggregation pipeline is Mongo-specific; the prune helper is
        the same one its fallback path uses).
        """
        all_servers = await self.list_all(exclude_tool_list=exclude_tool_list)
        ordered = {key: all_servers[key] for key in sorted(all_servers)}
        page = {
            path: doc
            for index, (path, doc) in enumerate(ordered.items())
            if skip <= index < skip + limit
        }
        if metadata_paths:
            for doc in page.values():
                doc["metadata"] = project_metadata(doc.get("metadata"), metadata_paths)
        return page

    async def list_by_ids(
        self,
        paths: list[str],
    ) -> dict[str, dict[str, Any]]:
        """List servers whose path is in the given set (exact match)."""
        found: dict[str, dict[str, Any]] = {}
        for path in paths:
            doc = await self.get(path)
            if doc is not None:
                found[doc["path"]] = doc
        return found

    async def list_by_source(
        self,
        source: str,
    ) -> dict[str, dict[str, Any]]:
        """List servers whose ``source`` field (promoted legacy field) matches."""
        servers: dict[str, dict[str, Any]] = {}
        for row in self._adapter.replica.list_mcp_rows():
            doc = mcp_row_to_doc(row)
            if doc.get("source") == source:
                path = doc.pop("_id")
                doc["path"] = path
                servers[path] = doc
        return servers

    async def list_proxied(self) -> list[dict[str, Any]]:
        """Projected list of proxied servers for the nginx render hot path.

        Mirrors the DocumentDB projection: only the fields the render +
        ``resolve_proxy_target`` need, from assembled documents.
        """
        projection_fields = (
            "is_proxied",
            "is_enabled",
            "proxy_target_url",
            "proxy_streaming",
            "custom_header_names",
            "custom_header_overridable_names",
            "proxy_resolved_ips",
            "proxy_target_host",
            "proxy_disabled_reason",
            "proxy_pass_url",
            "deployment",
            "sync_metadata",
        )
        rows: list[dict[str, Any]] = []
        for row in self._adapter.replica.list_mcp_rows():
            doc = mcp_row_to_doc(row)
            if not doc.get("is_proxied"):
                continue
            projected = {field: doc.get(field) for field in projection_fields}
            projected["path"] = doc.get("_id")
            rows.append(projected)
        return rows

    # ── writes (forwarded to the spine gate) ─────────────────────────────

    async def create(
        self,
        server_info: dict[str, Any],
    ) -> bool:
        """Register a server: one upsert through the spine write gate.

        The gate validates the entry (unknown fields are 400
        ``SUPPLY_UNKNOWN_FIELD``) and lands the audit row with the acting
        identity; the error is re-raised verbatim.
        """
        entry = mcp_doc_to_upsert_entry(server_info)
        await self._adapter.gate.mcp_upsert(actor=current_actor(), entry=entry)
        logger.info("Spine WRITE: upserted server '%s' via gate", entry.get("id"))
        await self._adapter.resync_after_write()
        return True

    async def update(
        self,
        path: str,
        server_info: dict[str, Any],
        *,
        updated_fields: list[str] | None = None,
        expected_updated_at: str | None = None,
    ) -> bool:
        """Update a server: forwarded as a spine patch.

        ``expected_updated_at`` compare-and-set cannot be honoured across the
        gate (the gate stamps ``updated_at`` itself); it is accepted and
        ignored so the ABC contract stays intact — the gate's audit trail
        carries the acting identity for the lost-update window.
        """
        if expected_updated_at is not None:
            logger.info(
                "Spine WRITE: CAS expectation on '%s' not enforceable across the gate; "
                "forwarding unconditionally",
                path,
            )
        patch_fields, unmapped = mcp_doc_fields_to_patch_fields(server_info, updated_fields)
        report_unmapped(f"server '{path}' update", unmapped)
        if not patch_fields:
            logger.info("Spine WRITE: nothing forwardable for '%s' update", path)
            return False
        server_id = self._existing_id(path)
        if server_id is None:
            logger.error("Server at '%s' not found in spine replica", path)
            return False
        await self._adapter.gate.mcp_patch(
            actor=current_actor(),
            server_id=server_id,
            fields=patch_fields,
        )
        logger.info("Spine WRITE: patched server '%s' via gate", server_id)
        await self._adapter.resync_after_write()
        return True

    async def delete(
        self,
        path: str,
    ) -> bool:
        """Delete a server through the gate; False when the spine has no row."""
        server_id = self._existing_id(path)
        if server_id is None:
            logger.error("Server at '%s' not found in spine replica", path)
            return False
        try:
            await self._adapter.gate.mcp_delete(actor=current_actor(), server_id=server_id)
        except SpineGateError as exc:
            if exc.status == 404:
                logger.error("Spine gate reports no row for '%s'", path)
                return False
            raise
        logger.info("Spine WRITE: deleted server '%s' via gate", server_id)
        await self._adapter.resync_after_write()
        return True

    async def delete_with_versions(
        self,
        path: str,
    ) -> int:
        """Delete the active row. The spine keeps no version documents (0/1)."""
        deleted = await self.delete(path)
        return 1 if deleted else 0

    async def set_state(
        self,
        path: str,
        enabled: bool,
    ) -> bool:
        """Toggle a server's ``is_enabled`` (spine patch column)."""
        server_id = self._existing_id(path)
        if server_id is None:
            logger.error("Server at '%s' not found", path)
            return False
        await self._adapter.gate.mcp_patch(
            actor=current_actor(),
            server_id=server_id,
            fields={"is_enabled": bool(enabled)},
        )
        logger.info("Spine WRITE: set server '%s' is_enabled=%s via gate", server_id, enabled)
        await self._adapter.resync_after_write()
        return True

    async def update_field(
        self,
        path: str,
        field: str,
        value: Any,
    ) -> bool:
        """Update one field: mapped through the same patch translation."""
        merged = await self.get(path)
        if merged is None:
            logger.error("Server at '%s' not found in spine replica", path)
            return False
        if value is None:
            merged.pop(field, None)
        else:
            merged[field] = value
        return await self.update(path, merged, updated_fields=[field])

    async def set_tool_override(
        self,
        path: str,
        tool_name: str,
        override: dict[str, Any],
    ) -> bool:
        """Set one tool's override entry (carried in ``metadata.tool_overrides``)."""
        merged = await self.get(path)
        if merged is None:
            logger.error("Server at '%s' not found in spine replica", path)
            return False
        overrides = dict(merged.get("tool_overrides") or {})
        overrides[tool_name] = override
        merged["tool_overrides"] = overrides
        return await self.update(path, merged, updated_fields=["tool_overrides"])

    async def replace_tool_overrides(
        self,
        path: str,
        overrides: dict[str, Any],
    ) -> bool:
        """Replace the whole tool_overrides map in one forwarded write."""
        merged = await self.get(path)
        if merged is None:
            logger.error("Server at '%s' not found in spine replica", path)
            return False
        merged["tool_overrides"] = dict(overrides)
        return await self.update(path, merged, updated_fields=["tool_overrides"])

    # ── reads derived from documents ─────────────────────────────────────

    async def get_state(
        self,
        path: str,
    ) -> bool:
        """Get server enabled/disabled state."""
        doc = await self.get(path)
        if doc:
            return bool(doc.get("is_enabled", False))
        return False

    async def get_all_states(self) -> dict[str, bool]:
        """Enabled/disabled state for every replica row."""
        return {
            path: bool(doc.get("is_enabled", False))
            for path, doc in (await self.list_all()).items()
        }

    async def load_all(self) -> None:
        """No-op warm-up: the replica is already loaded (pull-only store)."""
        status = self._adapter.replica.status()
        logger.info(
            "Spine replica load_all: %s mcp rows available (stale=%s)",
            status.get("mcp_count"),
            status.get("stale"),
        )

    async def count(
        self,
        exclude_versions: bool = False,
    ) -> int:
        """Count of servers in the replica.

        The spine keeps one row per entry (no ``{path}:{version}`` version
        documents), so ``exclude_versions`` does not change the number.
        """
        return len(self._adapter.replica.list_mcp_rows())

    async def count_tools(self) -> int:
        """Total number of tools across all replica rows."""
        total = 0
        for row in self._adapter.replica.list_mcp_rows():
            tool_list = row.get("tool_list")
            if isinstance(tool_list, list):
                total += len(tool_list)
        return total

    async def get_tool_overrides(
        self,
        path: str,
    ) -> dict[str, Any]:
        """The server's raw tool_overrides map (carried in metadata)."""
        doc = await self.get(path)
        if not doc:
            return {}
        overrides = doc.get("tool_overrides") or {}
        return overrides if isinstance(overrides, dict) else {}

    async def get_blocked_tools(
        self,
        path: str,
    ) -> set[str]:
        """Names of tools currently blocked on a server."""
        overrides = await self.get_tool_overrides(path)
        return {
            name
            for name, entry in overrides.items()
            if isinstance(entry, dict) and entry.get("blocked") is True
        }

    async def find_with_filter(
        self,
        filter_dict: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> dict[str, dict]:
        """Match assembled documents against a Mongo-style filter.

        Supports the operators the registry actually issues on this
        collection (equality, ``$in``, ``$ne``, ``$regex``, ``$exists``,
        dot-notation into metadata) evaluated in Python over the replica.
        Unknown operators raise rather than silently matching nothing.
        """
        results: dict[str, dict] = {}
        for row in self._adapter.replica.list_mcp_rows():
            doc = mcp_row_to_doc(row)
            doc_id = doc.pop("_id")
            doc["path"] = doc_id
            if _matches_filter(doc, filter_dict):
                results[doc_id] = doc
                if limit is not None and len(results) >= limit:
                    break
        return results

    # ── helpers ──────────────────────────────────────────────────────────

    def _existing_id(self, path: str) -> str | None:
        """Resolve the spine id for a caller path (trailing-slash variants)."""
        for candidate in self._path_variants(path):
            if self._adapter.replica.get_mcp_row(candidate) is not None:
                return candidate
        return None

    @staticmethod
    def _path_variants(path: str) -> list[str]:
        """Both trailing-slash forms of a server path, the caller's form first.

        Same convention as ``DocumentDBServerRepository._path_variants``.
        """
        stem = "/" + path.strip("/")
        variants = [stem, stem + "/"]
        if path in variants:
            variants.remove(path)
            variants.insert(0, path)
        return variants


def _matches_filter(doc: dict[str, Any], filter_dict: dict[str, Any]) -> bool:
    """Evaluate a Mongo-style equality filter against an assembled document."""
    for key, expected in filter_dict.items():
        actual = _dot_get(doc, key)
        if isinstance(expected, dict):
            if not _matches_operators(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def _matches_operators(actual: Any, conditions: dict[str, Any]) -> bool:
    """Evaluate the supported Mongo operator subset for one field."""
    for operator, operand in conditions.items():
        if operator == "$in":
            if actual not in operand:
                return False
        elif operator == "$ne":
            if actual == operand:
                return False
        elif operator == "$regex":
            if not isinstance(actual, str) or not re.search(operand, actual):
                return False
        elif operator == "$exists":
            if (actual is not None) != bool(operand):
                return False
        else:
            raise ValueError(
                f"Unsupported filter operator {operator!r} in spine find_with_filter"
            )
    return True


def _dot_get(doc: dict[str, Any], dotted: str) -> Any:
    """Resolve a dot-notation path (``metadata.surface.commercial_tools``)."""
    current: Any = doc
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current
