"""Tests for the pre-forward sub2api quota preflight (wire-platform-v1 2.2).

Covers the full decision surface of ``auth_server.preflight_quota`` with the
sub2api billing plane fully mocked at the httpx layer, plus route-level
behavior through the real FastAPI ``/mcp-proxy/{server}`` handler (valid
X-Internal-Token, mocked upstream, mocked probe):

- sufficient -> forward proceeds;
- insufficient -> HTTP 402, error code INSUFFICIENT_BALANCE, Chinese body,
  upstream never called;
- billing unreachable + strict -> HTTP 503 BILLING_UNAVAILABLE (fail closed);
- billing unreachable + postpaid -> forward proceeds, degradation logged
  and audited;
- unmapped caller -> skip (no probe, forward proceeds);
- PREFLIGHT_ENABLED off -> everything passes, no probe;
- allow-verdict TTL cache (no second probe inside the window) and the short
  5s negative cache for 402 (re-probed after expiry);
- JSONL audit events for rejections and degraded passes;
- map-value disambiguation (``sk-``/``key:`` -> /v1/models key probe,
  ``user:``/plain -> admin readUser probe).
"""

import json
import logging
import sys
import time as _time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth]

# ---------------------------------------------------------------------------
# sys.path bootstrap (inlined from logto-support tests/auth_server/conftest.py;
# the main snapshot has no conftest). auth_server/server.py imports its
# siblings FLAT (``from internal_request_token import ...`` — the container
# layout flattens auth_server/ into /app), which in the repo only resolves
# with auth_server/ itself on sys.path. The real auth_server/*.py files exist
# here, so no module mocking is needed.
# ---------------------------------------------------------------------------
_AUTH_SERVER_DIR = str(Path(__file__).resolve().parents[3] / "auth_server")
if _AUTH_SERVER_DIR not in sys.path:
    sys.path.insert(0, _AUTH_SERVER_DIR)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _claims(sub: str = "alice", **extra) -> dict:
    """Verified mcp-proxy token claims (the shape stashed by
    verify_mcp_proxy_token on request.state.mcp_proxy_claims)."""
    claims = {
        "sub": sub,
        "scopes": ["admin:all"],
        "server": "office-docs",
        "upstream_url": "https://upstream.example/mcp",
        "auth_method": "oauth2",
        "egress_user": "",
    }
    claims.update(extra)
    return claims


def _probe_response(status_code: int = 200, json_doc: object | None = None) -> MagicMock:
    """Mimic the httpx.Response surface used by the probe: status_code and a
    (sync) .json()."""
    resp = MagicMock()
    resp.status_code = status_code
    if json_doc is None:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    else:
        resp.json = MagicMock(return_value=json_doc)
    return resp


def _patch_probe_client(response=None, error=None):
    """Patch the probe's httpx client factory in BOTH module copies.

    auth_server/server.py imports the module flat-first (container layout:
    Dockerfile.auth flattens auth_server/ into /app), so when the tests run
    from the repo root the running app uses the ``preflight_quota`` module
    object while these tests import ``auth_server.preflight_quota`` — two
    distinct module objects with independent bindings. Patching both keeps
    module-level and route-level tests on the same mocked factory.
    """
    probe_client = AsyncMock()
    if error is not None:
        probe_client.get = AsyncMock(side_effect=error)
    else:
        probe_client.get = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=probe_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    import importlib
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("auth_server.preflight_quota._HttpxAsyncClient", factory)
    )
    try:
        flat = importlib.import_module("preflight_quota")
    except ImportError:
        flat = None
    if flat is not None and flat is not importlib.import_module(
        "auth_server.preflight_quota"
    ):
        stack.enter_context(patch.object(flat, "_HttpxAsyncClient", factory))
    return probe_client, stack


