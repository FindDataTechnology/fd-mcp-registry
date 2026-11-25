"""Unit tests for the sub2api ``sk-`` resolver (capability line-entry-unification)."""

import httpx
import pytest

from auth_server import sub2api_key_auth as m


class _FakeResp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _FakeClient:
    """Routes GET by URL substring; records the (url, headers/params) seen."""

    def __init__(self, router):
        self._router = router
        self.seen = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        self.seen.append({"url": url, "headers": headers or {}, "params": params or {}})
        return self._router(url)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SUB2API_BASE", "http://sub2api.test")
    monkeypatch.setenv("SUB2API_ADMIN_KEY", "admin-x")


def _patch(monkeypatch, router):
    client = _FakeClient(router)

    def _factory(*a, **k):
        return client

    monkeypatch.setattr(m.httpx, "AsyncClient", _factory)
    return client


def _router(models, holders):
    def r(url):
        if "/v1/models" in url:
            return models
        if "/api/v1/admin/users" in url:
            return holders
        return _FakeResp(404)

    return r


@pytest.mark.asyncio
async def test_resolves_unique_holder(monkeypatch):
    _patch(
        monkeypatch,
        _router(
            _FakeResp(200, {}),
            _FakeResp(200, {"data": {"items": [{"id": 143, "email": "a@b.c"}]}}),
        ),
    )
    out = await m.resolve_sk_caller("sk-live")
    assert out == {"sub2api_user_id": "143", "email": "a@b.c"}


@pytest.mark.asyncio
async def test_billing_refusal_is_rejected(monkeypatch):
    _patch(monkeypatch, _router(_FakeResp(403, {"code": "INSUFFICIENT_BALANCE"}), None))
    with pytest.raises(m.Sub2ApiKeyRejected):
        await m.resolve_sk_caller("sk-x")


@pytest.mark.asyncio
async def test_402_maps_to_402(monkeypatch):
    _patch(monkeypatch, _router(_FakeResp(402, {}), None))
    with pytest.raises(m.Sub2ApiKeyRejected) as exc:
        await m.resolve_sk_caller("sk-x")
    assert exc.value.status == 402


@pytest.mark.asyncio
async def test_403_insufficient_balance_maps_to_402(monkeypatch):
    # What the plane actually sends for an exhausted account: 403 + code. Mapping
    # that to 401 tells the customer their working key is invalid.
    _patch(
        monkeypatch,
        _router(_FakeResp(403, {"code": "INSUFFICIENT_BALANCE"}), None),
    )
    with pytest.raises(m.Sub2ApiKeyRejected) as exc:
        await m.resolve_sk_caller("sk-x")
    assert exc.value.status == 402


@pytest.mark.asyncio
async def test_403_other_code_stays_401(monkeypatch):
    _patch(monkeypatch, _router(_FakeResp(403, {"code": "GROUP_FORBIDDEN"}), None))
    with pytest.raises(m.Sub2ApiKeyRejected) as exc:
        await m.resolve_sk_caller("sk-x")
    assert exc.value.status is None


@pytest.mark.asyncio
async def test_connect_failure_is_unavailable(monkeypatch):
    def _factory(*a, **k):
        class C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *x):
                return False

            async def get(self, *a, **k):
                raise httpx.ConnectError("boom")

        return C()

    monkeypatch.setattr(m.httpx, "AsyncClient", _factory)
    with pytest.raises(m.Sub2ApiUnavailable):
        await m.resolve_sk_caller("sk-x")


@pytest.mark.asyncio
async def test_500_is_unavailable(monkeypatch):
    _patch(monkeypatch, _router(_FakeResp(503, {}), None))
    with pytest.raises(m.Sub2ApiUnavailable):
        await m.resolve_sk_caller("sk-x")


@pytest.mark.parametrize("items", [[], [{"id": 1}, {"id": 2}]])
@pytest.mark.asyncio
async def test_ambiguous_or_zero_holders_rejected(monkeypatch, items):
    _patch(monkeypatch, _router(_FakeResp(200, {}), _FakeResp(200, {"data": {"items": items}})))
    with pytest.raises(m.Sub2ApiKeyRejected):
        await m.resolve_sk_caller("sk-x")


@pytest.mark.asyncio
async def test_missing_admin_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("SUB2API_ADMIN_KEY", raising=False)
    _patch(monkeypatch, _router(_FakeResp(200, {}), None))
    with pytest.raises(m.Sub2ApiUnavailable):
        await m.resolve_sk_caller("sk-x")