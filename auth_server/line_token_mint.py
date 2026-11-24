"""Mint a per-line 门K line token for the authenticated caller.

The wire line (and peers built on the same family) accepts a short-lived ES256
JWT — ``aud = source = <line>``, issued by this gateway
(``https://mcp.finddatatech.cloud``), verified offline against the line's public
key. wire-api already trusts this issuer (``FD_WIRE_LINE_TOKEN_ISSUER``); the
token's ``sub`` maps to the line's customer record (``Customer.logto_sub``).
Because the platform and the line share one Logto tenant, the gateway's
resolved user ``sub`` IS that ``sub`` — no identity translation.

This gateway can therefore mint the token itself for an authenticated caller,
so a virtual face can call a pass-through line backend through this gateway
alone: no paas line-proxy call-out, no IdP token exchange, no relaying the
caller's gateway token (which the line rejects).

Security: the ``sub`` is the /validate-verified principal (never a client
field), ``aud``/``source`` are pinned to the target line, ``exp`` ≤ 600s, and
the private key lives only in this process. The minted token is a bearer line
credential — it must never be logged.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import jwt as pyjwt

logger = logging.getLogger(__name__)

#: Issuer every line token carries. Matches wire-api's FD_WIRE_LINE_TOKEN_ISSUER.
LINE_TOKEN_ISSUER = "https://mcp.finddatatech.cloud"

#: Short lived by contract (wire-api enforces <= this).
LINE_TOKEN_TTL_SECONDS = 600

#: Scope claim the family expects.
LINE_TOKEN_SCOPE = "line:read"


class LineTokenKeyMissing(RuntimeError):
    """No signing key configured for the requested line (fail closed)."""


def _env_key_name(line: str, suffix: str) -> str:
    return f"LINE_TOKEN_{suffix}_{line.upper().replace('-', '_')}"


def line_signing_material(line: str) -> tuple[str, str]:
    """Return ``(private_key_pem, kid)`` for ``line`` from the environment.

    Raises:
        LineTokenKeyMissing: when the private key is absent — callers MUST fail
            closed rather than fall back to relaying the caller's credential.
    """
    name = (line or "").strip().lower()
    if not name:
        raise LineTokenKeyMissing("empty line")
    private_key = os.environ.get(_env_key_name(name, "KEY"), "").strip()
    if not private_key:
        raise LineTokenKeyMissing(
            f"no signing key for line {name!r} ({_env_key_name(name, 'KEY')})"
        )
    kid = os.environ.get(_env_key_name(name, "KID"), "").strip() or f"{name}-1"
    return private_key, kid


def mint_line_token(
    *,
    line: str,
    subject: str,
    email: str | None = None,
    ttl_seconds: int = LINE_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a line token for ``subject`` on ``line``.

    Args:
        line: Target line (``aud``/``source``), e.g. ``wire``.
        subject: The /validate-verified caller identity (the line's ``logto_sub``).
        email: Optional caller email claim.
        ttl_seconds: Lifetime; clamped to ``[1, LINE_TOKEN_TTL_SECONDS]``.
        now: Clock override (tests); default wall clock.

    Returns:
        The compact ES256 JWT.

    Raises:
        LineTokenKeyMissing: no key for the line.
        ValueError: empty subject/line.
    """
    name = (line or "").strip().lower()
    sub = (subject or "").strip()
    if not name:
        raise ValueError("line is required")
    if not sub:
        raise ValueError("subject is required")
    private_key, kid = line_signing_material(name)

    iat = int(now if now is not None else time.time())
    ttl = max(1, min(int(ttl_seconds or LINE_TOKEN_TTL_SECONDS), LINE_TOKEN_TTL_SECONDS))
    claims: dict = {
        "iss": LINE_TOKEN_ISSUER,
        "sub": sub,
        "aud": name,
        # source marks which line this ticket was minted for (audit + the line
        # rejects tickets minted for another line even if signature/aud pass).
        "source": name,
        "scope": LINE_TOKEN_SCOPE,
        "iat": iat,
        "exp": iat + ttl,
        "jti": str(uuid.uuid4()),
    }
    if email:
        claims["email"] = str(email)
    headers = {"kid": kid, "typ": "JWT"}
    return pyjwt.encode(claims, private_key, algorithm="ES256", headers=headers)
