"""Unit tests for the anonymous root-path split (registry-product-home).

Contract (specs/registry-product-home):

- Anonymous GET / returns 200 landing-page HTML with no login redirect.
- Authenticated GET / (valid session cookie present) returns the console SPA.
- Console deep links (/login) and API paths keep their existing behavior —
  the split applies only to the anonymous root request.
"""

import importlib
import shutil
import sys

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

# The root-path split is registered only when ``frontend/build`` exists at
# import time (registry/main.py guards the whole block with
# ``if FRONTEND_BUILD_PATH.exists()``). The build directory is produced by the
# image build and is NOT committed, so a plain source checkout (and the CI test
# job, which does not build the frontend) sees no "/" route at all -> 404.
# This fixture materialises a minimal build skeleton and re-imports
# ``registry.main`` so the contract under test is actually registered.
_CONSOLE_STUB = (
    '<!doctype html><html><head>'
    '<script src="/static/js/main.stub.js"></script>'
    "</head><body><div id=\"root\"></div></body></html>"
)


@pytest.fixture(scope="module", autouse=True)
def _frontend_build_skeleton(tmp_path_factory):
    """Create a stub frontend build and re-import the app with it in place."""
    from registry import main as _main

    build = _main.FRONTEND_BUILD_PATH
    created: list = []
    try:
        if not build.exists():
            (build / "static" / "js").mkdir(parents=True, exist_ok=True)
            (build / "index.html").write_text(_CONSOLE_STUB)
            (build / "static" / "js" / "main.stub.js").write_text("// stub\n")
            created.append(build)
        # The route table is built at import time; reload so the guarded block
        # runs against the skeleton that now exists.
        importlib.reload(_main)
        yield
    finally:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
        # Leave the module in a sane state for other test modules.
        sys.modules.pop("registry.main", None)


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
