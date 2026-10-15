"""Unit tests for the Logto branch of the auth-server logout URL builder.

Live-observed defect (2026-10-15, registry-product-home): Logto hit the
Cognito-shaped ``else`` branch, so the browser was sent to
``/oidc/session/end?...logout_uri=...`` — Logto ignores ``logout_uri`` (it is
not a Cognito host) and lands the user on its generic
``/oidc/session/end/success`` page with no way back to the registry. The fix
adds a Logto branch speaking standard OIDC RP-initiated logout
(``post_logout_redirect_uri`` + ``id_token_hint``).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth_server.server as auth_server


@pytest.fixture()
def logto_client(monkeypatch):
    """Minimal app binding the real oauth2_logout with a Logto provider set."""
    providers = {
        "logto": {
            "client_id": "logto-app",
            "client_secret": "secret",
            "authorize_url": "https://auth.example.com/oidc/auth",
            "token_url": "https://auth.example.com/oidc/token",
            "user_info_url": "https://auth.example.com/oidc/me",
            "logout_url": "https://auth.example.com/oidc/session/end",
        }
    }
    monkeypatch.setattr(auth_server, "OAUTH2_CONFIG", {"providers": providers})

    app = FastAPI()
    app.add_api_route(
        "/oauth2/logout/{provider}",
        auth_server.oauth2_logout,
        methods=["GET"],
    )
    return TestClient(app)


class TestLogtoLogoutBranch:
    def test_logto_uses_post_logout_redirect_uri(self, logto_client):
        resp = logto_client.get(
            "/oauth2/logout/logto",
            params={"redirect_uri": "http://testserver/logout"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert "post_logout_redirect_uri=" in loc
        assert "http%3A%2F%2Ftestserver%2Flogout" in loc
        # The Cognito-shaped parameter must NOT appear.
        assert "logout_uri=" not in loc

    def test_logto_forwards_id_token_hint(self, logto_client):
        resp = logto_client.get(
            "/oauth2/logout/logto",
            params={
                "redirect_uri": "http://testserver/logout",
                "id_token_hint": "eyJhbGciOiJSUzI1NiJ9.x.y",
            },
            follow_redirects=False,
        )
        assert "id_token_hint=" in resp.headers["location"]

    def test_logto_omits_hint_when_absent(self, logto_client):
        resp = logto_client.get(
            "/oauth2/logout/logto",
            params={"redirect_uri": "http://testserver/logout"},
            follow_redirects=False,
        )
        assert "id_token_hint" not in resp.headers["location"]

    def test_branch_also_matches_session_end_path(self, logto_client, monkeypatch):
        """A provider whose name doesn't say logto but whose logout_url is the
        OIDC session/end endpoint still gets standard RP-initiated params."""
        generic = {
            **auth_server.OAUTH2_CONFIG["providers"]["logto"],
            "client_id": "oidc-generic",
        }
        monkeypatch.setattr(
            auth_server,
            "OAUTH2_CONFIG",
            {"providers": {**auth_server.OAUTH2_CONFIG["providers"], "oidc-generic": generic}},
        )
        resp = logto_client.get(
            "/oauth2/logout/oidc-generic",
            params={"redirect_uri": "http://testserver/logout"},
            follow_redirects=False,
        )
        assert "post_logout_redirect_uri=" in resp.headers["location"]

    def test_cognito_shape_unchanged(self, logto_client, monkeypatch):
        """The else branch stays Cognito-shaped — no regression there."""
        monkeypatch.setattr(
            auth_server,
            "OAUTH2_CONFIG",
            {"providers": {
                **auth_server.OAUTH2_CONFIG["providers"],
                "cognito": {
                    "client_id": "cog-app",
                    "logout_url": "https://foo.auth.amazonaws.com/logout",
                },
            }},
        )
        resp = logto_client.get(
            "/oauth2/logout/cognito",
            params={"redirect_uri": "http://testserver/logout"},
            follow_redirects=False,
        )
        loc = resp.headers["location"]
        assert "logout_uri=" in loc
        assert "post_logout_redirect_uri=" not in loc