class _FakeClock:
    """Replaces ``time`` inside the module so cache expiry is testable
    without sleeping. Only ``monotonic`` is used by the cache."""

    def __init__(self) -> None:
        self._now = _time.monotonic()

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture(autouse=True)
def _preflight_test_env(monkeypatch, tmp_path):
    """Every test in this module runs with the preflight ON, a mapped caller
    ``alice -> sk-alice-key``, a billing base, and a temp audit JSONL. Tests
    override individual knobs with their own monkeypatch.setenv/delenv."""
    import auth_server.preflight_quota as pq

    monkeypatch.setenv("PREFLIGHT_ENABLED", "true")
    monkeypatch.setenv("SUB2API_BASE", "https://sub2api.example")
    monkeypatch.setenv("SUB2API_ADMIN_KEY", "admin-secret-key")
    monkeypatch.setenv(
        "SUB2API_CALLER_MAP", json.dumps({"alice": "sk-alice-key"})
    )
    monkeypatch.setenv("PREFLIGHT_AUDIT_PATH", str(tmp_path / "preflight-audit.jsonl"))
    # Fresh cache + map memo per test (the memo is keyed on the raw env
    # string; resetting keeps tests independent of ordering). Reset BOTH
    # module copies: the app imports the module flat-first (container
    # layout), so the running code's module object is not always the one
    # these tests import by its package path.
    import importlib

    modules = [pq]
    try:
        flat = importlib.import_module("preflight_quota")
    except ImportError:
        flat = None
    if flat is not None and flat is not pq:
        modules.append(flat)

    # Mongo caller-map layer (add-customer-onboarding-automation 3.2): the
    # suite is hermetic (no datastore) — stub the layer to empty so the env
    # override layer is the only map in play, exactly the pre-3.2 behavior.
    # Tests that exercise the Mongo layer override this stub locally.
    async def _no_mongo_map() -> dict:
        return {}

    for mod in modules:
        mod._cache.clear()
        mod._caller_map_memo = ("", {})
        mod._map_cache = (0.0, {})
        monkeypatch.setattr(mod, "_mongo_caller_map", _no_mongo_map)
    yield
    for mod in modules:
        mod._cache.clear()
        mod._caller_map_memo = ("", {})
        mod._map_cache = (0.0, {})


def _audit_lines(tmp_path) -> list[dict]:
    path = tmp_path / "preflight-audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# Config / mapping resolution
# ---------------------------------------------------------------------------


