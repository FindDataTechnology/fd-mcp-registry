"""Unit tests for the auth-server-side grant acceptance (task group 4).

Covers the pure selectors and the offline-verify glue without a live gateway:
looks_like_grant prefilter, tenant upstream selection, and the budget verdict
path (mocked counter).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

from registry.auth.grant import GrantSigner

import sys, pathlib

# auth_server modules use flat imports in-container; for tests import by path.
_AUTH = pathlib.Path(__file__).resolve().parents[2] / "auth_server"
if str(_AUTH) not in sys.path:
    sys.path.insert(0, str(_AUTH))

from tenant_routing import select_upstream  # noqa: E402
from grant_accept import looks_like_grant, grant_claims_from_request  # noqa: E402

SEED = b"w" * 32


def _grant() -> str:
    s = GrantSigner(SEED, generation=1)
    return s.sign(
        sub="agent:1", act="user:9", aud="svcA", scope=["line:read"],
        tenant="t1", run="r1", budget={"calls": 5}, ttl_seconds=60,
    )


def test_looks_like_grant_true_for_gateway_jwt():
    assert looks_like_grant(_grant()) is True


def test_looks_like_grant_false_for_non_jwt():
    assert looks_like_grant("wgk-abc") is False
    assert looks_like_grant("not.a.jwt") is False


def test_grant_claims_from_request_none_without_bearer():
    req = SimpleNamespace(headers={})
    assert grant_claims_from_request(req) is None


def test_grant_claims_from_request_reads_bearer():
    token = _grant()
    req = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
    claims = grant_claims_from_request(req)
    assert claims is not None
    assert claims["tenant"] == "t1"
    assert claims["budget"] == {"calls": 5}


def test_grant_claims_ignores_non_grant_iss():
    # A non-gateway JWT returns None
    tok = jwt.encode({"iss": "other", "sub": "x"}, "k" * 32, algorithm="HS256")
    req = SimpleNamespace(headers={"authorization": f"Bearer {tok}"})
    assert grant_claims_from_request(req) is None


def test_select_upstream_shared_ignores_override():
    assert select_upstream(
        default_upstream="http://shared",
        entry_metadata={"tenant_upstreams": {"t1": "http://cell1"}},
        tenant_id="t1",
        isolation_tier="shared",
    ) == "http://shared"


def test_select_upstream_dedicated_with_override():
    assert select_upstream(
        default_upstream="http://shared",
        entry_metadata={"tenant_upstreams": {"t1": "http://cell1"}},
        tenant_id="t1",
        isolation_tier="dedicated_cell",
    ) == "http://cell1"


def test_select_upstream_dedicated_without_override_keeps_default():
    assert select_upstream(
        default_upstream="http://shared",
        entry_metadata={"tenant_upstreams": {}},
        tenant_id="t1",
        isolation_tier="dedicated_cell",
    ) == "http://shared"


def test_select_upstream_no_tenant_keeps_default():
    assert select_upstream(
        default_upstream="http://shared",
        entry_metadata=None,
        tenant_id=None,
        isolation_tier=None,
    ) == "http://shared"


@pytest.mark.asyncio
async def test_enforce_grant_budget_no_budget_is_noop():
    from grant_budget import enforce_grant_budget

    # No budget claim => returns without touching the counter
    await enforce_grant_budget(
        {"tenant": "t1"},
        server_name="svcA",
        tool_name="read",
        request_body={"method": "tools/call"},
    )


@pytest.mark.asyncio
async def test_enforce_grant_budget_skips_non_tools_call():
    from grant_budget import enforce_grant_budget

    await enforce_grant_budget(
        {"budget": {"calls": 1}, "run": "r1"},
        server_name="svcA",
        tool_name=None,
        request_body={"method": "initialize"},
    )


@pytest.mark.asyncio
async def test_enforce_grant_budget_exhausted_402(monkeypatch):
    from fastapi import HTTPException

    import grant_budget

    counter = MagicMock()
    counter.available = AsyncMock(return_value=True)
    counter.peek = AsyncMock(return_value={"calls": 5})
    counter.consume = AsyncMock()

    async def _get():
        return counter

    monkeypatch.setattr(
        "registry.ledger.mongo_counter.get_mongo_budget_counter", _get
    )
    with pytest.raises(HTTPException) as ei:
        await grant_budget.enforce_grant_budget(
            {"budget": {"calls": 5}, "run": "r1"},
            server_name="svcA",
            tool_name="read",
            request_body={"method": "tools/call"},
        )
    assert ei.value.status_code == 402
    counter.consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_grant_budget_unavailable_503(monkeypatch):
    from fastapi import HTTPException

    import grant_budget

    counter = MagicMock()
    counter.available = AsyncMock(return_value=False)

    async def _get():
        return counter

    monkeypatch.setattr(
        "registry.ledger.mongo_counter.get_mongo_budget_counter", _get
    )
    with pytest.raises(HTTPException) as ei:
        await grant_budget.enforce_grant_budget(
            {"budget": {"calls": 5}, "run": "r1"},
            server_name="svcA",
            tool_name="read",
            request_body={"method": "tools/call"},
        )
    assert ei.value.status_code == 503


def test_issuance_does_not_emit_ledger(monkeypatch):
    """换票不计费: minting a grant must never append a ledger event."""
    import registry.ledger.events as ledger_events

    emitted = []
    monkeypatch.setattr(ledger_events, "emit", lambda e: emitted.append(e))

    s = GrantSigner(SEED, generation=1)
    s.sign(
        sub="u1", aud="svcA", scope=["line:read"], tenant="t1", run="r1",
        budget={"calls": 5}, ttl_seconds=60,
    )
    assert emitted == []  # signing is not a billable call