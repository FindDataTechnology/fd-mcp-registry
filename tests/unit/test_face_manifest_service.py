"""Unit tests for the face manifest importer's pure planner (capability ``tool-face``)."""

from unittest.mock import AsyncMock, patch

import pytest

from registry.exceptions import VirtualServerValidationError
from registry.schemas.face_manifest import BrandPolicy, FaceManifest, ToolSelection
from registry.schemas.virtual_server_models import (
    ToolMapping,
    VirtualServerConfig,
)
from registry.services.face_manifest_service import (
    _manifest_tool_mappings,
    plan_face_import,
    reconcile_face,
)


def _manifest(**overrides) -> FaceManifest:
    base: dict = {
        "face": "wire-customer",
        "server_name": "Wire Customer",
        "description": "d",
        "audience": "paying-customer",
        "exposure": "commercial",
        "required_scopes": ["mcp-wire-execute"],
        "is_enabled": True,
        "brand_policy": BrandPolicy(strip_provenance=True),
        "tools": [
            ToolSelection(
                backend="/wire-ops-mcp",
                tool_name="wire_demands_list",
                alias="wire_demands_list",
            ),
            ToolSelection(backend="/fd-info", tool_name="search_info", alias="info_search"),
        ],
    }
    base.update(overrides)
    return FaceManifest(**base)


def _config_from(manifest: FaceManifest, **overrides) -> VirtualServerConfig:
    """A registry config that already matches ``manifest`` (the noop baseline)."""
    base: dict = {
        "path": manifest.path,
        "server_name": manifest.server_name,
        "description": manifest.description,
        "required_scopes": list(manifest.required_scopes),
        "tool_mappings": _manifest_tool_mappings(manifest),
        "is_enabled": manifest.is_enabled,
    }
    base.update(overrides)
    return VirtualServerConfig(**base)


def test_absent_face_plans_create():
    plan = plan_face_import(_manifest(), None)
    assert plan.action == "create"
    assert plan.path == "/virtual/wire-customer"


def test_matching_face_plans_noop():
    m = _manifest()
    plan = plan_face_import(m, _config_from(m))
    assert plan.action == "noop"
    assert plan.changes == {}


def test_idempotent_second_run_is_noop():
    m = _manifest()
    first = plan_face_import(m, None)  # would create
    assert first.action == "create"
    # After create, the stored config matches; re-planning is a noop.
    second = plan_face_import(m, _config_from(m))
    assert second.action == "noop"


def test_alias_change_plans_update_with_field():
    m = _manifest()
    current = _config_from(
        m,
        tool_mappings=[
            ToolMapping(
                tool_name="wire_demands_list",
                alias="wire_demands_list",
                backend_server_path="/wire-ops-mcp",
            ),
            ToolMapping(
                tool_name="search_info",
                alias="OLD_alias",
                backend_server_path="/fd-info",
            ),
        ],
    )
    plan = plan_face_import(m, current)
    assert plan.action == "update"
    assert "tool_mappings" in plan.changes


def test_tool_mapping_order_independent():
    m = _manifest()
    reversed_current = _config_from(m, tool_mappings=list(reversed(_manifest_tool_mappings(m))))
    plan = plan_face_import(m, reversed_current)
    assert plan.action == "noop"


def test_scope_change_plans_update():
    m = _manifest(required_scopes=["mcp-wire-execute", "extra"])
    current = _config_from(_manifest())  # baseline scopes
    plan = plan_face_import(m, current)
    assert plan.action == "update"
    assert plan.changes["required_scopes"] == ["mcp-wire-execute", "extra"]


def test_enabled_toggle_plans_update():
    m = _manifest(is_enabled=False)
    current = _config_from(_manifest(is_enabled=True))
    plan = plan_face_import(m, current)
    assert plan.action == "update"
    assert plan.changes["is_enabled"] is False


# --- reconcile ---


def test_reconcile_missing_face_is_drift():
    r = reconcile_face(_manifest(), None)
    assert r.drift is True
    assert r.reason == "missing"
    assert r.face == "wire-customer"


def test_reconcile_matching_face_is_ok():
    m = _manifest()
    r = reconcile_face(m, _config_from(m))
    assert r.drift is False
    assert r.reason == "ok"


def test_reconcile_stale_face_is_drift():
    m = _manifest(server_name="Renamed Face")
    current = _config_from(_manifest())  # old name
    r = reconcile_face(m, current)
    assert r.drift is True
    assert r.reason == "stale"


# --- closure: a face's (server, tool) must exist in the backend tool surface ---


@pytest.mark.asyncio
async def test_face_mappings_closed_against_backend_index():
    """A face mapping to a tool the backend does not expose is rejected by name.

    The importer reuses VirtualServerService._validate_tool_mappings; this pins
    that a face whose backend lacks the tool fails closure validation and names
    the offending server.
    """
    from registry.services.virtual_server_service import VirtualServerService

    manifest = _manifest(
        tools=[
            ToolSelection(
                backend="/wire-ops-mcp",
                tool_name="does_not_exist",
                alias="does_not_exist",
            )
        ]
    )
    server_repo = AsyncMock()
    server_repo.get.return_value = {
        "server_name": "wire-ops",
        "tool_list": [{"name": "wire_demands_list", "description": ""}],
    }
    with (
        patch(
            "registry.services.virtual_server_service.get_virtual_server_repository",
            return_value=AsyncMock(),
        ),
        patch(
            "registry.services.virtual_server_service.get_server_repository",
            return_value=server_repo,
        ),
    ):
        service = VirtualServerService()
        with pytest.raises(VirtualServerValidationError, match="not found in backend"):
            await service._validate_tool_mappings(_manifest_tool_mappings(manifest))
