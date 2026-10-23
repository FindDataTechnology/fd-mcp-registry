"""Self-reported tool surface route (add-self-reported-tool-surface 1.3).

Four pins from the task:
  1. legal report writes the full surface (snapshot + scopes + broadcast)
  2. a token bound to another path is refused
  3. no token → 401
  4. re-reporting the same surface is a no-op
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api import surface_report_routes as mod


class FakeServerService:
    def __init__(self, entries):
        self.entries = {k: dict(v) for k, v in entries.items()}
        self.updates = []

    async def get_server_info(self, path, *args, **kwargs):
        info = self.entries.get(path)
        return dict(info) if info else None

    async def update_server(self, path, server_info, *args, **kwargs):
        self.entries[path] = dict(server_info)
        self.updates.append(path)
        return True


@pytest.fixture
def client(monkeypatch):
    svc = FakeServerService({
        "/wire-build-mcp": {"server_name": "wire-build-mcp", "num_tools": 15,
                            "tool_list": [{"name": "old_tool"}], "surface_source": "self_report"},
        "/other-mcp": {"server_name": "other", "num_tools": 1,
                       "tool_list": [{"name": "x"}], "surface_source": "self_report"},
    })
    token, updated = mod.issue_surface_token(svc.entries["/wire-build-mcp"])
    svc.entries["/wire-build-mcp"] = updated
    other_token, other_updated = mod.issue_surface_token(svc.entries["/other-mcp"])
    svc.entries["/other-mcp"] = other_updated

    monkeypatch.setattr(mod, "server_service", svc)

    written = {}

    async def fake_apply(service_path, tool_list, mcp_server_info=None, *, broadcast=None):
        written["path"] = service_path
        written["tools"] = [t["name"] for t in tool_list]
        written["version"] = (mcp_server_info or {}).get("version")
        written["broadcast"] = broadcast is not None
        if broadcast is not None:
            await broadcast(service_path)
        return {"updated": True, "num_tools": len(tool_list), "reason": "changed"}

    monkeypatch.setattr(mod, "apply_tool_surface", fake_apply)

    broadcasts = []

    async def fake_broadcast(path=None):
        broadcasts.append(path)

    monkeypatch.setattr(mod.health_service, "broadcast_health_update", fake_broadcast, raising=False)

    app = FastAPI()
    app.include_router(mod.router, prefix="/api")
    return TestClient(app), svc, token, other_token, written, broadcasts


def test_legal_report_writes_full_surface(client):
    c, svc, token, _other, written, broadcasts = client
    r = c.post("/api/surface-report/servers/wire-build-mcp/tool-list",
               headers={"Authorization": f"Bearer {token}"},
               json={"tools": [{"name": "a"}, {"name": "b"}], "surface_version": "sha-9"})
    assert r.status_code == 200, r.text
    assert written["path"] == "/wire-build-mcp"
    assert written["tools"] == ["a", "b"]
    assert written["version"] == "sha-9"
    assert written["broadcast"] is True          # scope/broadcast ride the shared writer
    assert broadcasts == ["/wire-build-mcp"]

    body = r.json()
    assert body["num_tools"] == 2 and body["updated"] is True
    # freshness marker for the console/ledger
    assert "last_surface_report_at" in svc.entries["/wire-build-mcp"]


def test_token_for_another_path_is_refused(client):
    c, _svc, _token, other_token, written, _b = client
    r = c.post("/api/surface-report/servers/wire-build-mcp/tool-list",
               headers={"Authorization": f"Bearer {other_token}"},
               json={"tools": [{"name": "a"}]})
    assert r.status_code == 403
    assert written == {}


def test_missing_token_is_401(client):
    c, _svc, _token, _other, written, _b = client
    r = c.post("/api/surface-report/servers/wire-build-mcp/tool-list",
               json={"tools": [{"name": "a"}]})
    assert r.status_code == 401
    assert written == {}


def test_unknown_path_is_404(client):
    c, _svc, token, _other, written, _b = client
    r = c.post("/api/surface-report/servers/nope/tool-list",
               headers={"Authorization": f"Bearer {token}"},
               json={"tools": [{"name": "a"}]})
    assert r.status_code == 404
    assert written == {}


def test_repeat_report_is_idempotent_at_the_writer(client, monkeypatch):
    """Same surface twice: the shared writer's short-circuit governs; the route
    must not add its own write on top."""
    c, svc, token, _other, written, _b = client
    calls = []

    async def unchanged(service_path, tool_list, mcp_server_info=None, *, broadcast=None):
        calls.append(service_path)
        return {"updated": False, "num_tools": len(tool_list), "reason": "unchanged"}

    monkeypatch.setattr(mod, "apply_tool_surface", unchanged)
    r = c.post("/api/surface-report/servers/wire-build-mcp/tool-list",
               headers={"Authorization": f"Bearer {token}"},
               json={"tools": [{"name": "a"}]})
    assert r.status_code == 200
    assert r.json()["updated"] is False
    assert len(calls) == 1


def test_empty_tool_list_is_rejected(client):
    c, _svc, token, _other, written, _b = client
    r = c.post("/api/surface-report/servers/wire-build-mcp/tool-list",
               headers={"Authorization": f"Bearer {token}"},
               json={"tools": []})
    assert r.status_code == 422
    assert written == {}
