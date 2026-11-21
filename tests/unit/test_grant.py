"""Unit tests for the delegation-grant crypto (task group 2).

Covers: per-audience HKDF key derivation (stable + isolated), sign/verify
round-trip, single-audience enforcement, TTL cap, scope check, issuer check,
and JWKS export. No network, no DB.
"""

import time

import jwt
import pytest

from registry.auth.grant import (
    GRANT_MAX_TTL_SECONDS,
    GrantConfigError,
    GrantSigner,
    GrantVerificationError,
    verify_grant,
)

SEED = b"z" * 32


def _signer(gen: int = 1) -> GrantSigner:
    return GrantSigner(SEED, generation=gen)


def test_seed_too_short_rejected():
    with pytest.raises(GrantConfigError):
        GrantSigner(b"short")


def test_derivation_is_stable_and_isolated():
    s = _signer()
    assert s.signing_key("a").kid == "a:1"
    # same aud → same key material (deterministic derivation)
    jwk_a1 = s.jwks(["a"])["keys"][0]["x"]
    jwk_a2 = _signer().jwks(["a"])["keys"][0]["x"]
    assert jwk_a1 == jwk_a2
    # different aud → different key
    assert s.jwks(["a"])["keys"][0]["x"] != s.jwks(["b"])["keys"][0]["x"]
    # different generation → different key
    assert s.jwks(["a"])["keys"][0]["x"] != _signer(gen=2).jwks(["a"])["keys"][0]["x"]


def test_roundtrip_with_act_and_budget():
    s = _signer()
    token = s.sign(
        sub="agent:1",
        act="user:9",
        aud="svcA",
        scope=["line:read"],
        tenant="t1",
        run="r1",
        budget={"calls": 500},
        ttl_seconds=600,
    )
    claims = verify_grant(token, jwks=s.jwks(["svcA"]), expected_aud="svcA", required_scope="line:read")
    assert claims["sub"] == "agent:1"
    assert claims["act"] == {"sub": "user:9"}
    assert claims["tenant"] == "t1"
    assert claims["run"] == "r1"
    assert claims["budget"] == {"calls": 500}
    assert claims["iss"] == "mcp-gateway"


def test_ttl_over_max_rejected():
    with pytest.raises(ValueError):
        _signer().sign(sub="x", aud="a", scope=[], tenant="t", run="r", budget=None, ttl_seconds=4000)


def test_wrong_audience_rejected():
    s = _signer()
    token = s.sign(sub="x", aud="svcA", scope=[], tenant="t", run="r", budget=None, ttl_seconds=60)
    with pytest.raises(GrantVerificationError):
        verify_grant(token, jwks=s.jwks(["svcB"]), expected_aud="svcB")


def test_cross_audience_key_cannot_verify():
    s = _signer()
    token = s.sign(sub="x", aud="svcA", scope=[], tenant="t", run="r", budget=None, ttl_seconds=60)
    # serve only svcB's key; the svcA token's kid is absent → no key matches
    with pytest.raises(GrantVerificationError):
        verify_grant(token, jwks=s.jwks(["svcB"]), expected_aud="svcA")


def test_expired_rejected():
    s = _signer()
    token = s.sign(sub="x", aud="svcA", scope=[], tenant="t", run="r", budget=None, ttl_seconds=1)
    # forge expiry by re-signing with a past exp is not possible via sign(); instead
    # verify that a token minted now+1s fails once we treat it as expired by patching.
    import jwt as _jwt

    claims = _jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = int(time.time()) - 10
    from cryptography.hazmat.primitives import serialization

    pem = s.signing_key("svcA").private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    expired = _jwt.encode(claims, pem, algorithm="EdDSA", headers={"kid": "svcA:1"})
    with pytest.raises(GrantVerificationError):
        verify_grant(expired, jwks=s.jwks(["svcA"]), expected_aud="svcA")


def test_missing_scope_rejected():
    s = _signer()
    token = s.sign(sub="x", aud="svcA", scope=["line:read"], tenant="t", run="r", budget=None, ttl_seconds=60)
    with pytest.raises(GrantVerificationError):
        verify_grant(token, jwks=s.jwks(["svcA"]), expected_aud="svcA", required_scope="line:write")


def test_issuer_check():
    s = _signer()
    token = s.sign(sub="x", aud="svcA", scope=[], tenant="t", run="r", budget=None, ttl_seconds=60)
    import jwt as _jwt
    from cryptography.hazmat.primitives import serialization

    claims = _jwt.decode(token, options={"verify_signature": False})
    claims["iss"] = "evil"
    pem = s.signing_key("svcA").private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    bad = _jwt.encode(claims, pem, algorithm="EdDSA", headers={"kid": "svcA:1"})
    with pytest.raises(GrantVerificationError):
        verify_grant(bad, jwks=s.jwks(["svcA"]), expected_aud="svcA")


def test_rotation_jwks_serves_old_and_new_generation():
    old = _signer(gen=1)
    new = _signer(gen=2)
    old_token = old.sign(sub="x", aud="svcA", scope=[], tenant="t", run="r", budget=None, ttl_seconds=60)
    # The new generation's JWKS includes the previous generation's kid, so an
    # old token minted under gen=1 still verifies after the rotation.
    kids = {k["kid"] for k in new.jwks(["svcA"], include_previous=1)["keys"]}
    assert kids == {"svcA:1", "svcA:2"}
    claims = verify_grant(
        old_token, jwks={"keys": new.jwks(["svcA"], include_previous=1)["keys"]}, expected_aud="svcA"
    )
    assert claims["sub"] == "x"