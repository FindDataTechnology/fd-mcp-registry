"""Unit tests for the vendor onboarding lane (fd-vendor-lane).

Mounts the lane's router in a fresh FastAPI app and patches its service
edges (``server_service`` + registration gate), mirroring the same wiring
the console routes use — the assertions pin the CONTRACT differences:

- the lane is shared-secret bearer gated (env unset ⇒ 503; wrong ⇒ 401);
- register always passes for the remote shape and lands via the same
  ``server_service.register_server`` entry construction used by the console;
- the token IS the authority (no per-user permission shape to defer to);
- audit actions are attributable ("vendor-lane" actor fingerprint).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.vendor_lane_routes import router as vendor_lane_router


TOKEN = "srf_test_lane_token"


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(vendor_lane_router)
    monkeypatch.setattr("registry.api.vendor_lane_routes.validate_internal_auth", AsyncMock(return_value=None))
    monkeypatch.setenv("VENDOR_LANE_TOKEN", TOKEN)
    with TestClient(app) as c:
        yield c


def _ok_gate():
    return SimpleNamespace(allowed=True)


def _bad_gate(msg="denied-by-policy"):
    return SimpleNamespace(allowed=False, error_message=msg)


def test_register_token_unset_is_503(monkeypatch):
    app = FastAPI()
    app.include_router(vendor_lane_router)
    monkeypatch.delenv("VENDOR_LANE_TOKEN", raising=False)
    c = TestClient(app)
    r = c.post("/api/vendor-servers/register", json={"name": "x", "path": "/x/", "proxy_pass_url": "http://up"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503


def test_register_wrong_token_is_401(client):
    r = client.post("/api/vendor-servers/register", json={"name": "x", "path": "/x/", "proxy_pass_url": "http://up"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_register_success_calls_server_service(client, monkeypatch):
    register_mock = AsyncMock(return_value={"success": True, "server_id": "abc"})
    gate_mock = AsyncMock(side_effect=lambda **kw: _ok_gate())
    monkeypatch.setattr("registry.api.vendor_lane_routes.server_service.register_server", register_mock)
    monkeypatch.setattr("registry.api.vendor_lane_routes.check_registration_gate", gate_mock)
    monkeypatch.setattr("registry.api.vendor_lane_routes.set_audit_action", lambda *a, **kw: None)
    r = client.post(
        "/api/vendor-servers/register",
        json={
            "name": "acme-tools",
            "description": "acme",
            "path": "/acme-tools/",
            "proxy_pass_url": "http://100.64.0.9:8090/mcp",
            "license": "MIT",
            "tags": ["vendor", "acme"],
            "num_tools": 2,
            "metadata": {"surface": {"commercial_tools": ["t1", "t2"]}},
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    entry = register_mock.await_args.args[0]
    assert entry["server_name"] == "acme-tools"
    assert entry["path"] == "/acme-tools/"
    assert entry["registered_by"] == "vendor-lane"
    assert entry["metadata"]["surface"]["commercial_tools"] == ["t1", "t2"]
    assert entry["deployment"] == entry["deployment"]  # remote shape retained
    gate_mock.assert_awaited_once()


def test_register_gate_denial_is_403(client, monkeypatch):
    monkeypatch.setattr("registry.api.vendor_lane_routes.server_service.register_server", AsyncMock())
    monkeypatch.setattr("registry.api.vendor_lane_routes.check_registration_gate", AsyncMock(side_effect=lambda **kw: _bad_gate()))
    r = client.post("/api/vendor-servers/register", json={"name": "x", "path": "/x/", "proxy_pass_url": "http://up"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403


def test_register_conflict_maps_409(client, monkeypatch):
    monkeypatch.setattr("registry.api.vendor_lane_routes.server_service.register_server", AsyncMock(return_value={"success": False, "message": "path already registered"}))
    monkeypatch.setattr("registry.api.vendor_lane_routes.check_registration_gate", AsyncMock(side_effect=lambda **kw: _ok_gate()))
    r = client.post("/api/vendor-servers/register", json={"name": "x", "path": "/x/", "proxy_pass_url": "http://up"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 409


def test_toggle_missing_server_is_404(client, monkeypatch):
    monkeypatch.setattr("registry.api.vendor_lane_routes.server_service.get_server_info", AsyncMock(return_value=None))
    r = client.post("/api/vendor-servers/toggle", json={"path": "/ghost/", "new_state": True}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


def test_toggle_success_flips_state(client, monkeypatch):
    monkeypatch.setattr("registry.api.vendor_lane_routes.server_service.get_server_info", AsyncMock(return_value={"server_name": "acme-tools", "_id": "abc"}))
    toggle_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("registry.api.vendor_lane_routes.server_service.toggle_service", toggle_mock)
    monkeypatch.setattr("registry.api.vendor_lane_routes.set_audit_action", lambda *a, **kw: None)
    r = client.post("/api/vendor-servers/toggle", json={"path": "/acme-tools/", "new_state": True}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert toggle_mock.await_args.args == ("/acme-tools/", True)
