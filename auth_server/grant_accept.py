"""Grant acceptance on the auth server's /validate hop.

When a Bearer token is a delegation grant (Ed25519, single-``aud``, minted by the
registry), the auth server verifies it OFFLINE against the registry's JWKS and
turns it into a normal ``validation_result`` dict so the rest of /validate
(scope FGAC, MCP audit, mcp-proxy token mint) proceeds unchanged. This is the
"before" branch the change's design.puts at the head of the chain.

Scope: the grant carries an explicit ``scope`` list. We map it onto the caller's
``user_scopes`` and — because the grant itself is the authorization — we SKIP the
per-server scope FGAC (which keys on registry scope documents the grant bypasses).
The grant's own signature+aud+exp+scope are the authority; the downstream still
enforces tool-block lists and the preflight/budget gates.

Ships inert: ``GRANT_PLANE_ENABLED`` off => this module is never called.
"""

import logging
from typing import Any

import jwt

logger = logging.getLogger(__name__)

_GRANT_ISS = "mcp-gateway"


def looks_like_grant(token: str) -> bool:
    """Cheap prefilter: a grant is a JWT whose iss is the gateway.

    Non-grants (wgk- keys, provider JWTs) fail this fast and fall through to
    their existing branches, so the grant path never interferes with them.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 - not a decodable JWT
        return False
    return unverified.get("iss") == _GRANT_ISS


async def validate_grant_token(token: str) -> dict[str, Any]:
    """Verify a grant offline and return a validation_result-shaped dict.

    The verifier is constructed against the PUBLIC registry base so the JWKS URL
    matches what every downstream line also uses (mcp.finddatatech.cloud/.well-known/grant-jwks.json).
    Raises ValueError on any failure — the /validate caller maps that to a 401,
    the same as a bad patch key.

    ``aud`` is not known a priori here (the grant names the target service; the
    request URL does too). We verify against the JWKS for the grant's own aud and
    then require that the URL's server matches it in the caller's scope check; a
    mismatch is a verification failure, not a silent pass.
    """
    from registry.auth.grant import GrantVerificationError, verify_grant
    from registry.auth.grant_verify import JWKSUnavailable
    from registry.core.config import settings

    # Resolve the audience from the token itself for the JWKS fetch, then verify
    # it strictly. verify_grant enforces aud==expected, so we pass the token's
    # own aud — the request-level match is done by the caller against server_name.
    try:
        claims_unverified = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Grant is not a decodable JWT") from exc
    aud = claims_unverified.get("aud")
    if not aud:
        raise ValueError("Grant has no audience")

    base = _registry_base()
    if not base:
        raise ValueError("Grant verification unavailable: registry base not configured")

    from registry.auth.grant_verify import GrantVerifier

    verifier = GrantVerifier(jwks_url=f"{base.rstrip('/')}/.well-known/grant-jwks.json", aud=aud)
    try:
        claims = await verifier.verify(token)
    except JWKSUnavailable as exc:
        # Distinct from a bad grant: surface as an upstream problem, not a 401 on
        # the credential. /validate maps ValueError→401; we want 503 here so the
        # caller retries rather than re-mints. Use a sentinel exception.
        raise GrantUnavailable(str(exc)) from exc
    except GrantVerificationError as exc:
        raise ValueError(f"Invalid grant: {exc}") from exc

    scope = claims.get("scope") or []
    if not isinstance(scope, list):
        raise ValueError("Grant scope malformed")
    return {
        "valid": True,
        "username": claims.get("sub", ""),
        "client_id": "grant",
        "scopes": scope,
        "groups": [],
        "method": "grant",
        "server_name": None,
        "tool_name": None,
        # Carried so the mcp-proxy hop and the ledger can attribute the call:
        "grant_aud": aud,
        "tenant": claims.get("tenant"),
        "run": claims.get("run"),
        "grant_jti": claims.get("jti"),
        "actor": (claims.get("act") or {}).get("sub"),
    }


class GrantUnavailable(Exception):
    """JWKS/verification infra unreachable — distinct from an invalid grant."""


def grant_claims_from_request(request: Any) -> dict[str, Any] | None:
    """Return the grant's claims from the request's bearer, or None.

    The grant was already signature-verified at /validate on this same request;
    here we only need budget/run/jti for the counter, which the token's own
    ``exp`` bounds. We deliberately do NOT re-verify the signature on this hot
    path (that already happened one hop earlier).
    """
    try:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        token = auth[len("bearer "):].strip()
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001
        return None
    if claims.get("iss") != _GRANT_ISS:
        return None
    return claims


def _registry_base() -> str:
    import os

    from registry.core.config import settings

    for value in (getattr(settings, "registry_url", ""), os.environ.get("REGISTRY_URL", "")):
        if value:
            return value
    return ""
