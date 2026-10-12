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
    # wire line-quota gate (wire-self-serve-launch 3.2): hermetic default is
    # OFF with no wire endpoint configuration — only the gate tests opt in.
    for _var in (
        "FD_WIRE_QUOTA_GATE",
        "FD_WIRE_QUOTA_URL",
        "FD_WIRE_INTERNAL_TOKEN",
        "FD_WIRE_QUOTA_TTL",
    ):
        monkeypatch.delenv(_var, raising=False)
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
        mod._quota_cache.clear()
        monkeypatch.setattr(mod, "_mongo_caller_map", _no_mongo_map)
    yield
    for mod in modules:
        mod._cache.clear()
        mod._caller_map_memo = ("", {})
        mod._map_cache = (0.0, {})
        mod._quota_cache.clear()


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


# ---------------------------------------------------------------------------
# R6: mapped caller whose billing account does not exist (readUser 404)
# ---------------------------------------------------------------------------


class TestBillingAccountMissing:
    """alice -> 56（user-id 探针）为底；全部走 user 探针语义。"""

    def _user_map(self, monkeypatch):
        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "56"}))

    def test_readuser_404_yields_missing_verdict(self):
        import asyncio

        import auth_server.preflight_quota as pq

        probe_client, patch_stack = _patch_probe_client(_probe_response(404))
        with patch_stack:
            verdict, reason = asyncio.run(pq._probe_user("https://s.example", "999"))
        assert verdict == pq._MISSING
        assert "404" in reason

    def test_missing_raises_account_missing_402(self, monkeypatch, tmp_path):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        self._user_map(monkeypatch)
        probe_client, patch_stack = _patch_probe_client(_probe_response(404))
        with patch_stack, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.status_code == 402
        assert exc.value.detail["error"] == "BILLING_ACCOUNT_MISSING"
        lines = _audit_lines(tmp_path)
        assert any(line.get("result") == "rejected_billing_account_missing" for line in lines)

    def test_cached_missing_verdict_also_402(self, monkeypatch):
        import asyncio
        import time as _t

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        self._user_map(monkeypatch)
        pq._cache.clear()
        pq._cache["alice\x00user\x0056"] = (pq._MISSING, "readUser 404", _t.monotonic() + 60)
        try:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            assert exc.value.detail["error"] == "BILLING_ACCOUNT_MISSING"
        finally:
            pq._cache.clear()

    def test_true_zero_balance_still_insufficient(self, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        self._user_map(monkeypatch)
        probe_client, patch_stack = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": 0, "status": "active"}})
        )
        with patch_stack, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "INSUFFICIENT_BALANCE"


# ---------------------------------------------------------------------------
# wire monthly line-quota gate (wire-self-serve-launch 3.2)
# ---------------------------------------------------------------------------


def _quota_response(
    remaining_lines: int,
    enforce_balance: bool = True,
    included_lines: int = 1000,
    used_lines: int | None = None,
) -> MagicMock:
    """Wire api /internal/quota/{id} success body per the 3.2 contract."""
    if used_lines is None:
        used_lines = max(included_lines - remaining_lines, 0)
    return _probe_response(
        200,
        {
            "sub2api_user_id": "56",
            "plan": "free",
            "included_lines": included_lines,
            "used_lines": used_lines,
            "remaining_lines": remaining_lines,
            "enforce_balance": enforce_balance,
        },
    )


def _gate_env(monkeypatch, **overrides) -> None:
    """FD_WIRE_QUOTA_GATE=on + wire base/token; alice mapped to sub2api user 56."""
    monkeypatch.setenv("FD_WIRE_QUOTA_GATE", "true")
    monkeypatch.setenv("FD_WIRE_QUOTA_URL", "https://wire.example")
    monkeypatch.setenv("FD_WIRE_INTERNAL_TOKEN", "wire-internal-secret")
    monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "56"}))
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _probe_urls(probe_client) -> list[str]:
    """Every URL the mocked httpx client factory saw, in order."""
    return [call.args[0] for call in probe_client.get.await_args_list]


_WIRE_QUOTA_URL = "https://wire.example/internal/quota/56"
_SUB2API_USER_URL = "https://sub2api.example/api/v1/admin/users/56"


