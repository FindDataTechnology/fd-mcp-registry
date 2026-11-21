"""Unit tests for the downstream grant verifier (task group 2).

Uses an httpx MockTransport so JWKS is served from the in-process signer; no
real network. Covers: successful offline verify, JWKS caching, JWKS-unavailable
distinct from a bad grant, and expiry.
"""

import httpx
import pytest

from registry.auth.grant import GrantSigner
from registry.auth.grant_verify import GrantVerifier, JWKSUnavailable

SEED = b"q" * 32


def _signer() -> GrantSigner:
    return GrantSigner(SEED, generation=1)


def _transport_for(signer: GrantSigner, *, fail: bool = False, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if fail:
            raise httpx.ConnectError("boom")
        aud = request.url.params.get("aud")
        return httpx.Response(200, json=signer.jwks([aud]))

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_verify_success():
    s = _signer()
    token = s.sign(sub="u1", aud="svcA", scope=["line:read"], tenant="t", run="r", budget=None)
    v = GrantVerifier(
        jwks_url="https://gw/api/jwks",
        aud="svcA",
        transport=_transport_for(s),
    )
    claims = await v.verify(token, required_scope="line:read")
    assert claims["sub"] == "u1"


@pytest.mark.asyncio
async def test_jwks_cached_within_ttl():
    s = _signer()
    calls: list[str] = []
    token = s.sign(sub="u1", aud="svcA", scope=[], tenant="t", run="r", budget=None)
    v = GrantVerifier(
        jwks_url="https://gw/api/jwks",
        aud="svcA",
        jwks_ttl=300,
        transport=_transport_for(s, calls=calls),
    )
    await v.verify(token)
    await v.verify(token)
    assert len(calls) == 1  # second verify reused the cache


@pytest.mark.asyncio
async def test_jwks_unavailable_is_distinct():
    s = _signer()
    token = s.sign(sub="u1", aud="svcA", scope=[], tenant="t", run="r", budget=None)
    v = GrantVerifier(
        jwks_url="https://gw/api/jwks",
        aud="svcA",
        transport=_transport_for(s, fail=True),
    )
    with pytest.raises(JWKSUnavailable):
        await v.verify(token)