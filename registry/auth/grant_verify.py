"""Downstream-side grant verification for line services.

A line service does NOT import the signer — only this verifier. It fetches the
gateway's JWKS (for its own ``aud``), then verifies grants OFFLINE: signature,
``aud``, ``exp``, ``iss``, and optional scope. No per-request call to the gateway
or billing plane.

The JWKS is cached with a TTL bounded by ``exp`` semantics: a verification result
is never trusted past the token's own ``exp`` (handled by ``jwt.decode``), and
the cached key set is refreshed at most every ``jwks_ttl`` seconds so a seed
rotation publishes new kids quickly.

Failure modes are distinct (spec ``delegation-grant``): a verification failure is
``GrantVerificationError`` (→ 401), while a JWKS fetch failure is
``JWKSUnavailable`` (→ 503, "try again"), never collapsed into an auth failure.

Change: establish-agent-principal-plane (task group 2, verify library).
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from registry.auth.grant import GrantVerificationError, verify_grant

logger = logging.getLogger(__name__)


class JWKSUnavailable(Exception):
    """The verification key set could not be fetched. Distinct from a bad grant."""


@dataclass
class _CachedJWKS:
    keys: dict[str, Any]
    fetched_at: float


class GrantVerifier:
    """Fetches and caches JWKS for one audience, then verifies grants."""

    def __init__(
        self,
        *,
        jwks_url: str,
        aud: str,
        jwks_ttl: float = 300.0,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._jwks_url = jwks_url
        self._aud = aud
        self._jwks_ttl = jwks_ttl
        self._timeout = timeout
        self._transport = transport
        self._cache: _CachedJWKS | None = None
        self._cached_aud_list: tuple[str, ...] = (aud,)

    def _cache_valid(self) -> bool:
        if self._cache is None:
            return False
        return (time.monotonic() - self._cache.fetched_at) < self._jwks_ttl

    async def _fetch_jwks(self) -> dict[str, Any]:
        params = {"aud": self._aud}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.get(self._jwks_url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise JWKSUnavailable(f"could not fetch JWKS from {self._jwks_url}: {exc}") from exc
        if not isinstance(data, dict) or "keys" not in data:
            raise JWKSUnavailable("JWKS response is not a key set")
        return data

    async def jwks(self) -> dict[str, Any]:
        if self._cache_valid() and self._cache is not None:
            return self._cache.keys
        keys = await self._fetch_jwks()
        self._cache = _CachedJWKS(keys=keys, fetched_at=time.monotonic())
        return keys

    async def verify(
        self,
        token: str,
        *,
        required_scope: str | None = None,
        consume_jti: bool = False,
    ) -> dict[str, Any]:
        """Offline-verify a grant. Raises GrantVerificationError or JWKSUnavailable.

        ``consume_jti=True`` additionally enforces single-use for one-time
        actions by recording the token's ``jti`` (reuses the internal-token
        replay store, which is cluster-wide and fail-closed). Ordinary calls
        leave it off — a grant legitimately backs many calls in one run.
        """
        keys = await self.jwks()
        claims = verify_grant(
            token, jwks=keys, expected_aud=self._aud, required_scope=required_scope
        )
        if consume_jti:
            jti = claims.get("jti")
            if not jti:
                raise GrantVerificationError("one-time grant has no jti")
            from registry.auth.internal_replay_store import consume_jti as _consume

            ttl = max(1, int(claims.get("exp", 0)) - int(claims.get("iat", 0)))
            ok = await _consume(jti, ttl_seconds=ttl)
            if not ok:
                raise GrantVerificationError("grant jti replay or replay store unavailable")
        return claims

    def reset_cache(self) -> None:
        self._cache = None