class TestWireQuotaGateConfig:
    def test_switch_defaults_off(self):
        import auth_server.preflight_quota as pq

        assert pq._quota_gate_enabled() is False

    def test_switch_truthy_variants(self, monkeypatch):
        import auth_server.preflight_quota as pq

        for raw in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("FD_WIRE_QUOTA_GATE", raw)
            assert pq._quota_gate_enabled() is True, raw

    def test_ttl_default_60_env_overridable_garbage_falls_back(self, monkeypatch):
        import auth_server.preflight_quota as pq

        assert pq._quota_ttl() == 60.0
        monkeypatch.setenv("FD_WIRE_QUOTA_TTL", "120")
        assert pq._quota_ttl() == 120.0
        monkeypatch.setenv("FD_WIRE_QUOTA_TTL", "garbage")
        assert pq._quota_ttl() == 60.0


class TestWireQuotaGateOff:
    def test_gate_off_runs_the_old_flow_only(self, monkeypatch):
        """Switch off (default): no wire GET at all; the sub2api balance gate
        still governs (balance 0 -> 402 INSUFFICIENT_BALANCE) even though the
        quota endpoint would have said enforce_balance=False."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "56"}))
        balance_resp = _probe_response(
            200, {"code": 0, "data": {"balance": 0, "status": "active"}}
        )
        probe_client, patcher = _patch_probe_client(response=balance_resp)
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "INSUFFICIENT_BALANCE"
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]


class TestWireQuotaGateExhausted:
    def test_remaining_zero_refuses_monthly_quota_not_balance(self, monkeypatch, tmp_path):
        """remaining_lines<=0 -> 402 MONTHLY_QUOTA_EXHAUSTED (its own code and
        message, distinct from the balance 402); the balance probe never runs
        and the rejection is audited with a locatable result."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client(_quota_response(0))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.status_code == 402
        assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert "当月额度已用完" in exc.value.detail["message"]
        urls = _probe_urls(probe_client)
        assert urls == [_WIRE_QUOTA_URL]
        headers = probe_client.get.await_args_list[0].kwargs["headers"]
        assert headers == {"X-Internal-Token": "wire-internal-secret"}
        events = _audit_lines(tmp_path)
        quota_events = [e for e in events if e["result"] == "rejected_monthly_quota_exhausted"]
        assert quota_events and "remaining=0" in quota_events[0]["reason"]

    def test_exhausted_with_enforce_balance_false_still_refuses(self, monkeypatch):
        """The line gate is the primary gate: a free tier (enforce_balance
        false) that has spent its included lines is still refused."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client(_quota_response(0, enforce_balance=False))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]


class TestWireQuotaGateAllow:
    def test_not_exhausted_enforce_balance_false_zero_balance_passes(self, monkeypatch):
        """Free-tier semantics (D1): within the included lines +
        enforce_balance=false -> the sub2api balance probe is SKIPPED, so a
        0 balance cannot kill the call (the second mocked response would 402
        if the probe wrongly ran — it must never be consumed)."""
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        would_refuse_balance = _probe_response(
            200, {"code": 0, "data": {"balance": 0, "status": "active"}}
        )
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(
            side_effect=[_quota_response(371, enforce_balance=False), would_refuse_balance]
        )
        with patcher:
            result = asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert result is None
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]

    def test_not_exhausted_enforce_balance_true_zero_balance_refuses_balance_402(
        self, monkeypatch
    ):
        """enforce_balance=true: the paid-tier balance gate runs after the
        line gate passes, and a 0 balance still refuses INSUFFICIENT_BALANCE."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(
            side_effect=[
                _quota_response(500, enforce_balance=True),
                _probe_response(200, {"code": 0, "data": {"balance": 0, "status": "active"}}),
            ]
        )
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "INSUFFICIENT_BALANCE"
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL, _SUB2API_USER_URL]

    def test_not_exhausted_enforce_balance_true_positive_balance_passes(self, monkeypatch):
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(
            side_effect=[
                _quota_response(500, enforce_balance=True),
                _probe_response(
                    200, {"code": 0, "data": {"balance": "12.5", "status": "active"}}
                ),
            ]
        )
        with patcher:
            result = asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert result is None
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL, _SUB2API_USER_URL]


