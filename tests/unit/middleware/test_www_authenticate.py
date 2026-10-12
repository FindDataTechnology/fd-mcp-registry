"""Tests for the WWW-Authenticate middleware (issue #989)."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from registry.middleware.mcp_www_authenticate import WWWAuthenticateMiddleware

RESOURCE_METADATA_URL = "https://gw.example.com/.well-known/oauth-protected-resource"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        WWWAuthenticateMiddleware,
        resource_metadata_url=RESOURCE_METADATA_URL,
    )

    @app.get("/airegistry-tools/mcp")
    async def airegistry_tools_mcp_unauth():
        return JSONResponse(status_code=401, content={"error": "auth required"})

    @app.get("/airegistry-tools/mcp/ok")
    async def airegistry_tools_mcp_ok():
        return JSONResponse(status_code=200, content={"ok": True})

    @app.get("/oauth/token")
    async def oauth_token_unauth():
        return JSONResponse(status_code=401, content={"error": "no token"})

    @app.get("/oauth/authorize")
    async def oauth_authorize_unauth():
        return JSONResponse(status_code=401, content={"error": "no auth"})

    @app.get("/api/auth/me")
    async def api_auth_me_unauth():
        return JSONResponse(status_code=401, content={"error": "no session"})

    @app.get("/airegistry-tools/mcp/forbidden")
    async def airegistry_tools_mcp_forbidden():
        return JSONResponse(status_code=403, content={"error": "scope mismatch"})

    return app


class TestWWWAuthenticateMiddleware:
    def test_header_added_on_401_for_mcp_path(self):
        client = TestClient(_build_app())
        response = client.get("/airegistry-tools/mcp")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            f'Bearer realm="mcp", resource_metadata="{RESOURCE_METADATA_URL}"'
        )

    def test_header_not_added_on_200(self):
        client = TestClient(_build_app())
        response = client.get("/airegistry-tools/mcp/ok")

        assert response.status_code == 200
        assert "www-authenticate" not in {k.lower() for k in response.headers.keys()}

    def test_header_added_on_oauth_token_401(self):
        """Phase-1 routes don't exist yet, but the middleware regex includes them
        so #991's token proxy lights up automatically when it ships."""
        client = TestClient(_build_app())
        response = client.get("/oauth/token")

        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]

    def test_header_added_on_oauth_authorize_401(self):
        client = TestClient(_build_app())
        response = client.get("/oauth/authorize")

        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]

    def test_header_not_added_on_unrelated_401(self):
        """401 from /api/auth/me is a session-cookie concern, not MCP discovery."""
        client = TestClient(_build_app())
        response = client.get("/api/auth/me")

        assert response.status_code == 401
        assert "www-authenticate" not in {k.lower() for k in response.headers.keys()}

    def test_header_not_added_on_403(self):
        """403 (forbidden) is not the discovery trigger; only 401 is."""
        client = TestClient(_build_app())
        response = client.get("/airegistry-tools/mcp/forbidden")

        assert response.status_code == 403
        assert "www-authenticate" not in {k.lower() for k in response.headers.keys()}

    def test_resource_metadata_url_byte_for_byte(self):
        """The header's resource_metadata must equal exactly the URL passed at init."""
        client = TestClient(_build_app())
        response = client.get("/airegistry-tools/mcp")

        # The full embedded URL must round-trip without any normalization
        header = response.headers["www-authenticate"]
        embedded = header.split('resource_metadata="', 1)[1].rsplit('"', 1)[0]
        assert embedded == RESOURCE_METADATA_URL


class TestHeaderReplacesUpstreamValue:
    """ecosystem-bridge 5.1: a 401 that already carries the auth server's bare
    `WWW-Authenticate: Bearer` must end with exactly ONE header — this value.
    Appending produced two headers and clients that read the first lost the
    resource_metadata pointer (the discovery dead end this middleware exists
    to prevent)."""

    def _app_with_upstream_header(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(
            WWWAuthenticateMiddleware,
            resource_metadata_url=RESOURCE_METADATA_URL,
        )

        @app.get("/airegistry-tools/mcp")
        async def upstream_401():
            return JSONResponse(
                status_code=401,
                content={"error": "auth required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return app

    def test_single_header_survives_upstream_bare_bearer(self):
        client = TestClient(self._app_with_upstream_header())
        response = client.get("/airegistry-tools/mcp")
        values = response.headers.get_list("www-authenticate")
        assert len(values) == 1, f"expected one header, got {values!r}"
        assert values[0] == (
            f'Bearer realm="mcp", resource_metadata="{RESOURCE_METADATA_URL}"'
        )
