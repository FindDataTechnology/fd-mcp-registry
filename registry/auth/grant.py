"""Delegation-grant signing: Ed25519 keys derived per audience, JWKS export,
and the shared offline-verify helper.

Design (see the change's design.md Decision 1-2):

- The gateway is the sole issuer. Signing is **asymmetric** (Ed25519) so a
  downstream line can verify OFFLINE — no per-request callback to the gateway or
  billing plane. The existing internal-token key (`registry/auth/internal.py`)
  is HS256 and deliberately NOT reused here: a symmetric key would mean every
  line holds something capable of minting tokens for every other line.
- One master seed exists operationally. Each audience's signing key is
  ``Ed25519(HKDF-SHA256(seed, info=aud))``. Derivation is one-way, so a leaked
  per-audience key never exposes the seed or another audience's key.
- ``kid = "<aud>:<generation>"``. ``generation`` lets a seed rotation publish a
  new set while the old set stays in the JWKS until the longest ``exp`` passes.

A grant is a JWT carrying ``iss/sub/act/aud/scope/tenant/run/budget/exp/iat/jti``
plus the standard ``kid`` header. ``aud`` is a single string.

Change: establish-agent-principal-plane (task group 2).
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

GRANT_ISSUER: str = "mcp-gateway"
GRANT_ALGORITHM: str = "EdDSA"
GRANT_MAX_TTL_SECONDS: int = 3600
GRANT_KEY_INFO_PREFIX: bytes = b"grant-signing-v1:"
GRANT_AUDIENCE_CLAIM: str = "aud"


class GrantConfigError(RuntimeError):
    """Master seed missing or malformed."""


class GrantVerificationError(Exception):
    """Signature / audience / expiry / scope check failed.

    Distinct from a service fault so a caller can tell "get a new grant" from
    "try again later".
    """


@dataclass(frozen=True)
class SigningKey:
    """One derived signing key for one audience, at one generation."""

    aud: str
    generation: int
    private_key: ed25519.Ed25519PrivateKey

    @property
    def kid(self) -> str:
        return f"{self.aud}:{self.generation}"


def _derive_private_key(seed: bytes, aud: str, generation: int) -> ed25519.Ed25519PrivateKey:
    info = GRANT_KEY_INFO_PREFIX + f"{aud}:{generation}".encode()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info)
    derived = hkdf.derive(seed)
    return ed25519.Ed25519PrivateKey.from_private_bytes(derived)


def _ed25519_public_jwk(public_key: ed25519.Ed25519PublicKey, kid: str) -> dict[str, Any]:
    raw = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    import base64

    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": kid, "use": "sig", "alg": "EdDSA"}


class GrantSigner:
    """Signs grants per audience and exports the matching JWKS."""

    def __init__(self, master_seed: bytes, generation: int = 1) -> None:
        if len(master_seed) < 32:
            raise GrantConfigError("grant master seed must be >= 32 bytes")
        self._seed = master_seed
        self._generation = generation
        self._cache: dict[str, SigningKey] = {}

    @classmethod
    def from_settings(cls) -> "GrantSigner":
        from registry.core.config import settings

        seed_b64 = getattr(settings, "grant_master_seed", "") or ""
        if not seed_b64:
            raise GrantConfigError(
                "GRANT_MASTER_SEED is not set; the grant plane cannot sign"
            )
        import base64

        try:
            seed = base64.b64decode(seed_b64)
        except Exception as exc:  # noqa: BLE001
            raise GrantConfigError(f"GRANT_MASTER_SEED is not valid base64: {exc}") from exc
        gen = int(getattr(settings, "grant_key_generation", 1) or 1)
        return cls(seed, generation=gen)

    def signing_key(self, aud: str) -> SigningKey:
        key = self._cache.get(aud)
        if key is None:
            key = SigningKey(
                aud=aud,
                generation=self._generation,
                private_key=_derive_private_key(self._seed, aud, self._generation),
            )
            self._cache[aud] = key
        return key

    def jwks(self, auds: list[str], *, include_previous: int = 1) -> dict[str, Any]:
        """Public verification keys for the given audiences.

        ``include_previous`` publishes the prior generation(s) too, so a seed
        rotation (generation bump) keeps old tokens verifiable until their
        ``exp`` passes. New tokens are always signed with the current kid.
        """
        keys = []
        for aud in auds:
            generations = [self._generation] + [
                self._generation - g for g in range(1, include_previous + 1)
            ]
            for gen in generations:
                if gen < 1:
                    continue
                priv = _derive_private_key(self._seed, aud, gen)
                keys.append(_ed25519_public_jwk(priv.public_key(), f"{aud}:{gen}"))
        return {"keys": keys}

    def sign(
        self,
        *,
        sub: str,
        aud: str,
        scope: list[str],
        tenant: str,
        run: str,
        budget: dict[str, Any] | None,
        ttl_seconds: int = GRANT_MAX_TTL_SECONDS,
        act: str | None = None,
        jti: str | None = None,
    ) -> str:
        if not aud:
            raise ValueError("aud is required (single audience)")
        if ttl_seconds <= 0 or ttl_seconds > GRANT_MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be in (0, {GRANT_MAX_TTL_SECONDS}]")
        sk = self.signing_key(aud)
        now = int(time.time())
        import uuid

        claims: dict[str, Any] = {
            "iss": GRANT_ISSUER,
            "sub": sub,
            "aud": aud,
            "scope": list(scope),
            "tenant": tenant,
            "run": run,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": jti or uuid.uuid4().hex,
        }
        if act:
            # RFC 8693 shape: act.sub is who the subject is acting for.
            claims["act"] = {"sub": act}
        if budget is not None:
            claims["budget"] = budget

        pem = sk.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return jwt.encode(claims, pem, algorithm=GRANT_ALGORITHM, headers={"kid": sk.kid})


def public_key_from_jwk(jwk: dict[str, Any]) -> ed25519.Ed25519PublicKey:
    import base64

    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise GrantVerificationError("unsupported key type in JWKS")
    x = jwk["x"]
    pad = "=" * (-len(x) % 4)
    raw = base64.urlsafe_b64decode(x + pad)
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


def verify_grant(
    token: str,
    *,
    jwks: dict[str, Any],
    expected_aud: str,
    required_scope: str | None = None,
) -> dict[str, Any]:
    """Offline verification: signature, aud, exp, and optional scope.

    Never contacts the issuer. Raises GrantVerificationError on any failure.
    """
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        raise GrantVerificationError(f"malformed token: {exc}") from exc
    kid = header.get("kid")
    if not kid:
        raise GrantVerificationError("token has no kid")
    matching = [k for k in jwks.get("keys", []) if k.get("kid") == kid]
    if not matching:
        raise GrantVerificationError(f"no verification key for kid {kid!r}")
    pub = public_key_from_jwk(matching[0])
    try:
        claims = jwt.decode(
            token,
            pub,
            algorithms=[GRANT_ALGORITHM],
            audience=expected_aud,
            options={"require": ["exp", "iat", "aud", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise GrantVerificationError("grant expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise GrantVerificationError("audience mismatch") from exc
    except jwt.InvalidSignatureError as exc:
        raise GrantVerificationError("invalid signature") from exc
    except jwt.InvalidTokenError as exc:
        raise GrantVerificationError(f"invalid token: {exc}") from exc

    if claims.get("iss") != GRANT_ISSUER:
        raise GrantVerificationError("unexpected issuer")
    if required_scope is not None and required_scope not in (claims.get("scope") or []):
        raise GrantVerificationError(f"missing scope {required_scope!r}")
    return claims