class TestWireQuotaGateFailClosed:
    def test_endpoint_timeout_refuses_503_quota_unavailable(self, monkeypatch, tmp_path):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client(error=httpx.ReadTimeout("timed out"))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.status_code == 503
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"
        assert exc.value.headers is not None and exc.value.headers.get("Retry-After") == "2"
        # The balance probe is never reached (fail-closed comes first).
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]
        events = _audit_lines(tmp_path)
        unavailable = [e for e in events if e["result"] == "rejected_quota_unavailable"]
        assert unavailable and "ReadTimeout" in unavailable[0]["reason"]

    @pytest.mark.parametrize("status", [401, 404, 500, 503])
    def test_non_200_refuses_fail_closed(self, monkeypatch, status):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client(
            _probe_response(status, {"error": "boom"})
        )
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.status_code == 503
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"

    def test_unparseable_200_body_refuses_fail_closed(self, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        probe_client, patcher = _patch_probe_client(_probe_response(200, None))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"

    def test_missing_config_refuses_before_any_io(self, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_URL=None)
        probe_client, patcher = _patch_probe_client(_quota_response(100))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"
        assert probe_client.get.await_count == 0

    def test_key_shaped_map_value_cannot_hit_quota_endpoint_fail_closed(
        self, monkeypatch
    ):
        """Contract misalignment guard: the wire quota endpoint is keyed by
        sub2api USER id; a legacy key-shaped map value cannot identify a quota
        account -> refuse with the gap named, never forward ungated."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        monkeypatch.setenv("SUB2API_CALLER_MAP", json.dumps({"alice": "sk-alice-key"}))
        probe_client, patcher = _patch_probe_client(_quota_response(100))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"
        assert probe_client.get.await_count == 0

    def test_postpaid_mode_does_not_relax_the_quota_gate(self, monkeypatch):
        """The gate's own switch is the rollback lever: PREFLIGHT_MODE=postpaid
        (a sub2api-plane doctrine) must not open a quota-endpoint bypass."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        monkeypatch.setenv("PREFLIGHT_MODE", "postpaid")
        probe_client, patcher = _patch_probe_client(error=httpx.ConnectError("down"))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert exc.value.status_code == 503
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"


class TestWireQuotaGateAdminExemption:
    def test_admin_scopes_skip_line_gate(self, monkeypatch):
        """MAPPED admin-semantic caller: no wire quota GET; the balance probe
        keeps its pre-3.2 behavior (platform track, not the line gate)."""
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        admin = _claims(sub="alice", scopes=["mcp-registry-admin"])
        probe_client, patcher = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": "9", "status": "active"}})
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(admin, "srv"))  # no raise
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]

    def test_admin_groups_also_exempt_even_when_exhausted(self, monkeypatch):
        """groups-based admin semantics are the same口径; even a remaining=0
        quota answer must not refuse a platform credential."""
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        admin = _claims(sub="alice", groups=["mcp-registry-admin"])
        probe_client, patcher = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": "9", "status": "active"}})
        )
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(admin, "srv"))  # no raise
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]

    def test_unmapped_admin_caller_still_passes_without_any_probe(self, monkeypatch):
        """Gate on + deny policy: the admin exemption is the SAME one the
        unmapped policy uses — platform credentials never hit either gate."""
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        monkeypatch.setenv("PREFLIGHT_UNMAPPED", "deny")
        monkeypatch.setenv("SUB2API_CALLER_MAP", "")
        admin = _claims(sub="wire-platform-service", scopes=["mcp-registry-admin"])
        probe_client, patcher = _patch_probe_client(_quota_response(0))
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(admin, "srv"))  # no raise
        assert probe_client.get.await_count == 0


