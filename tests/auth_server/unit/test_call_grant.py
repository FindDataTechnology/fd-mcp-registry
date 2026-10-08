"""Tests for the per-owner monthly call grant on the proxy path (ecosystem-bridge 3.3).

Decision surface of ``auth_server.call_grant.enforce_call_grant`` with the
grant service mocked: gates (enabled / patch-key / group / tools-call body),
verdicts (allow, 402 GRANT_EXHAUSTED, 503 GRANT_UNAVAILABLE), and the
no-charge-on-refuse guarantee.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from registry.schemas.patch_key import PatchKeyInfo
from registry.services.call_grant_service import GrantVerdict

pytestmark = [pytest.mark.unit, pytest.mark.auth]

ENABLED = {"CALL_GRANT_ENABLED": "true"}


def _claims(sub: str = "comuser", groups: list[str] | None = None, **extra) -> dict:
    claims = {
        "sub": sub,
        "scopes": ["mcp:call"],
        "server": "fd-open-data-mcp",
        "auth_method": "patch-key",
        "client_id": "patch-key",
        "egress_user": "",
    }
    if groups is not None:
        claims["groups"] = groups
    claims.update(extra)
    return claims


def _body(method: str = "tools/call", *, batch: list[str] | None = None) -> bytes:
    if batch is not None:
        return json.dumps([{"jsonrpc": "2.0", "method": m, "id": i} for i, m in enumerate(batch)]).encode()
    return json.dumps({"jsonrpc": "2.0", "method": method, "id": 1, "params": {"name": "read"}}).encode()




# ── 自包含夹具（镜像 test_patch_key_validate 的形状）────────────────────
_PLAINTEXT_KEY = "wgk-" + "x" * 40


class _FakePatchKeyService:
    def __init__(self, info, plaintext):
        self._info = info
        self._plaintext = plaintext

    async def verify_key(self, plaintext):
        if self._info is not None and plaintext == self._plaintext:
            return self._info
        return None

    async def touch_last_used(self, key_id):
        pass


def _verdict(allowed: bool, used: int, limit: int = 5000) -> GrantVerdict:
    return GrantVerdict(allowed=allowed, used=used, limit=limit, month="2026-10")


@pytest.fixture
def grant_env(monkeypatch, tmp_path):
    """Enabled gate + hermetic audit path; yield the service-mock patcher."""
    monkeypatch.setenv("CALL_GRANT_ENABLED", "true")
    monkeypatch.setenv("CALL_GRANT_MONTHLY_LIMIT", "5000")
    monkeypatch.setenv("CALL_GRANT_GROUPS", "community")
    monkeypatch.setenv("CALL_GRANT_AUDIT_PATH", str(tmp_path / "grant.jsonl"))
    yield lambda verdict_or_error: patch(
        "registry.services.call_grant_service.get_call_grant_service",
        AsyncMock(return_value=_fake_service(verdict_or_error)),
    )


class _FakeService:
    def __init__(self, verdict: GrantVerdict | None = None, error: Exception | None = None) -> None:
        self.verdict = verdict
        self.error = error
        self.consume_calls: list[str] = []

    async def check_and_consume(self, owner, limit, *, month=None):
        self.consume_calls.append(owner)
        if self.error:
            raise self.error
        assert month is None  # 真实月由服务端自取
        return self.verdict


def _fake_service(verdict_or_error):
    if isinstance(verdict_or_error, Exception):
        return _FakeService(error=verdict_or_error)
    return _FakeService(verdict=verdict_or_error)


async def test_disabled_gate_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("CALL_GRANT_ENABLED", raising=False)
    # 服务从未被触达：patch 成「碰即炸」证明零调用
    with patch(
        "registry.services.call_grant_service.get_call_grant_service",
        AsyncMock(side_effect=AssertionError("service must not be touched")),
    ):
        from auth_server.call_grant import enforce_call_grant

        await enforce_call_grant(_claims(groups=["community"]), _body())  # 不抛即过


async def test_tools_call_under_limit_admits_and_consumes(grant_env) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(True, used=1))
    with fake_holder as svc:
        await enforce_call_grant(_claims(groups=["community"]), _body())
    assert svc.return_value.consume_calls == ["comuser"]


async def test_handshake_methods_ride_free(grant_env) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(True, used=0))
    with fake_holder as svc:
        await enforce_call_grant(_claims(groups=["community"]), _body("initialize"))
        await enforce_call_grant(_claims(groups=["community"]), _body("tools/list"))
        await enforce_call_grant(_claims(groups=["community"]), _body("notifications/initialized"))
        await enforce_call_grant(_claims(groups=["community"]), b"")  # 空体
        await enforce_call_grant(_claims(groups=["community"]), b"{not json")
    assert svc.return_value.consume_calls == []


async def test_batch_with_tools_call_counts(grant_env) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(True, used=1))
    with fake_holder as svc:
        await enforce_call_grant(_claims(groups=["community"]), _body(batch=["initialize", "tools/call"]))
    assert svc.return_value.consume_calls == ["comuser"]


async def test_non_patch_key_caller_skips(grant_env) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(True, used=0))
    with fake_holder as svc:
        await enforce_call_grant(_claims(groups=["community"], auth_method="oauth2", client_id="logto-app"), _body())
    assert svc.return_value.consume_calls == []


async def test_non_grant_group_caller_skips(grant_env) -> None:
    """wire 客户（sub2api 预检车道）不走免费池。"""
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(True, used=0))
    with fake_holder as svc:
        await enforce_call_grant(_claims(groups=["wire-customers"]), _body())
        await enforce_call_grant(_claims(groups=[]), _body())
    assert svc.return_value.consume_calls == []


async def test_exhausted_refuses_402_and_audits(grant_env) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(False, used=5000))
    with fake_holder as svc:
        with pytest.raises(HTTPException) as excinfo:
            await enforce_call_grant(_claims(groups=["community"]), _body())
    assert excinfo.value.status_code == 402
    detail = excinfo.value.detail
    assert detail["error"] == "GRANT_EXHAUSTED"
    assert detail["limit"] == 5000 and detail["month"] == "2026-10"
    assert svc.return_value.consume_calls == ["comuser"]  # 查过（判定来源）
    # 服务端语义：拒收不 +1 —— 由 test_call_grant_service 侧证


async def test_store_unavailable_fails_closed_503(grant_env) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(RuntimeError("mongo down"))
    with fake_holder:
        with pytest.raises(HTTPException) as excinfo:
            await enforce_call_grant(_claims(groups=["community"]), _body())
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"] == "GRANT_UNAVAILABLE"


def _audit_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


async def test_refusal_writes_jsonl_audit(grant_env, tmp_path) -> None:
    from auth_server.call_grant import enforce_call_grant

    fake_holder = grant_env(_verdict(False, used=5000))
    with fake_holder:
        with pytest.raises(HTTPException):
            await enforce_call_grant(_claims(groups=["community"]), _body())
    events = _audit_events(tmp_path / "grant.jsonl")
    assert events and events[-1]["event"] == "grant_exhausted"
    assert events[-1]["owner"] == "comuser" and events[-1]["limit"] == 5000


# ── 3.4 tier gate：付费档 server 的升级式拒绝 ─────────────────────────────

async def test_tier_gate_names_the_tier(grant_env, monkeypatch) -> None:
    from auth_server.call_grant import is_tier_gated

    monkeypatch.setenv("CALL_GRANT_TIER_SERVERS", "law-bench:paid,other-x:pro")
    assert is_tier_gated(_claims(groups=["community"]), "law-bench") == "paid"
    assert is_tier_gated(_claims(groups=["community"]), "other-x") == "pro"


async def test_tier_gate_skips_untargeted_servers_and_lanes(grant_env, monkeypatch) -> None:
    from auth_server.call_grant import is_tier_gated

    monkeypatch.setenv("CALL_GRANT_TIER_SERVERS", "law-bench:paid")
    assert is_tier_gated(_claims(groups=["community"]), "fd-open-data-mcp") is None
    assert is_tier_gated(_claims(groups=["wire-customers"]), "law-bench") is None
    assert is_tier_gated(_claims(groups=["community"], auth_method="oauth2", client_id="app"), "law-bench") is None


async def test_tier_gate_inert_when_unconfigured(grant_env, monkeypatch) -> None:
    from auth_server.call_grant import is_tier_gated

    monkeypatch.delenv("CALL_GRANT_TIER_SERVERS", raising=False)
    assert is_tier_gated(_claims(groups=["community"]), "law-bench") is None


class TestTierGateOnValidate:
    """ecosystem-bridge 3.4: the tier gate must fire on the /validate hop too —
    a grant-group patch-key caller aiming at a gated server gets 402
    TIER_REQUIRED there (nginx blocks on /validate; the mcp_proxy gate alone
    never runs for them)."""

    def test_law_bench_402_before_scope_403(self, monkeypatch, tmp_path):
        import auth_server.server as server_module

        monkeypatch.setenv("CALL_GRANT_ENABLED", "true")
        monkeypatch.setenv("CALL_GRANT_MONTHLY_LIMIT", "5000")
        monkeypatch.setenv("CALL_GRANT_GROUPS", "community")
        monkeypatch.setenv("CALL_GRANT_TIER_SERVERS", "law-bench:paid")
        monkeypatch.setenv("CALL_GRANT_AUDIT_PATH", str(tmp_path / "g.jsonl"))

        info = PatchKeyInfo(
            key_id="keyid-tier", name="tier-verify", key_prefix=_PLAINTEXT_KEY[:12],
            username="freshuser", email="f@example.com", provider="logto",
            groups=[], status=PATCH_KEY_STATUS_ACTIVE,
            created_at=datetime.utcnow(), last_used_at=None, revoked_at=None,
        )
        service = _FakePatchKeyService(info, _PLAINTEXT_KEY)
        enrich = AsyncMock(return_value=["community"])
        with (
            patch("registry.services.patch_key_service.get_patch_key_service",
                  new=AsyncMock(return_value=service)),
            patch("mongodb_groups_enrichment.enrich_user_groups_from_mongodb", enrich),
        ):
            client = TestClient(server_module.app)
            response = client.get(
                "/validate",
                headers={
                    "Authorization": f"Bearer {_PLAINTEXT_KEY}",
                    "X-Original-URL": "https://example.com/law-bench/mcp",
                },
            )
        assert response.status_code == 402, response.text
        body = response.json()
        assert body["detail"]["error"] == "TIER_REQUIRED"
        assert body["detail"]["tier"] == "paid"

    def test_open_data_server_not_gated(self, monkeypatch, tmp_path):
        import auth_server.server as server_module

        monkeypatch.setenv("CALL_GRANT_ENABLED", "true")
        monkeypatch.setenv("CALL_GRANT_MONTHLY_LIMIT", "5000")
        monkeypatch.setenv("CALL_GRANT_GROUPS", "community")
        monkeypatch.setenv("CALL_GRANT_TIER_SERVERS", "law-bench:paid")
        monkeypatch.setenv("CALL_GRANT_AUDIT_PATH", str(tmp_path / "g.jsonl"))

        info = PatchKeyInfo(
            key_id="keyid-tier2", name="tier-verify-2", key_prefix=_PLAINTEXT_KEY[:12],
            username="freshuser", email="f@example.com", provider="logto",
            groups=[], status=PATCH_KEY_STATUS_ACTIVE,
            created_at=datetime.utcnow(), last_used_at=None, revoked_at=None,
        )
        service = _FakePatchKeyService(info, _PLAINTEXT_KEY)
        enrich = AsyncMock(return_value=["community"])
        with (
            patch("registry.services.patch_key_service.get_patch_key_service",
                  new=AsyncMock(return_value=service)),
            patch("mongodb_groups_enrichment.enrich_user_groups_from_mongodb", enrich),
        ):
            client = TestClient(server_module.app)
            response = client.get(
                "/validate",
                headers={
                    "Authorization": f"Bearer {_PLAINTEXT_KEY}",
                    "X-Original-URL": "https://example.com/fd-open-data-mcp/mcp",
                },
            )
        # 非档位 server：不应 402（401/403 由 scope 结果决定，但绝非档位拒绝）
        assert response.status_code != 402
