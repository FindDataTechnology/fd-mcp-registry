"""Unit tests for OIDC id_token signature verification.

These tests exercise the shared JWKS-backed id_token verification path used by
the OAuth2 callback. They use a real RSA keypair so that a forged or tampered
signature is genuinely rejected by the cryptography, not merely by a mock.

Regression coverage: a compromised/misconfigured IdP or a token-endpoint
response tamper must NOT be able to inject arbitrary identity/group claims. Any
id_token that fails signature, issuer, audience, or expiry verification is
rejected and its claims never reach the caller.
"""

import json
import logging
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.unit, pytest.mark.auth]


# =============================================================================
# Test helpers: real RSA keypair + JWKS + signed id_token
# =============================================================================


def _build_keypair(kid: str = "test-kid") -> tuple[rsa.RSAPrivateKey, dict]:
    """Generate an RSA keypair and the matching single-key JWKS document."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = kid
    public_jwk["alg"] = "RS256"
    public_jwk["use"] = "sig"
    return private_key, {"keys": [public_jwk]}


def _sign_id_token(
    private_key: rsa.RSAPrivateKey,
    claims: dict,
    kid: str = "test-kid",
) -> str:
    """Sign an id_token with the given RSA private key (RS256)."""
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _base_claims(issuer: str, audience: str) -> dict:
    now = int(time.time())
    return {
        "iss": issuer,
        "aud": audience,
        "sub": "user-123",
        "preferred_username": "alice",
        "email": "alice@example.com",
        "groups": ["mcp-registry-admin"],
        "iat": now,
        "exp": now + 3600,
    }


def _make_logto_provider():
    from providers.logto import LogtoProvider

    return LogtoProvider(
        logto_url="http://logto:3001",
        client_id="gateway-web",
        client_secret="secret",  # noqa: S106 - test fixture, not a real secret
        logto_external_url="https://logto.example.com",
    )


# =============================================================================
# Happy path: a validly signed token is accepted and claims returned
# =============================================================================


class TestIdTokenAccepted:
    """A correctly signed id_token with the right iss/aud/exp is accepted."""

    def test_valid_signed_token_accepted(self):
        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        issuer = provider.issuer
        token = _sign_id_token(private_key, _base_claims(issuer, provider.client_id))

        with patch.object(provider, "get_jwks", return_value=jwks):
            claims = provider.validate_id_token(token)

        assert claims["sub"] == "user-123"
        assert claims["groups"] == ["mcp-registry-admin"]

    def test_issuer_is_public_endpoint(self):
        """Logto has a single issuer: the configured public endpoint + /oidc."""
        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        token = _sign_id_token(
            private_key, _base_claims(provider.issuer, provider.client_id)
        )

        with patch.object(provider, "get_jwks", return_value=jwks):
            claims = provider.validate_id_token(token)

        assert claims["iss"] == "https://logto.example.com/oidc"


# =============================================================================
# Rejection paths: fail closed on any verification failure
# =============================================================================


class TestIdTokenRejected:
    """Any id_token that fails verification is rejected; no claims returned."""

    def test_wrong_signing_key_rejected(self):
        """Token signed by a DIFFERENT key than the JWKS advertises."""
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        # JWKS advertises key A; token is signed with key B.
        _key_a, jwks = _build_keypair()
        attacker_key, _ = _build_keypair()
        token = _sign_id_token(attacker_key, _base_claims(provider.issuer, provider.client_id))

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_unsigned_none_alg_rejected(self):
        """A token with alg=none (unsigned) is rejected before any claim use."""
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        _key, jwks = _build_keypair()
        # Attacker crafts an unsigned token asserting admin group.
        forged = jwt.encode(
            _base_claims(provider.issuer, provider.client_id),
            key="",
            algorithm="none",
            headers={"kid": "test-kid"},
        )

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(forged)

    def test_wrong_issuer_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        token = _sign_id_token(
            private_key,
            _base_claims("https://evil.example.com/realms/test-realm", provider.client_id),
        )

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_wrong_audience_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        token = _sign_id_token(private_key, _base_claims(provider.issuer, "some-other-client"))

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_expired_token_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        claims = _base_claims(provider.issuer, provider.client_id)
        claims["exp"] = int(time.time()) - 10  # expired
        claims["iat"] = int(time.time()) - 3610
        token = _sign_id_token(private_key, claims)

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_kid_not_in_jwks_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair(kid="kid-a")
        token = _sign_id_token(
            private_key, _base_claims(provider.issuer, provider.client_id), kid="kid-b"
        )

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_missing_kid_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        # Sign without a kid header.
        token = jwt.encode(
            _base_claims(provider.issuer, provider.client_id),
            private_key,
            algorithm="RS256",
        )

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_jwks_unreachable_fails_closed(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, _jwks = _build_keypair()
        token = _sign_id_token(private_key, _base_claims(provider.issuer, provider.client_id))

        with patch.object(provider, "get_jwks", side_effect=ValueError("network down")):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token)

    def test_empty_token_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        with pytest.raises(IdTokenVerificationError):
            provider.validate_id_token("")

    def test_garbage_token_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        _key, jwks = _build_keypair()
        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token("not-a-jwt")


# =============================================================================
# Per-provider audience/issuer wiring
# =============================================================================


class TestIdTokenNonce:
    """When an expected_nonce is supplied, the verified token's nonce claim must
    match it. This binds the id_token to the specific login (replay/injection
    protection) and is enforced AFTER signature verification.
    """

    def test_matching_nonce_accepted(self):
        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        claims = _base_claims(provider.issuer, provider.client_id)
        claims["nonce"] = "login-nonce-abc"
        token = _sign_id_token(private_key, claims)

        with patch.object(provider, "get_jwks", return_value=jwks):
            verified = provider.validate_id_token(token, expected_nonce="login-nonce-abc")

        assert verified["nonce"] == "login-nonce-abc"

    def test_mismatched_nonce_rejected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        claims = _base_claims(provider.issuer, provider.client_id)
        claims["nonce"] = "attacker-nonce"
        token = _sign_id_token(private_key, claims)

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token, expected_nonce="expected-nonce")

    def test_absent_nonce_claim_rejected_when_expected(self):
        from providers.base import IdTokenVerificationError

        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        # No nonce claim in the token, but a nonce is expected for this login.
        token = _sign_id_token(private_key, _base_claims(provider.issuer, provider.client_id))

        with patch.object(provider, "get_jwks", return_value=jwks):
            with pytest.raises(IdTokenVerificationError):
                provider.validate_id_token(token, expected_nonce="expected-nonce")

    def test_no_expected_nonce_skips_check(self):
        """When no nonce is bound to the login, verification does not require one
        (a signature-only valid token still passes)."""
        provider = _make_logto_provider()
        private_key, jwks = _build_keypair()
        token = _sign_id_token(private_key, _base_claims(provider.issuer, provider.client_id))

        with patch.object(provider, "get_jwks", return_value=jwks):
            verified = provider.validate_id_token(token, expected_nonce=None)

        assert verified["sub"] == "user-123"


# =============================================================================
# Default base implementation fails closed
# =============================================================================


class TestBaseDefaultFailsClosed:
    """A provider that has not opted into id_token verification must deny."""

    def test_default_validate_id_token_raises(self):
        from providers.base import AuthProvider, IdTokenVerificationError

        class _MinimalProvider(AuthProvider):
            def validate_token(self, token, **kwargs):
                return {}

            def get_jwks(self):
                return {"keys": []}

            def exchange_code_for_token(self, code, redirect_uri):
                return {}

            def get_user_info(self, access_token):
                return {}

            def get_auth_url(self, redirect_uri, state, scope=None):
                return ""

            def get_logout_url(self, redirect_uri):
                return ""

            def refresh_token(self, refresh_token):
                return {}

            def validate_m2m_token(self, token):
                return {}

            def get_m2m_token(self, client_id=None, client_secret=None, scope=None):
                return {}

            def authorization_server_metadata(self):
                return {}

        provider = _MinimalProvider()
        with pytest.raises(IdTokenVerificationError):
            provider.validate_id_token("any-token")
