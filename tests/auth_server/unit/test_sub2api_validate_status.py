"""The sk- branch's refusal status must reach the client intact.

``HTTPException`` is an ``Exception``, so the provider-validation wrapper's
generic handler turned a rejected key (401/402) and the retryable billing 503
into a 500. nginx relays only 401/403 from the auth subrequest and answers
everything else with its own 500 page, so a customer with an over-quota key
and a customer hitting a broken gateway saw the same thing.
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-definitely-long-enough-32b")

# Flat-first, matching the import inside /validate. Patching the PACKAGED module
# would patch a different object than the server resolves, so the real resolver
# would run (and fail closed as "plane unavailable") instead of the stub.
try:
    import sub2api_key_auth
except ImportError:
    from auth_server import sub2api_key_auth

from auth_server import server  # noqa: E402
from auth_server.server import app  # noqa: E402

pytestmark = pytest.mark.unit


def _validate(client, key="sk-rejected"):
    return client.get(
        "/validate",
        headers={
            "Authorization": f"Bearer {key}",
            "X-Original-URL": "http://gateway/virtual/sentiment/mcp",
        },
    )


@pytest.mark.parametrize(
    ("exc", "status", "marker"),
    [
        (sub2api_key_auth.Sub2ApiKeyRejected("insufficient balance", status=402), 403, "402"),
        # A retryable plane failure must NOT look like a bad key: nginx cannot
        # relay a 503 from auth_request, so it rides the marker too.
        (sub2api_key_auth.Sub2ApiUnavailable("plane down"), 403, "503"),
        (sub2api_key_auth.Sub2ApiKeyRejected("invalid key"), 401, None),
    ],
)
def test_sk_refusal_reaches_the_client(monkeypatch, exc, status, marker):
    async def _raise(key):
        raise exc

    monkeypatch.setattr(sub2api_key_auth, "resolve_sk_caller", _raise)
    r = _validate(TestClient(app))
    assert r.status_code == status, r.text
    if marker is None:
        assert "x-auth-refusal" not in r.headers
    else:
        assert r.headers["x-auth-refusal"] == marker


async def _allow(*a, **k):
    return True


def test_caller_scopes_are_per_holder(monkeypatch):
    """A per-customer line scope reaches only that holder, plus the global list."""

    async def _holder(key):
        return {"sub2api_user_id": "72", "email": "a@b.c"}

    monkeypatch.setattr(sub2api_key_auth, "resolve_sk_caller", _holder)
    monkeypatch.setattr(server, "SUB2API_KEY_SCOPES", ["mcp-sentiment-execute"])
    monkeypatch.setattr(
        server, "SUB2API_KEY_CALLER_SCOPES", {"72": ["mcp-wire-execute"]}
    )
    # The registry-scope lookup needs Mongo; this test is about the scope LIST
    # built here, not about registry authorization.
    monkeypatch.setattr(server, "validate_server_tool_access", _allow)
    r = _validate(TestClient(app))
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["mcp-sentiment-execute", "mcp-wire-execute"]


def test_other_holder_does_not_get_another_callers_scope(monkeypatch):
    """The seam's whole point: a different sk- key must not inherit it."""

    async def _holder(key):
        return {"sub2api_user_id": "999", "email": "x@y.z"}

    monkeypatch.setattr(sub2api_key_auth, "resolve_sk_caller", _holder)
    monkeypatch.setattr(server, "SUB2API_KEY_SCOPES", ["mcp-sentiment-execute"])
    monkeypatch.setattr(
        server, "SUB2API_KEY_CALLER_SCOPES", {"72": ["mcp-wire-execute"]}
    )
    monkeypatch.setattr(server, "validate_server_tool_access", _allow)
    r = _validate(TestClient(app))
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["mcp-sentiment-execute"]


def test_malformed_caller_map_grants_nobody():
    assert server._parse_caller_scopes("{not json") == {}
    assert server._parse_caller_scopes('["72"]') == {}
    assert server._parse_caller_scopes('{"72": "a b", "73": []}') == {"72": ["a", "b"]}