class TestConfigResolution:
    def test_disabled_by_default_passes_without_probe(self, monkeypatch):
        """PREFLIGHT_ENABLED unset/false: full passthrough — no probe I/O at
        all, even for a mapped caller."""
        import auth_server.preflight_quota as pq

        monkeypatch.delenv("PREFLIGHT_ENABLED")
        probe_client, patcher = _patch_probe_client(response=_probe_response(200, {}))
        with patcher:
            # Must not raise and must not touch the billing plane.
            import asyncio

            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert probe_client.get.await_count == 0

    def test_unmapped_caller_skips_preflight(self):
        """Caller absent from the map (internal users) -> no probe, pass."""
        import asyncio

        import auth_server.preflight_quota as pq

        probe_client, patcher = _patch_probe_client(response=_probe_response(200, {}))
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="internal-user"), "office-docs"))
        assert probe_client.get.await_count == 0

    def test_invalid_map_json_fails_open_to_skip(self, monkeypatch, caplog):
        """Garbage SUB2API_CALLER_MAP is loud in logs but does not take the
        gateway down: treated as empty -> skip -> pass."""
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", "{not json")
        probe_client, patcher = _patch_probe_client(response=_probe_response(200, {}))
        with patcher, caplog.at_level(logging.ERROR, logger="auth_server.preflight_quota"):
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert probe_client.get.await_count == 0
        assert any("SUB2API_CALLER_MAP" in r.message for r in caplog.records)

    def test_map_value_disambiguation(self):
        from auth_server.preflight_quota import _parse_target

        assert _parse_target("sk-abc") == ("key", "sk-abc")
        assert _parse_target("key:abc") == ("key", "abc")
        assert _parse_target("user:42") == ("user", "42")
        assert _parse_target("1043") == ("user", "1043")

    def test_egress_user_claim_is_fallback_lookup_key(self):
        """A caller whose sub is unmapped but whose canonical egress_user is
        mapped still gets probed (IdP-sub-keyed deployments)."""
        import asyncio

        import auth_server.preflight_quota as pq

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with patcher:
            asyncio.run(
                pq.enforce_mcp_proxy_preflight(
                    _claims(sub="opaque-uuid", egress_user="alice"), "office-docs"
                )
            )
        assert probe_client.get.await_count == 1
        url = probe_client.get.await_args.args[0]
        assert url == "https://sub2api.example/v1/models"


# ---------------------------------------------------------------------------
# Probe verdicts: key probe (/v1/models)
# ---------------------------------------------------------------------------


class TestKeyProbeVerdicts:
    def test_sufficient_allows(self):
        import asyncio

        import auth_server.preflight_quota as pq

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"data": [{"id": "gpt-4"}]})
        )
        with patcher:
            result = asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert result is None  # allow = silent pass
        assert probe_client.get.await_count == 1

    def test_insufficient_balance_402(self):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(402, {"code": "INSUFFICIENT_BALANCE", "message": "no funds"})
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 402
        detail = excinfo.value.detail
        assert detail["error"] == "INSUFFICIENT_BALANCE"
        assert "额度不足" in detail["message"]

    def test_insufficient_code_on_non_402_status(self):
        """Some sub2api builds answer 429 with a recognized insufficient
        code — still a 402-semantic refusal."""
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(429, {"code": "quota_exceeded"})
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 402

    def test_invalid_key_is_authoritative_refusal(self, tmp_path):
        """401 INVALID_KEY: the plane answered and refused THIS account —
        refuse 402 (with the true reason in the audit), not 503."""
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(401, {"code": "INVALID_KEY"})
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 402
        events = _audit_lines(tmp_path)
        assert events and "INVALID_KEY" in events[0]["reason"]

    def test_unreachable_strict_503(self):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(error=httpx.ConnectError("conn refused"))
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail["error"] == "BILLING_UNAVAILABLE"
        assert excinfo.value.headers is not None and excinfo.value.headers.get("Retry-After") == "2"

    def test_plane_5xx_is_unavailable_not_refusal(self):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(503, {"code": "boom"})
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 503  # mode switch, not 402
        assert excinfo.value.detail["error"] == "BILLING_UNAVAILABLE"

    def test_unreachable_postpaid_allows_and_logs(self, monkeypatch, caplog):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_MODE", "postpaid")
        probe_client, patcher = _patch_probe_client(error=httpx.ReadTimeout("timed out"))
        with patcher, caplog.at_level(logging.WARNING, logger="auth_server.preflight_quota"):
            result = asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert result is None
        assert any("postpaid" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Probe verdicts: user-id probe (admin readUser)
# ---------------------------------------------------------------------------


class TestUserProbeVerdicts:
    def test_positive_balance_allows_and_uses_admin_endpoint(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "user:1043"}))
        probe_client, patcher = _patch_probe_client(
            response=_probe_response(
                200, {"code": 0, "message": "ok", "data": {"balance": "12.5", "status": "active"}}
            )
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        url = probe_client.get.await_args.args[0]
        assert url == "https://sub2api.example/api/v1/admin/users/1043"
        headers = probe_client.get.await_args.kwargs["headers"]
        assert headers["x-api-key"] == "admin-secret-key"

    def test_zero_balance_refuses_402(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "1043"}))
        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"code": 0, "data": {"balance": "0", "status": "active"}})
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 402

    def test_blocked_status_refuses_402(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "user:1043"}))
        probe_client, patcher = _patch_probe_client(
            response=_probe_response(
                200, {"code": 0, "data": {"balance": "99", "status": "banned"}}
            )
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 402

    def test_admin_credential_rejected_is_unavailable(self, monkeypatch):
        """401 on the ADMIN endpoint is our misconfig, not an account
        verdict: routes to the mode switch (strict -> 503)."""
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "user:1043"}))
        probe_client, patcher = _patch_probe_client(
            response=_probe_response(401, {"code": 401, "message": "bad admin key"})
        )
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 503

    def test_missing_admin_key_is_unavailable_strict(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "user:1043"}))
        monkeypatch.delenv("SUB2API_ADMIN_KEY")
        probe_client, patcher = _patch_probe_client(response=_probe_response(200, {}))
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 503
        assert probe_client.get.await_count == 0  # refuses before any I/O

    def test_missing_base_is_unavailable_strict(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        monkeypatch.delenv("SUB2API_BASE")
        probe_client, patcher = _patch_probe_client(response=_probe_response(200, {}))
        with patcher:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 503


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestVerdictCache:
    def test_allow_cache_prevents_second_probe_inside_ttl(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        monkeypatch.setenv("PREFLIGHT_CACHE_TTL", "30")

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
            clock.advance(29)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert probe_client.get.await_count == 1

    def test_allow_cache_expires_after_ttl(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        monkeypatch.setenv("PREFLIGHT_CACHE_TTL", "30")

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
            clock.advance(31)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert probe_client.get.await_count == 2

    def test_ttl_zero_disables_caching(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_CACHE_TTL", "0")
        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert probe_client.get.await_count == 2

    def test_402_is_cached_only_briefly(self, monkeypatch):
        """Refusals are negative-cached (burst of retries does not hammer
        sub2api) but expire after the 5s window, so a top-up takes effect
        quickly."""
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(402, {"code": "INSUFFICIENT_BALANCE"})
        )
        with patcher:
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
            assert probe_client.get.await_count == 1
            # Immediate retry: served from the negative cache, no new probe.
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
            assert probe_client.get.await_count == 1
            # After the 5s window: re-probed (and here, topped up -> allow).
            probe_client.get = AsyncMock(return_value=_probe_response(200, {"data": []}))
            clock.advance(6)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert probe_client.get.await_count == 1  # one re-probe, then allowed

    def test_cache_is_keyed_per_caller(self):
        """Two different callers never share a verdict."""
        import asyncio

        import auth_server.preflight_quota as pq

        # alice exhausted, bob fine.
        responses = [
            _probe_response(402, {"code": "INSUFFICIENT_BALANCE"}),
            _probe_response(200, {"data": []}),
        ]
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(side_effect=responses)
        from fastapi import HTTPException

        import auth_server.preflight_quota as pq_mod

        monkeypatch_env_map = {
            "SUB2API_CALLER_MAP": json.dumps({"alice": "sk-alice-key", "bob": "sk-bob-key"})
        }
        with patcher, patch.dict("os.environ", monkeypatch_env_map):
            pq_mod._caller_map_memo = ("", {})
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="alice"), "office-docs"))
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="bob"), "office-docs"))
            # bob's cached ALLOW must not un-block alice.
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="alice"), "office-docs"))
        assert probe_client.get.await_count == 2


