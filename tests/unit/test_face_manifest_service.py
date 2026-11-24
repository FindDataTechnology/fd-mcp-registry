"""Unit tests for the face manifest importer's pure planner (capability ``tool-face``)."""

from registry.schemas.face_manifest import BrandPolicy, FaceManifest, ToolSelection
from registry.schemas.virtual_server_models import (
    ToolMapping,
    VirtualServerConfig,
)
from registry.services.face_manifest_service import (
    _manifest_tool_mappings,
    plan_face_import,
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
