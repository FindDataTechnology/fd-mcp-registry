"""Import a declarative tool face into the registry's virtual servers.

A face manifest (``registry/schemas/face_manifest.py``) is the single source of
truth; this service makes a registry ``virtual_server`` match it. The work is
split so the policy is testable without MongoDB or nginx:

  * :func:`plan_face_import` is pure. Given a manifest and the face's current
    config (or None), it returns the action to take (``create`` / ``update`` /
    ``noop``) and, for an update, the concrete field changes. No I/O.
  * :func:`apply_face_import` is the thin async shell that reads the current
    config, calls the pure planner, and performs the create/update through the
    existing :class:`VirtualServerService` (which already validates tool closure
    against the backend server index and reloads nginx).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from registry.schemas.face_manifest import FaceManifest
from registry.schemas.virtual_server_models import (
    CreateVirtualServerRequest,
    ToolMapping,
    VirtualServerConfig,
)

logger = logging.getLogger(__name__)

FaceImportAction = Literal["create", "update", "noop"]


@dataclass(frozen=True)
class FaceImportPlan:
    """What importing one face would do.

    Attributes:
        action: ``create`` when the face is absent, ``update`` when it exists but
            differs, ``noop`` when it already matches the manifest.
        path: The virtual server path (``/virtual/<face>``).
        changes: For an ``update``, the fields that differ and their new values.
    """

    action: FaceImportAction
    path: str
    changes: dict[str, Any]

    @property
    def is_change(self) -> bool:
        """True when applying this plan would write."""
        return self.action in ("create", "update")


def _manifest_tool_mappings(manifest: FaceManifest) -> list[ToolMapping]:
    """Map the manifest's tool selections to registry ``ToolMapping`` objects.

    The manifest alias is always set, so the mapping is fully deterministic
    (``alias`` explicitly carries the served name even when it equals the tool).
    """
    return [
        ToolMapping(
            tool_name=sel.tool_name,
            alias=sel.alias,
            backend_server_path=sel.backend,
        )
        for sel in manifest.tools
    ]


def _mapping_key(mappings: list[ToolMapping]) -> list[tuple[str, str, str]]:
    """Order-independent identity of a mapping set, for diffing."""
    return sorted((m.backend_server_path, m.tool_name, m.alias or m.tool_name) for m in mappings)


def _scalar_changes(
    manifest: FaceManifest,
    existing: VirtualServerConfig,
    mappings: list[ToolMapping],
) -> dict[str, Any]:
    """Fields that differ between the manifest and the existing config."""
    changes: dict[str, Any] = {}
    if existing.server_name != manifest.server_name:
        changes["server_name"] = manifest.server_name
    if (existing.description or "") != manifest.description:
        changes["description"] = manifest.description
    if sorted(existing.required_scopes) != sorted(manifest.required_scopes):
        changes["required_scopes"] = manifest.required_scopes
    if _mapping_key(existing.tool_mappings) != _mapping_key(mappings):
        changes["tool_mappings"] = [m.model_dump() for m in mappings]
    if existing.is_enabled != manifest.is_enabled:
        changes["is_enabled"] = manifest.is_enabled
    return changes


def plan_face_import(
    manifest: FaceManifest,
    existing: VirtualServerConfig | None,
) -> FaceImportPlan:
    """Decide what importing ``manifest`` should do, given the current config.

    Pure and side-effect free so the idempotence contract is unit-testable.

    Args:
        manifest: The validated face manifest (the desired state).
        existing: The face's current config, or None when it does not exist.

    Returns:
        A :class:`FaceImportPlan`. Running the importer twice with no manifest
        change yields ``noop`` the second time.
    """
    path = manifest.path
    mappings = _manifest_tool_mappings(manifest)
    if existing is None:
        return FaceImportPlan(action="create", path=path, changes={})

    changes = _scalar_changes(manifest, existing, mappings)
    if not changes:
        return FaceImportPlan(action="noop", path=path, changes={})
    return FaceImportPlan(action="update", path=path, changes=changes)


def _create_request(manifest: FaceManifest) -> CreateVirtualServerRequest:
    """Build the creation request for a new face."""
    return CreateVirtualServerRequest(
        server_name=manifest.server_name,
        path=manifest.path,
        description=manifest.description,
        tool_mappings=_manifest_tool_mappings(manifest),
        required_scopes=manifest.required_scopes,
    )


async def apply_face_import(
    manifest: FaceManifest,
    *,
    dry_run: bool = False,
) -> FaceImportPlan:
    """Import one face into the registry (create/update/noop).

    The tool-closure check and nginx reload are performed by
    :class:`VirtualServerService`, so this function only plans and dispatches.

    Args:
        manifest: The validated face manifest.
        dry_run: When True, plan only; do not read-through to any write.

    Returns:
        The applied :class:`FaceImportPlan` (its ``action`` is what was done, or
        what *would* be done under ``dry_run``).
    """
    from registry.repositories.factory import get_virtual_server_repository

    repo = get_virtual_server_repository()
    existing = await repo.get(manifest.path)
    plan = plan_face_import(manifest, existing)
    if dry_run or not plan.is_change:
        return plan

    from registry.schemas.virtual_server_models import UpdateVirtualServerRequest
    from registry.services.virtual_server_service import get_virtual_server_service

    service = get_virtual_server_service()
    if plan.action == "create":
        await service.create_virtual_server(_create_request(manifest))
    else:
        await service.update_virtual_server(
            manifest.path, UpdateVirtualServerRequest(**plan.changes)
        )

    # is_enabled is set by the manifest but not part of the create/update request
    # shapes; reconcile it explicitly so a manifest toggle takes effect.
    if "is_enabled" in plan.changes or plan.action == "create":
        fresh = await repo.get(manifest.path)
        if fresh is not None and fresh.is_enabled != manifest.is_enabled:
            await repo.update(manifest.path, {"is_enabled": manifest.is_enabled})

    logger.info(
        "face import %s: %s (%d change field(s))",
        manifest.face,
        plan.action,
        len(plan.changes),
    )
    return plan