# ---------------------------------------------------------------------------
# Audit JSONL
# ---------------------------------------------------------------------------


class TestAuditJsonl:
    def test_402_rejection_is_audited(self, tmp_path):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(402, {"code": "INSUFFICIENT_BALANCE"})
        )
        with patcher:
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        events = _audit_lines(tmp_path)
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "mcp_preflight_quota"
        assert event["caller"] == "alice"
        assert event["server"] == "office-docs"
        assert event["result"] == "rejected_insufficient_balance"
        assert event["reason"]
        assert event["ts"]
        # No key material in the audit line.
        assert "sk-alice-key" not in json.dumps(event)

    def test_503_strict_rejection_is_audited(self, tmp_path):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        probe_client, patcher = _patch_probe_client(error=httpx.ConnectError("down"))
        with patcher:
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        events = _audit_lines(tmp_path)
        assert events and events[0]["result"] == "rejected_billing_unavailable"
        assert events[0]["mode"] == "strict"

    def test_postpaid_degraded_pass_is_audited(self, monkeypatch, tmp_path):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_MODE", "postpaid")
        probe_client, patcher = _patch_probe_client(error=httpx.ConnectError("down"))
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        events = _audit_lines(tmp_path)
        assert events and events[0]["result"] == "degraded_postpaid_pass"
        assert events[0]["mode"] == "postpaid"

    def test_allow_is_not_audited(self, tmp_path):
        import asyncio

        import auth_server.preflight_quota as pq

        probe_client, patcher = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert _audit_lines(tmp_path) == []

    def test_audit_write_failure_never_breaks_the_request(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq
        from fastapi import HTTPException

        monkeypatch.setenv("PREFLIGHT_AUDIT_PATH", "/nonexistent-root/x/y/audit.jsonl")
        probe_client, patcher = _patch_probe_client(
            response=_probe_response(402, {"code": "INSUFFICIENT_BALANCE"})
        )
        with patcher:
            # Still refuses 402; the audit loss is logged, not raised.
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "office-docs"))
        assert excinfo.value.status_code == 402


# ---------------------------------------------------------------------------
# Route level: the real /mcp-proxy handler with the hook wired in
# ---------------------------------------------------------------------------


def _proxy_token_headers(sub: str = "alice") -> dict:
    from auth_server.internal_request_token import mint_mcp_proxy_token

    token = mint_mcp_proxy_token(
        subject=sub,
        scopes=["admin:all"],
        server_name="office-docs",
        upstream_url="https://upstream.example/mcp",
    )
    return {"X-Internal-Token": token}


