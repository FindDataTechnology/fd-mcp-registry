"""Unit tests for the anonymous root-path split (registry-product-home).

Contract (specs/registry-product-home):

- Anonymous GET / returns 200 landing-page HTML with no login redirect.
- Authenticated GET / (valid session cookie present) returns the console SPA.
- Console deep links (/login) and API paths keep their existing behavior —
  the split applies only to the anonymous root request.
"""

import pytest
from fastapi.testclient import TestClient

from registry.core.config import settings

pytestmark = pytest.mark.unit

_LANDING_MARKERS = (
    "landing-hero",
    "landing-steps",
    "landing-market",
    "landing-dev",
)


def _client() -> TestClient:
    from registry.main import app

    return TestClient(app)


class TestAnonymousRoot:
    def test_anonymous_root_returns_200_landing_without_redirect(self):
        resp = _client().get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert resp.headers.get("location") is None
        for marker in _LANDING_MARKERS:
            assert marker in resp.text, f"landing section anchor missing: {marker}"

    def test_landing_is_chinese_copy(self):
        resp = _client().get("/")
        assert "壹座·Packs" in resp.text


class TestRootCaching:
    """Both / variants share one URL, so caches MUST be told they differ.

    Regression guard: without Vary: Cookie, a browser that cached the console
    shell while logged in replays it after logout — the shell then 401s and
    bounces to /login, which reads as "the landing page vanished".
    """

    def test_landing_declares_vary_cookie(self):
        resp = _client().get("/")
        assert "cookie" in resp.headers.get("vary", "").lower()

    def test_console_variant_declares_vary_cookie(self):
        resp = _client().get("/", cookies={settings.session_cookie_name: "opaque-id"})
        assert "cookie" in resp.headers.get("vary", "").lower()

    def test_both_variants_revalidate(self):
        for kwargs in ({}, {"cookies": {settings.session_cookie_name: "opaque-id"}}):
            resp = _client().get("/", **kwargs)
            assert "no-cache" in resp.headers.get("cache-control", "")

    def test_the_two_variants_are_distinguishable(self):
        anon = _client().get("/")
        authed = _client().get("/", cookies={settings.session_cookie_name: "opaque-id"})
        assert anon.headers.get("etag") != authed.headers.get("etag") or anon.text != authed.text


class TestSessionRoot:
    def test_session_cookie_serves_console_spa(self):
        client = _client()
        resp = client.get("/", cookies={settings.session_cookie_name: "opaque-id"})
        assert resp.status_code == 200
        # Console SPA shell: references the React bundle, not the landing copy.
        assert "static/js" in resp.text or "static/" in resp.text
        assert "landing-hero" not in resp.text


class TestDeepLinksUnaffected:
    def test_login_route_still_served_to_anonymous(self):
        # /login is a console SPA route: the catch-all must still serve the
        # shell to it — the split applies only to the root path.
        resp = _client().get("/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "landing-hero" not in resp.text

    def test_api_paths_unaffected(self):
        resp = _client().get("/api/stats", follow_redirects=False)
        # Anonymous API access keeps its existing 401 semantics.
        assert resp.status_code == 401


class TestLandingOverride:
    def test_override_dir_beats_baseline(self, tmp_path, monkeypatch):
        override = tmp_path / "index.html"
        override.write_text("<html>override-landing</html>")
        monkeypatch.setattr("registry.main._LANDING_OVERRIDE_DIR", tmp_path)
        resp = _client().get("/")
        assert "override-landing" in resp.text
