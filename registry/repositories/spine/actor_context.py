"""Acting-identity passthrough for spine write-gate forwarding.

Every spine write MUST name the acting identity (the gate returns 401
``SUPPLY_UNIDENTIFIED_WRITE`` otherwise), but the repository ABC write
methods carry no actor parameter. This module bridges the gap with a
``contextvars.ContextVar``: the auth layer stamps the resolved identity
(user email/username, M2M client id, or internal service principal) and the
spine repositories read it back when they forward a write.

Contextvars are the right carrier here: FastAPI/Starlette propagate the
caller's context into the request handler, and each request runs in its own
context, so concurrent requests cannot cross-contaminate identities.

A *default actor* can be installed for process-owned background writes (the
health-probe tool-snapshot refresh is the registry itself acting, not a
human): ``install_default_actor`` pins a fallback that ``current_actor()``
returns when no request-scoped identity is set. The fallback is honest — the
gate still audits ``source="facade"`` with the service principal named.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

logger = logging.getLogger(__name__)

_acting_actor: ContextVar[str] = ContextVar("spine_acting_actor", default="")
_default_actor: str = "registry-service"


def set_acting_actor(actor: str) -> Token[str]:
    """Stamp the acting identity for the current request context.

    Args:
        actor: Human email/username, M2M client id, or service principal.

    Returns:
        Reset token — pass to :func:`reset_acting_actor` on teardown.
    """
    return _acting_actor.set((actor or "").strip())


def reset_acting_actor(token: Token[str]) -> None:
    """Restore the previous actor (used by ``try/finally`` wrappers)."""
    _acting_actor.reset(token)


def install_default_actor(actor: str) -> None:
    """Set the process-level fallback actor for background writes."""
    global _default_actor
    if actor and actor.strip():
        _default_actor = actor.strip()


def current_actor() -> str:
    """Resolve the acting identity for a forwarded write.

    Order: request-scoped identity (set by the auth layer) first, then the
    process-level default actor. Never returns an empty string — the write
    gate rejects unidentified writes with 401 and an honest service principal
    is better than a forwarded empty actor.
    """
    scoped = _acting_actor.get().strip()
    if scoped:
        return scoped
    return _default_actor