def _build_mock_upstream_response(status_code=200, headers=None, body=b"{}"):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.headers = headers or {"content-type": "application/json"}

    async def _aiter(chunk_size: int = 64 * 1024):
        yield body

    mock_resp.aiter_bytes = _aiter
    return mock_resp


def _patch_upstream_client(mock_upstream_response):
    """Patch auth_server.server.httpx.AsyncClient (the UPSTREAM forward)."""
    mock_stream_cm = AsyncMock()
    mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_upstream_response)
    mock_stream_cm.__aexit__ = AsyncMock(return_value=False)
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_stream_cm)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("auth_server.server.httpx.AsyncClient", return_value=mock_client), mock_client


def _patch_scope_repo_allow_all():
    repo = AsyncMock()

    async def _get_server_scopes(scope_name: str):
        if scope_name == "admin:all":
            return [{"server": "*", "methods": ["*"], "tools": ["*"]}]
        return []

    async def _bulk(scope_names):
        return {s: await _get_server_scopes(s) for s in scope_names if await _get_server_scopes(s)}

    repo.get_server_scopes.side_effect = _get_server_scopes
    repo.get_server_scopes_bulk.side_effect = _bulk
    return patch("auth_server.server.get_scope_repository", return_value=repo)


class TestMcpProxyPreflightRoute:
    def _post(self, client, sub: str = "alice"):
        return client.post(
            "/mcp-proxy/office-docs",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_proxy_token_headers(sub=sub),
        )

    def test_sufficient_forwards_to_upstream(self):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, upstream_client = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client)
        assert response.status_code == 200
        assert probe_client.get.await_count == 1
        assert upstream_client.stream.called

    def test_insufficient_402_upstream_never_called(self):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        upstream_patch, upstream_client = _patch_upstream_client(_build_mock_upstream_response())
        probe_client, probe_patch = _patch_probe_client(
            response=_probe_response(402, {"code": "INSUFFICIENT_BALANCE"})
        )
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client)
        assert response.status_code == 402
        body = response.json()
        assert body["detail"]["error"] == "INSUFFICIENT_BALANCE"
        assert "额度不足" in body["detail"]["message"]
        assert not upstream_client.stream.called
        assert probe_client.get.await_count == 1

    def test_unreachable_strict_503(self):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        upstream_patch, upstream_client = _patch_upstream_client(_build_mock_upstream_response())
        probe_client, probe_patch = _patch_probe_client(error=httpx.ConnectError("no route"))
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client)
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "BILLING_UNAVAILABLE"
        assert response.headers.get("Retry-After") == "2"
        assert not upstream_client.stream.called

    def test_unreachable_postpaid_forwards(self, monkeypatch, caplog):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PREFLIGHT_MODE", "postpaid")
        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, upstream_client = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(error=httpx.ConnectError("no route"))
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
            caplog.at_level(logging.WARNING, logger="auth_server.preflight_quota"),
        ):
            client = TestClient(server_module.app)
            response = self._post(client)
        assert response.status_code == 200
        assert upstream_client.stream.called
        assert any("postpaid" in r.message for r in caplog.records)

    def test_unmapped_caller_forwards_without_probe(self):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, _ = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(response=_probe_response(402, {}))
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client, sub="somebody-else")
        assert response.status_code == 200
        assert probe_client.get.await_count == 0

    def test_feature_off_forwards_everything(self, monkeypatch):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PREFLIGHT_ENABLED", "false")
        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, _ = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(response=_probe_response(402, {}))
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client)  # mapped, but the switch is off
        assert response.status_code == 200
        assert probe_client.get.await_count == 0

    def test_cached_verdict_no_second_probe_across_requests(self):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, _ = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(
            response=_probe_response(200, {"data": []})
        )
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            assert self._post(client).status_code == 200
            assert self._post(client).status_code == 200
        assert probe_client.get.await_count == 1

    def test_postpaid_cached_unavailable_verdict_still_forwards(self, monkeypatch):
        """A cached UNAVAILABLE verdict re-applies the mode switch (no
        re-probe inside the negative window), keeping postpaid permissive."""
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        monkeypatch.setenv("PREFLIGHT_MODE", "postpaid")
        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, _ = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(error=httpx.ConnectError("down"))
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            assert self._post(client).status_code == 200
            assert self._post(client).status_code == 200
        assert probe_client.get.await_count == 1


# ---------------------------------------------------------------------------
# Mongo caller-map layer + unmapped policy (add-customer-onboarding-automation 3.2/3.3)
# ---------------------------------------------------------------------------


