"""Unit tests for registry/api/sub2api_caller_map_routes.py (3.1).

Same discipline as test_patch_key_routes: the router alone on a minimal
FastAPI app, service mocked, auth faked via the nginx_proxied_auth
dependency override. Admin gate is the surface under test.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_test_app() -> FastAPI:
    from registry.api.sub2api_caller_map_routes import router

    app = FastAPI()
    app.include_router(router)
    return app


app = _build_test_app()


def _override_auth(user_context: dict | None) -> None:
    from registry.auth.dependencies import nginx_proxied_auth

    app.dependency_overrides[nginx_proxied_auth] = lambda: user_context


@pytest.fixture
def mock_service() -> MagicMock:
    service = MagicMock()
    service.get_map = AsyncMock(return_value={"drillcust001": "67"})
    service.upsert = AsyncMock()
    service.delete = AsyncMock(return_value=True)
    return service


def _client(user_context: dict | None, mock_service: MagicMock):
    _override_auth(user_context)
    with patch(
        "registry.api.sub2api_caller_map_routes._get_service",
        new=AsyncMock(return_value=mock_service),
    ):
        yield TestClient(app, cookies={"mcp_gateway_session": "test-session"})
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(mock_service):
    ctx: dict[str, Any] = {"username": "wire-platform-service", "is_admin": True}
    yield from _client(ctx, mock_service)


@pytest.fixture
def user_client(mock_service):
    ctx: dict[str, Any] = {"username": "alice", "is_admin": False}
    yield from _client(ctx, mock_service)


def test_get_lists_sorted(admin_client, mock_service):
    mock_service.get_map = AsyncMock(
        return_value={"zeta": "9", "alpha": "1", "drillcust001": "67"}
    )
    resp = admin_client.get("/api/sub2api-caller-map")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [i["username"] for i in body["items"]] == ["alpha", "drillcust001", "zeta"]


def test_put_upserts(admin_client, mock_service):
    resp = admin_client.put(
        "/api/sub2api-caller-map", json={"username": "drillcust001", "value": "67"}
    )
    assert resp.status_code == 200
    mock_service.upsert.assert_awaited_once_with(username="drillcust001", value="67")


def test_put_non_admin_403(user_client, mock_service):
    resp = user_client.put(
        "/api/sub2api-caller-map", json={"username": "drillcust001", "value": "67"}
    )
    assert resp.status_code == 403
    mock_service.upsert.assert_not_awaited()


def test_put_blank_fields_422(admin_client):
    resp = admin_client.put(
        "/api/sub2api-caller-map", json={"username": "  ", "value": "67"}
    )
    assert resp.status_code == 422


def test_delete_admin_ok(admin_client, mock_service):
    resp = admin_client.delete("/api/sub2api-caller-map/drillcust001")
    assert resp.status_code == 200
    mock_service.delete.assert_awaited_once_with("drillcust001")


def test_delete_absent_404(admin_client, mock_service):
    mock_service.delete = AsyncMock(return_value=False)
    resp = admin_client.delete("/api/sub2api-caller-map/nobody")
    assert resp.status_code == 404


def test_get_non_admin_403(user_client):
    assert user_client.get("/api/sub2api-caller-map").status_code == 403
