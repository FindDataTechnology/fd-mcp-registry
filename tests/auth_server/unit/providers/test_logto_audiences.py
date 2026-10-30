"""Unit tests for Logto bearer-token audience acceptance (ingress gate).

Regression coverage for the keyless MCP lane: the gateway's RFC 9728 PRM
advertises ``resource`` = the canonical REGISTRY_URL (bare origin), and
RFC 8707 spec-compliant MCP clients send that value verbatim on every
authorization/token request — so the user access token's ``aud`` is the bare
origin, NOT the path-qualified ``m2m_resource``. A resource server MUST accept
the identifiers it itself advertises; before the ``LOGTO_ACCEPTED_AUDIENCES``
knob existed those tokens were rejected with 401.

Uses a real RSA keypair so signature/issuer/audience enforcement is genuine,
not mock-shaped (same convention as test_id_token_verification.py).
"""

import json
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

pytestmark = [pytest.mark.unit, pytest.mark.auth]

ISSUER = "https://auth.test/oidc"
KID = "test-kid"


def _build_keypair() -> tuple[rsa.RSAPrivateKey, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return private_key, {"keys": [public_jwk]}


def _provider():
    from providers.logto import LogtoProvider

    return LogtoProvider(
        logto_url="https://auth.test",
        client_id="gateway-web",
        client_secret="not-needed-here",
        m2m_client_id="gateway-m2m",
        m2m_client_secret="also-unused",
        m2m_resource="https://mcp.test/api",
    )


def _sign_access_token(private_key: rsa.RSAPrivateKey, claims: dict) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "user-1",
        "iat": now,
        "exp": now + 3600,
        "client_id": "some-native-app",
        "scope": "gateway:access",
        **claims,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": KID})


def _validate_with(provider, token: str, jwks: dict) -> dict:
    with patch.object(type(provider), "get_jwks", return_value=jwks):
        return provider.validate_token(token)


class TestExtraAcceptedAudiences:
    def test_default_config_rejects_bare_origin_audience(self):
        """Legacy behavior unchanged: without the knob, origin-aud tokens fail."""
        private_key, jwks = _build_keypair()
        provider = _provider()
        assert provider.extra_accepted_audiences == []
        token = _sign_access_token(private_key, {"aud": "https://mcp.test"})
        with pytest.raises(ValueError):
            _validate_with(provider, token, jwks)

    def test_advertised_origin_audience_accepted_via_env_knob(self):
        """With LOGTO_ACCEPTED_AUDIENCES set to the canonical origin, the
        RFC 8707 token that spec clients produce passes the audience check."""
        private_key, jwks = _build_keypair()
        provider = _provider()
        provider.extra_accepted_audiences = ["https://mcp.test"]
        token = _sign_access_token(
            private_key,
            {"aud": "https://mcp.test", "roles": ["mcp-registry-admin"]},
        )
        result = _validate_with(provider, token, jwks)
        assert result["valid"] is True
        # groups come from the roles claim (custom-JWT lane), which is what
        # the scope gate keys on.
        assert "mcp-registry-admin" in result["groups"]

    def test_m2m_resource_audience_still_accepted(self):
        """The pre-existing audience set is untouched by the new knob."""
        private_key, jwks = _build_keypair()
        provider = _provider()
        provider.extra_accepted_audiences = ["https://mcp.test"]
        token = _sign_access_token(private_key, {"aud": "https://mcp.test/api"})
        result = _validate_with(provider, token, jwks)
        assert result["valid"] is True

    def test_unknown_audience_still_rejected_with_knob_present(self):
        private_key, jwks = _build_keypair()
        provider = _provider()
        provider.extra_accepted_audiences = ["https://mcp.test"]
        token = _sign_access_token(private_key, {"aud": "https://evil.test/api"})
        with pytest.raises(ValueError):
            _validate_with(provider, token, jwks)

    def test_ctor_parses_comma_separated_string(self):
        from providers.logto import LogtoProvider

        provider = LogtoProvider(
            logto_url="https://auth.test",
            client_id="c",
            client_secret="s",
            extra_accepted_audiences=" https://mcp.test , https://other.test ,, ",
        )
        assert provider.extra_accepted_audiences == ["https://mcp.test", "https://other.test"]

    def test_ctor_none_and_empty_keep_legacy_set(self):
        from providers.logto import LogtoProvider

        for raw in (None, "", "  "):
            provider = LogtoProvider(
                logto_url="https://auth.test", client_id="c", client_secret="s",
                extra_accepted_audiences=raw,
            )
            assert provider.extra_accepted_audiences == []