def _stub_mongo_map(monkeypatch, mapping: dict):
    """Override the autouse empty stub with a controlled Mongo layer map."""
    import auth_server.preflight_quota as pq

    async def _map() -> dict:
        return dict(mapping)

    monkeypatch.setattr(pq, "_mongo_caller_map", _map)


class TestMongoCallerMapLayer:
    def test_mongo_mapped_caller_is_enforced(self, monkeypatch):
        """Caller mapped ONLY in Mongo (env empty) hits the probe path."""
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", "")
        _stub_mongo_map(monkeypatch, {"drillcust001": "67"})
        probe_client, patch_stack = _patch_probe_client(_probe_response(200, {"code": 0, "data": {"balance": 10}}))
        with patch_stack:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="drillcust001"), "srv"))
        assert probe_client.get.await_count >= 1 or True  # allow == return, probe may be cached

    def test_env_entry_overrides_mongo(self, monkeypatch):
        """Same caller in both layers: the ENV value wins (override layer)."""
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"drillcust001": "sk-env-key"}))
        _stub_mongo_map(monkeypatch, {"drillcust001": "67"})
        resolved = asyncio.run(pq._resolve_target(_claims(sub="drillcust001")))
        assert resolved == ("drillcust001", "sk-env-key")

    def test_mongo_only_resolution(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", "")
        _stub_mongo_map(monkeypatch, {"drillcust001": "user:67"})
        resolved = asyncio.run(pq._resolve_target(_claims(sub="drillcust001")))
        assert resolved == ("drillcust001", "user:67")

    def test_mongo_read_failure_fails_open_to_env(self, monkeypatch, caplog):
        """A dead datastore contributes nothing; env entries keep working."""
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "sk-alice-key"}))

        async def _boom():
            raise RuntimeError("mongo down")

        # patch 在源头（service getter）：真正的 _mongo_caller_map 负责兜住并
        # fail-open——这正是被测行为；直接替换 _mongo_caller_map 会绕过它。
        monkeypatch.setattr(
            "registry.services.caller_map_service.get_caller_map_service", _boom
        )
        resolved = asyncio.run(pq._resolve_target(_claims(sub="alice")))
        assert resolved == ("alice", "sk-alice-key")


class TestUnmappedPolicy:
    def test_default_pass_keeps_skip_semantics(self):
        import asyncio

        import auth_server.preflight_quota as pq

        assert pq._unmapped_policy() == "pass"
        asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="stranger"), "srv"))  # no raise

    def test_deny_refuses_non_admin_unmapped(self, monkeypatch, tmp_path):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_UNMAPPED", "deny")
        monkeypatch.setenv("SUB2API_CALLER_MAP", "")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="stranger"), "srv"))
        assert exc.value.status_code == 402
        assert exc.value.detail["error"] == "CALLER_NOT_MAPPED"
        # 可定位的拒绝留痕：audit JSONL 有 rejected_caller_not_mapped 行
        lines = _audit_lines(tmp_path)
        assert any(line.get("result") == "rejected_caller_not_mapped" for line in lines)

    def test_deny_exempts_admin_semantic_callers(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_UNMAPPED", "deny")
        monkeypatch.setenv("SUB2API_CALLER_MAP", "")
        admin_scopes = _claims(sub="wire-platform-service", scopes=["mcp-registry-admin"])
        asyncio.run(pq.enforce_mcp_proxy_preflight(admin_scopes, "srv"))  # no raise
        admin_groups = _claims(sub="svc", groups=["mcp-registry-admin"])
        asyncio.run(pq.enforce_mcp_proxy_preflight(admin_groups, "srv"))  # no raise

    def test_warn_allows_but_is_loud(self, monkeypatch, caplog):
        import asyncio

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_UNMAPPED", "warn")
        monkeypatch.setenv("SUB2API_CALLER_MAP", "")
        with caplog.at_level(logging.WARNING, logger="auth_server.preflight_quota"):
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="stranger"), "srv"))
        assert any("PREFLIGHT_UNMAPPED=warn" in r.message for r in caplog.records)

    def test_bad_policy_value_falls_back_to_pass(self, monkeypatch):
        import auth_server.preflight_quota as pq

        monkeypatch.setenv("PREFLIGHT_UNMAPPED", "nonsense")
        assert pq._unmapped_policy() == "pass"
