"""Spine-backed skill repository — the registry execution-plane adapter.

Same split as :mod:`.server_repository`: reads assemble ``SkillCard`` models
from the pull-only local replica (1:1 with what ``DocumentDBSkillRepository``
returned); writes forward to the spine write gate with the acting identity
and raise the gate's errors verbatim. Federation batch operations map onto
sequential gate writes (the book is tens of rows).
"""

from __future__ import annotations

import logging
from typing import Any

from ...exceptions import SkillAlreadyExistsError, SkillServiceError
from ...schemas.skill_models import SkillCard
from ..interfaces import SkillRepositoryBase
from .actor_context import current_actor
from .adapter import SpineAdapter
from .mapping import (
    skill_card_to_upsert_entry,
    skill_row_to_card_dict,
    skill_updates_to_patch_fields,
)
from .write_gate import SpineGateError

logger = logging.getLogger(__name__)


class SpineSkillRepository(SkillRepositoryBase):
    """SkillRepositoryBase over the spine read-only replica + write gate."""

    def __init__(self, adapter: SpineAdapter):
        self._adapter = adapter

    async def ensure_indexes(self) -> None:
        """No-op: the replica's SQLite schema is ensured at construction."""

    # ── reads ────────────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
    ) -> SkillCard | None:
        """Get a skill by path (the spine skills row keyed by that path)."""
        row = self._adapter.replica.get_skill_row(path)
        if row is None:
            return None
        return self._to_card(row, path)

    async def list_all(
        self,
        skip: int = 0,
        limit: int = 100,
    ) -> list[SkillCard]:
        """List all skills with pagination (ordered by spine id)."""
        cards: list[SkillCard] = []
        for row in self._adapter.replica.list_skill_rows():
            card = self._to_card(row, str(row.get("name")))
            if card is not None:
                cards.append(card)
        return cards[skip : skip + limit]

    async def list_paginated(
        self,
        skip: int = 0,
        limit: int = 100,
    ) -> list[SkillCard]:
        """Deterministically ordered page (replica rows are id-ordered)."""
        return await self.list_all(skip=skip, limit=limit)

    async def list_by_paths(
        self,
        paths: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Raw card dicts (not SkillCard) keyed by path, for the given paths."""
        found: dict[str, dict[str, Any]] = {}
        for path in paths:
            row = self._adapter.replica.get_skill_row(path)
            if row is None:
                continue
            try:
                found[path] = skill_row_to_card_dict(row)
            except Exception as exc:  # noqa: BLE001 - per-row tolerance
                logger.error("Failed to assemble skill row '%s': %s", path, exc)
        return found

    async def list_filtered(
        self,
        include_disabled: bool = False,
        tag: str | None = None,
        visibility: str | None = None,
        registry_name: str | None = None,
        limit: int | None = None,
    ) -> list[SkillCard]:
        """List skills with the legacy filter semantics applied in Python."""
        skills: list[SkillCard] = []
        for row in self._adapter.replica.list_skill_rows():
            if not include_disabled and not _row_enabled(row):
                continue
            if tag is not None:
                tags = row.get("tags")
                if not isinstance(tags, list) or tag not in tags:
                    continue
            if visibility is not None and row.get("visibility") != visibility:
                continue
            card = self._to_card(row, str(row.get("name")))
            if card is None:
                continue
            if registry_name is not None and card.registry_name != registry_name:
                continue
            skills.append(card)
            if limit is not None and len(skills) >= limit:
                break
        return skills

    async def list_proxied(self) -> list[dict[str, Any]]:
        """Projected proxied-skill rows for the nginx render hot path."""
        projection_fields = (
            "is_proxied",
            "is_enabled",
            "proxy_target_url",
            "proxy_streaming",
            "custom_header_names",
            "custom_header_overridable_names",
            "custom_headers_encrypted",
            "proxy_resolved_ips",
            "proxy_target_host",
            "proxy_disabled_reason",
            "sync_metadata",
        )
        rows: list[dict[str, Any]] = []
        for row in self._adapter.replica.list_skill_rows():
            card_dict = skill_row_to_card_dict(row)
            if not card_dict.get("is_proxied"):
                continue
            projected = {field: card_dict.get(field) for field in projection_fields}
            projected["path"] = card_dict.get("path")
            rows.append(projected)
        return rows

    # ── writes (forwarded to the spine gate) ─────────────────────────────

    async def create(
        self,
        skill: SkillCard,
    ) -> SkillCard:
        """Create a skill: one upsert through the spine write gate.

        The gate upserts (no duplicate rejection); the service layer's
        duplicate pre-check stays the 409 path exactly as before.
        """
        entry = skill_card_to_upsert_entry(skill.model_dump(mode="json"))
        response = await self._adapter.gate.skill_upsert(actor=current_actor(), entry=entry)
        logger.info("Spine WRITE: upserted skill '%s' via gate", entry.get("name"))
        await self._adapter.resync_after_write()
        return self._card_from_gate_row(response, fallback_card=skill)

    async def update(
        self,
        path: str,
        updates: dict[str, Any],
    ) -> SkillCard | None:
        """Update a skill: forwarded as a spine patch; None when unknown."""
        row = self._adapter.replica.get_skill_row(path)
        if row is None:
            logger.error("Skill '%s' not found in spine replica", path)
            return None
        patch_fields, _ = skill_updates_to_patch_fields(updates)
        if not patch_fields:
            logger.info("Spine WRITE: nothing forwardable for skill '%s' update", path)
            return None
        response = await self._adapter.gate.skill_patch(
            actor=current_actor(),
            skill_name=path,
            fields=patch_fields,
        )
        logger.info("Spine WRITE: patched skill '%s' via gate", path)
        await self._adapter.resync_after_write()
        if response:
            return self._to_card(response, path)
        return await self.get(path)

    async def delete(
        self,
        path: str,
    ) -> bool:
        """Delete a skill through the gate; False when the spine has no row."""
        if self._adapter.replica.get_skill_row(path) is None:
            logger.error("Skill '%s' not found in spine replica", path)
            return False
        try:
            await self._adapter.gate.skill_delete(actor=current_actor(), skill_name=path)
        except SpineGateError as exc:
            if exc.status == 404:
                logger.error("Spine gate reports no skill row '%s'", path)
                return False
            raise
        logger.info("Spine WRITE: deleted skill '%s' via gate", path)
        await self._adapter.resync_after_write()
        return True

    async def get_state(
        self,
        path: str,
    ) -> bool:
        """Get skill enabled state."""
        row = self._adapter.replica.get_skill_row(path)
        if row is None:
            return False
        return _row_enabled(row)

    async def set_state(
        self,
        path: str,
        enabled: bool,
    ) -> bool:
        """Set skill enabled state (spine patch column)."""
        if self._adapter.replica.get_skill_row(path) is None:
            logger.error("Skill '%s' not found in spine replica", path)
            return False
        await self._adapter.gate.skill_patch(
            actor=current_actor(),
            skill_name=path,
            fields={"is_enabled": bool(enabled)},
        )
        logger.info("Spine WRITE: set skill '%s' is_enabled=%s via gate", path, enabled)
        await self._adapter.resync_after_write()
        return True

    async def create_many(
        self,
        skills: list[SkillCard],
    ) -> list[SkillCard]:
        """Batch create: sequential gate upserts (book is tens of rows)."""
        created: list[SkillCard] = []
        for skill in skills:
            try:
                created.append(await self.create(skill))
            except SkillAlreadyExistsError:
                raise
            except SpineGateError as exc:
                logger.error("Batch create failed for '%s': %s", skill.path, exc)
                raise SkillServiceError(f"Batch create failed: {exc.message}") from exc
        return created

    async def update_many(
        self,
        updates: dict[str, dict[str, Any]],
    ) -> int:
        """Batch update by path: sequential gate patches; returns the count."""
        count = 0
        for path, update_data in updates.items():
            try:
                result = await self.update(path, update_data)
            except SpineGateError as exc:
                logger.error("Batch update failed for '%s': %s", path, exc)
                raise SkillServiceError(f"Batch update failed: {exc.message}") from exc
            if result is not None:
                count += 1
        return count

    async def count(self) -> int:
        """Total count of skills in the replica."""
        return len(self._adapter.replica.list_skill_rows())

    # ── helpers ──────────────────────────────────────────────────────────

    def _to_card(self, row: dict[str, Any], path: str) -> SkillCard | None:
        """Assemble one SkillCard; per-row tolerance like the DocumentDB impl."""
        try:
            return SkillCard(**skill_row_to_card_dict(row))
        except Exception as exc:  # noqa: BLE001 - mirror per-doc parse tolerance
            logger.error("Failed to parse skill row '%s': %s", path, exc)
            return None

    def _card_from_gate_row(
        self,
        response: dict[str, Any] | None,
        fallback_card: SkillCard,
    ) -> SkillCard:
        """Build the returned card from the gate's upsert response."""
        if isinstance(response, dict) and response.get("name"):
            card = self._to_card(response, str(response.get("name")))
            if card is not None:
                return card
        return fallback_card


def _row_enabled(row: dict[str, Any]) -> bool:
    value = row.get("is_enabled")
    if isinstance(value, str):
        return value.lower() == "true" or value == "1"
    return bool(value)
