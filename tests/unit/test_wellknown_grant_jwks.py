"""Public grant JWKS endpoint (establish-agent-principal-plane).

The delegation-grant JWKS must be reachable WITHOUT auth (downstream verifies
offline), so it lives under /.well-known/. These tests assert: no-aud -> 400,
plane-off -> empty key set (200, not 404), plane-on -> keys for the requested aud.
"""

from unittest.mock import patch

import pytest

from registry.api import wellknown_routes


class _FakeReq:
    def __init__(self, auds):
        self.query_params = _Q(auds)


class _Q:
    def __init__(self, auds):
        self._auds = auds

    def getlist(self, key):
        return self._auds if key == "aud" else []


@pytest.mark.asyncio
async def test_no_aud_is_400():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await wellknown_routes.get_grant_jwks(_FakeReq([]))
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_plane_off_returns_empty_keys():
    class S:
        grant_plane_enabled = False

    with patch("registry.core.config.settings", S):
        resp = await wellknown_routes.get_grant_jwks(_FakeReq(["fd-cn-report"]))
    import json

    assert json.loads(resp.body) == {"keys": []}


@pytest.mark.asyncio
async def test_plane_on_returns_keys():
    from registry.auth.grant import GrantSigner

    signer = GrantSigner(b"k" * 32, generation=1)

    class S:
        grant_plane_enabled = True

    class _Cache:
        def get(self):
            return signer

    with patch("registry.core.config.settings", S), patch(
        "registry.api.grant_routes._SIGNER_CACHE", _Cache()
    ):
        resp = await wellknown_routes.get_grant_jwks(_FakeReq(["fd-cn-report"]))
    import json

    body = json.loads(resp.body)
    assert len(body["keys"]) >= 1
    assert body["keys"][0]["kid"] == "fd-cn-report:1"