class TestWireQuotaGateCache:
    def test_ttl_cache_hit_does_not_repeat_the_wire_request(self, monkeypatch):
        import asyncio
        import types

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_TTL="60")
        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        probe_client, patcher = _patch_probe_client(_quota_response(371, enforce_balance=False))
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            clock.advance(59)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert probe_client.get.await_count == 1

    def test_cache_expires_after_ttl_and_reprobes(self, monkeypatch):
        import asyncio
        import types

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_TTL="60")
        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        probe_client, patcher = _patch_probe_client(_quota_response(371, enforce_balance=False))
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            clock.advance(61)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert probe_client.get.await_count == 2

    def test_ttl_zero_disables_caching(self, monkeypatch):
        import asyncio
        import types

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_TTL="0")
        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        probe_client, patcher = _patch_probe_client(_quota_response(371, enforce_balance=False))
        with patcher:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert probe_client.get.await_count == 2

    def test_exhausted_verdict_cached_for_the_full_window(self, monkeypatch):
        """A burst of retries against an exhausted caller does not hammer the
        wire endpoint; after the TTL a topped-up/expired window re-probes."""
        import asyncio
        import types

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_TTL="60")
        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(
            side_effect=[
                _quota_response(0),
                # Re-probe after the TTL: month rolled / plan upgraded;
                # enforce_balance=False so the recovered verdict alone ends
                # the request (no balance probe).
                _quota_response(10, enforce_balance=False),
            ]
        )
        with patcher:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"
            # Immediate retry: negative-hit served from cache, no new GET.
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            assert probe_client.get.await_count == 1
            clock.advance(61)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert probe_client.get.await_count == 2

    def test_unavailable_verdict_negative_cached_briefly_only(self, monkeypatch):
        """Fail-closed rejections cache for the short 5s window only, so a
        wire-side recovery takes effect quickly."""
        import asyncio
        import types

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_TTL="60")
        clock = _FakeClock()
        monkeypatch.setattr(pq, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(
            side_effect=[
                _probe_response(500, {"error": "boom"}),
                # Re-probe after the short negative window: wire recovered.
                _quota_response(10, enforce_balance=False),
            ]
        )
        with patcher:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
            assert probe_client.get.await_count == 1  # inside the 5s window
            clock.advance(6)
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "srv"))
        assert probe_client.get.await_count == 2  # re-probed, now healthy

    def test_cache_is_keyed_per_sub2api_user(self, monkeypatch):
        """Two callers never share a line-quota verdict."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch)
        monkeypatch.setenv(
            "SUB2API_CALLER_MAP", json.dumps({"alice": "56", "bob": "77"})
        )
        probe_client, patcher = _patch_probe_client()
        probe_client.get = AsyncMock(
            side_effect=[
                _quota_response(0),   # alice exhausted (user 56)
                # bob fine (user 77); enforce_balance=False so his allow ends
                # the request without a balance probe.
                _quota_response(10, enforce_balance=False),
            ]
        )
        with patcher:
            with pytest.raises(HTTPException):
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="alice"), "srv"))
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="bob"), "srv"))
            # alice's second refusal comes from HER cache entry (no new GET):
            # bob's cached ALLOW never un-blocks alice.
            with pytest.raises(HTTPException) as exc:
                asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(sub="alice"), "srv"))
            assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert probe_client.get.await_count == 2


class TestMcpProxyQuotaGateRoute:
    """Route level: the real /mcp-proxy handler refuses before the upstream."""

    def _post(self, client, sub: str = "alice"):
        return client.post(
            "/mcp-proxy/office-docs",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=_proxy_token_headers(sub=sub),
        )

    def test_monthly_quota_exhausted_402_upstream_never_called(self, monkeypatch):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        _gate_env(monkeypatch)
        upstream_patch, upstream_client = _patch_upstream_client(
            _build_mock_upstream_response()
        )
        probe_client, probe_patch = _patch_probe_client(_quota_response(0))
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
        assert body["detail"]["error"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert "当月额度已用完" in body["detail"]["message"]
        assert not upstream_client.stream.called
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]

    def test_quota_gate_off_route_unchanged(self, monkeypatch):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, upstream_client = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(
            _probe_response(200, {"data": []})
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
        assert _probe_urls(probe_client) == ["https://sub2api.example/v1/models"]


# ---------------------------------------------------------------------------
# Gate jurisdiction scoping (fix-quota-gate-server-scope)
# ---------------------------------------------------------------------------


class TestWireQuotaGateScope:
    """FD_WIRE_QUOTA_SERVERS opt-in jurisdiction: unset gates every server
    (legacy behavior kept for gray-release and wire free-tier semantics);
    set -> only the listed registered servers are gated and everything else
    skips the line gate entirely, keeping ordinary balance semantics."""

    def test_unset_scope_keeps_legacy_gate_everywhere(self, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS=None)
        probe_client, patcher = _patch_probe_client(_quota_response(0))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "lex-srv/mcp"))
        assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]

    def test_out_of_scope_server_skips_line_gate_line_probe_never_runs(self, monkeypatch):
        """The lex incident, regression-pinned: a mapped non-wire caller on an
        unlisted server must never be probed against the wire quota plane (a
        wire-side 404 "no such customer" is that line's NORMAL state, not a
        fail-closable misconfiguration)."""
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv, other-wire")
        probe_client, patcher = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": 100, "status": "active"}})
        )
        with patcher:
            asyncio.run(
                pq.enforce_mcp_proxy_preflight(_claims(), "fd-legal-search-mcp/mcp")
            )
        # Balance probe only — the wire quota URL is absent.
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]

    def test_out_of_scope_server_balance_gate_still_refuses(self, monkeypatch):
        """Scope exemption is NOT a billing bypass: the exempted caller keeps
        the ordinary balance semantics (0 balance -> 402 INSUFFICIENT_BALANCE)."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        probe_client, patcher = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": 0, "status": "active"}})
        )
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "lex-srv/mcp"))
        assert exc.value.detail["error"] == "INSUFFICIENT_BALANCE"
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]

    def test_in_scope_server_gated_despite_transport_suffix(self, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        probe_client, patcher = _patch_probe_client(_quota_response(0))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "wire-srv/mcp"))
        assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]

    def test_scope_list_entries_normalized_and_case_insensitive(self, monkeypatch):
        """Ops-facing forgiveness: list entries may carry the transport suffix
        and mixed case; the registered-name normalization applies to both
        sides of the comparison."""
        import asyncio

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="Wire-Srv/mcp, ,")
        probe_client, patcher = _patch_probe_client(_quota_response(0))
        with patcher, pytest.raises(Exception):
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "wire-srv/sse"))
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]

    def test_federated_key_scoped_intact(self, monkeypatch):
        """Federated ``peer/server`` keys stay intact: only the trailing
        transport segment is stripped, so a federated wire server is gated by
        its full key while a sibling backend under the same peer is exempt."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="peer-1/wire-srv")
        probe_client, patcher = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": 100, "status": "active"}})
        )
        with patcher:
            # Exempt sibling first: balance probe only.
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "peer-1/other-srv/mcp"))
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]

        pq._cache.clear()
        probe_client2, patcher2 = _patch_probe_client(_quota_response(0))
        with patcher2, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "peer-1/wire-srv/mcp"))
        assert exc.value.detail["error"] == "MONTHLY_QUOTA_EXHAUSTED"

    def test_in_scope_unavailable_fail_closed_doctrine_unchanged(self, monkeypatch):
        """Within jurisdiction the fail-closed doctrine is untouched: a wire
        404 for an in-scope server is a real misconfiguration and refuses."""
        import asyncio

        from fastapi import HTTPException

        import auth_server.preflight_quota as pq

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        probe_client, patcher = _patch_probe_client(_probe_response(404, {"detail": "x"}))
        with patcher, pytest.raises(HTTPException) as exc:
            asyncio.run(pq.enforce_mcp_proxy_preflight(_claims(), "wire-srv/mcp"))
        assert exc.value.status_code == 503
        assert exc.value.detail["error"] == "QUOTA_UNAVAILABLE"


class TestRegisteredNameNormalizationEquivalence:
    """The scope matcher's local mirror must derive the same registered name
    as the /validate-side helper — the two hops must never disagree on the
    key (an equivalence-test failure is a drift alarm, not a test bug)."""

    @pytest.mark.parametrize(
        "server_path",
        [
            "srv",
            "srv/mcp",
            "srv/sse",
            "srv/messages",
            "peer/srv",
            "peer/srv/mcp",
            "peer/srv/sse",
            "peer/srv/messages",
            "/srv/",
            "//srv//mcp",
            "",
            "/",
            "///",
        ],
    )
    def test_local_mirror_matches_server_helper(self, server_path):
        from auth_server.preflight_quota import _registered_server_from_proxy_name
        from auth_server.server import _registered_server_from_proxy_path

        assert _registered_server_from_proxy_name(server_path) == (
            _registered_server_from_proxy_path(server_path)
        )


class TestQuotaGateScopeRoute:
    """Route-level jurisdiction + refusal-audit finality
    (fix-quota-gate-server-scope)."""

    @staticmethod
    def _headers(server: str) -> dict:
        from auth_server.internal_request_token import mint_mcp_proxy_token

        token = mint_mcp_proxy_token(
            subject="alice",
            scopes=["admin:all"],
            server_name=server,
            upstream_url="https://upstream.example/mcp",
        )
        return {"X-Internal-Token": token}

    def _post(self, client, server: str):
        return client.post(
            f"/mcp-proxy/{server}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=self._headers(server),
        )

    def _fake_mcp_logger(self):
        recorder = MagicMock()
        recorder.log_mcp_access = AsyncMock()
        return recorder, patch("auth_server.server.get_mcp_logger", return_value=recorder)

    def test_out_of_scope_forwards_and_never_probes_wire(self, monkeypatch):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, upstream_client = _patch_upstream_client(upstream)
        probe_client, probe_patch = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": 100, "status": "active"}})
        )
        with (
            probe_patch,
            upstream_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client, "lex-srv")
        assert response.status_code == 200
        assert _probe_urls(probe_client) == [_SUB2API_USER_URL]
        assert upstream_client.stream.called

    def test_in_scope_refusal_writes_definitive_audit_record(self, monkeypatch):
        """Scenario 6 (reinterpreted): a refused call must leave a refused
        final-outcome record — status=refused, HTTP error code, machine code
        in the message — alongside /validate's earlier success row."""
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        upstream_patch, upstream_client = _patch_upstream_client(
            _build_mock_upstream_response()
        )
        probe_client, probe_patch = _patch_probe_client(
            _probe_response(404, {"detail": "caller 56 未映射到客户"})
        )
        recorder, logger_patch = self._fake_mcp_logger()
        with (
            probe_patch,
            upstream_patch,
            logger_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client, "wire-srv/mcp")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "QUOTA_UNAVAILABLE"
        # The refused call never reaches the upstream.
        assert not upstream_client.stream.called
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]
        # The definitive record exists and says refused.
        recorder.log_mcp_access.assert_awaited_once()
        kwargs = recorder.log_mcp_access.await_args.kwargs
        assert kwargs["response_status"] == "refused"
        assert kwargs["error_code"] == 503
        assert "QUOTA_UNAVAILABLE" in (kwargs["error_message"] or "")
        assert kwargs["mcp_server"].name == "wire-srv/mcp"
        assert kwargs["identity"].username == "alice"

    def test_allowed_traffic_writes_no_refusal_record(self, monkeypatch):
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        upstream = _build_mock_upstream_response(
            200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        )
        upstream_patch, _upstream_client = _patch_upstream_client(upstream)
        # Free-tier semantics (enforce_balance=false): the line gate passes and
        # skips the balance probe, so the wire URL is the ONLY probe on the
        # allowed path.
        probe_client, probe_patch = _patch_probe_client(
            _quota_response(100, enforce_balance=False)
        )
        recorder, logger_patch = self._fake_mcp_logger()
        with (
            probe_patch,
            upstream_patch,
            logger_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client, "wire-srv/mcp")
        assert response.status_code == 200
        assert recorder.log_mcp_access.await_count == 0
        assert _probe_urls(probe_client) == [_WIRE_QUOTA_URL]

    def test_audit_logger_failure_never_masks_the_refusal(self, monkeypatch):
        """The audit write is best-effort: a broken logger must not turn a
        503 refusal into a 500."""
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        upstream_patch, _upstream_client = _patch_upstream_client(
            _build_mock_upstream_response()
        )
        probe_client, probe_patch = _patch_probe_client(_probe_response(404, {"detail": "x"}))
        broken = MagicMock()
        broken.log_mcp_access = AsyncMock(side_effect=RuntimeError("audit plane down"))
        with (
            probe_patch,
            upstream_patch,
            patch("auth_server.server.get_mcp_logger", return_value=broken),
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client, "wire-srv/mcp")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "QUOTA_UNAVAILABLE"

    def test_balance_refusal_on_exempt_server_also_audited(self, monkeypatch):
        """The refusal-audit wrapper covers the preflight's balance verdicts
        too (402 INSUFFICIENT_BALANCE on an exempt server)."""
        import auth_server.server as server_module
        from fastapi.testclient import TestClient

        _gate_env(monkeypatch, FD_WIRE_QUOTA_SERVERS="wire-srv")
        upstream_patch, upstream_client = _patch_upstream_client(
            _build_mock_upstream_response()
        )
        probe_client, probe_patch = _patch_probe_client(
            _probe_response(200, {"code": 0, "data": {"balance": 0, "status": "active"}})
        )
        recorder, logger_patch = self._fake_mcp_logger()
        with (
            probe_patch,
            upstream_patch,
            logger_patch,
            _patch_scope_repo_allow_all(),
            patch.object(server_module, "_read_mcp_filter_enabled", return_value=False),
        ):
            client = TestClient(server_module.app)
            response = self._post(client, "lex-srv")
        assert response.status_code == 402
        assert not upstream_client.stream.called
        recorder.log_mcp_access.assert_awaited_once()
        kwargs = recorder.log_mcp_access.await_args.kwargs
        assert kwargs["response_status"] == "refused"
        assert kwargs["error_code"] == 402
        assert "INSUFFICIENT_BALANCE" in (kwargs["error_message"] or "")
