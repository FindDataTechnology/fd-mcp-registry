"""Shared surface writer (add-self-reported-tool-surface 1.1 / D3).

The probe and the self-report route must go through ONE writer: snapshot, scope
rows and console broadcast cannot drift apart. These pins cover the writer's
own contract plus the extraction's equivalence with the old inline block.
"""

from __future__ import annotations

import pytest

from registry.health import service as mod


class Store:
    def __init__(self, entry):
        self.entry = dict(entry) if entry is not None else None
        self.writes = []

    async def get_server_info(self, path, *args, **kwargs):
        return dict(self.entry) if self.entry is not None else None

    async def update_server(self, path, server_info, *args, **kwargs):
        self.writes.append({"info": dict(server_info), "fields": kwargs.get("updated_fields")})
        self.entry.update(server_info)
        return True


@pytest.fixture
def wired(monkeypatch):
    store = Store({
        "server_name": "wire-build-mcp",
        "num_tools": 1,
        "tool_list": [{"name": "old"}],
        "surface_report_token_hash": "deadbeef",
        "backend_auth": {"encrypted": "keep-me"},
    })
    import registry.services.server_service as server_mod
    monkeypatch.setattr(server_mod, "server_service", store)
    scopes = []

    async def fake_scopes(path, name, tools):
        scopes.append((path, name, list(tools)))

    import registry.services.scope_service as scope_mod
    monkeypatch.setattr(scope_mod, "update_server_scopes", fake_scopes)
    return store, scopes


@pytest.mark.anyio
async def test_unchanged_surface_is_a_no_op(wired):
    store, scopes = wired
    broadcasts = []

    async def bc(path=None):
        broadcasts.append(path)

    result = await mod.apply_tool_surface(
        "/wire-build-mcp", [{"name": "old"}], broadcast=bc
    )

    assert result == {"updated": False, "num_tools": 1, "reason": "unchanged"}
    assert store.writes == []          # no write, no scope churn, no broadcast
    assert scopes == []
    assert broadcasts == []


@pytest.mark.anyio
async def test_changed_surface_writes_scoped_fields(wired):
    store, scopes = wired
    broadcasts = []

    async def bc(path=None):
        broadcasts.append(path)

    result = await mod.apply_tool_surface(
        "/wire-build-mcp", [{"name": "a"}, {"name": "b"}], broadcast=bc
    )

    assert result == {"updated": True, "num_tools": 2, "reason": "changed"}
    assert len(store.writes) == 1
    fields = store.writes[0]["fields"]
    assert set(fields) == {"tool_list", "num_tools"}
    # The credential-bearing keys are never in the write set, so a stripped
    # read cannot erase them ($set leaves absent keys alone).
    assert "surface_report_token_hash" not in fields
    assert "backend_auth" not in fields
    assert store.entry["surface_report_token_hash"] == "deadbeef"
    assert scopes == [("/wire-build-mcp", "wire-build-mcp", ["a", "b"])]
    assert broadcasts == ["/wire-build-mcp"]


@pytest.mark.anyio
async def test_version_change_tracks_previous(wired):
    store, _scopes = wired

    await mod.apply_tool_surface(
        "/wire-build-mcp",
        [{"name": "a"}],
        {"version": "0.2.0"},
        broadcast=None,
    )

    fields = store.writes[0]["fields"]
    assert "mcp_server_version" in fields
    assert store.entry["mcp_server_version"] == "0.2.0"
    # First version ever seen: nothing to remember as previous.
    assert "mcp_server_version_previous" not in fields

    store.entry["mcp_server_version"] = "0.1.0"
    store.writes.clear()
    await mod.apply_tool_surface(
        "/wire-build-mcp", [{"name": "a"}], {"version": "0.2.0"}, broadcast=None
    )
    fields = store.writes[0]["fields"]
    assert "mcp_server_version_previous" in fields
    assert store.entry["mcp_server_version_previous"] == "0.1.0"


@pytest.mark.anyio
async def test_missing_entry_is_reported_not_written(monkeypatch):
    store = Store(None)
    import registry.services.server_service as server_mod
    monkeypatch.setattr(server_mod, "server_service", store)

    result = await mod.apply_tool_surface("/nope", [{"name": "a"}])

    assert result["updated"] is False and result["reason"] == "no_entry"
    assert store.writes == []
