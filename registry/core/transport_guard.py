"""Normalize a server entry's ``supported_transports`` declaration.

(fix-registry-probe-and-selfreport task 1.2 / A0 §4)

``dict.get("supported_transports", default)`` does NOT apply the default when
the key exists with an explicit ``null`` value — the readsite then handed
``None`` straight into ``in`` membership checks, which raised ``TypeError``,
flipped the worker's health verdict to unhealthy, had nginx comment the
location out, and surfaced as a gateway 405 (the 2026-11-04 lex-ops incident;
A0's 2026-11-07 sweep found the same live disease on wire-ops and
wanxing-ops). Every read of this field must go through :func:`normalize`.

Discipline (design D1/D2 of the change):

- ``None`` return means "no usable declaration" — the caller renders an
  honest degraded verdict (``UNHEALTHY_NO_TRANSPORT_DECLARATION``), never a
  silent ``streamable-http`` substitution (that would mask real config
  errors) and never an exception that breaks route generation.
- A bare string (``"streamable-http"`` as written by
  ``register_mcp_gateway.py``) is accepted as a one-element list during the
  transition window, with a warning telling the writer to normalize — the
  warning fires once per call, and the string form MUST eventually converge
  to a list at the write side.
- Any other non-list shape (dict/int/…) is unusable → ``None``.
- The input dict is never mutated.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_VALID_TRANSPORTS = frozenset({"streamable-http", "sse"})


def normalize_transports(server_info: dict[str, Any] | None) -> list[str] | None:
    """Return the entry's transports as a list, or ``None`` when unusable.

    Never raises, never mutates ``server_info``. A ``None`` return is the
    caller's signal to degrade honestly (no transport declaration) rather
    than guess a transport.
    """
    if not isinstance(server_info, dict):
        return None
    raw = server_info.get("supported_transports")
    if raw is None:
        # Key missing AND key-present-with-null land here: both mean the
        # entry never declared a usable transport list.
        return None
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            logger.warning(
                "supported_transports is an empty string; treating as no declaration "
                "(write side should store a JSON array)"
            )
            return None
        logger.warning(
            "supported_transports is a bare string %r; accepting as a one-element "
            "list during the transition window — the write side should normalize "
            "to a JSON array",
            value,
        )
        return [value]
    if isinstance(raw, list):
        normalized: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                logger.warning(
                    "supported_transports carries a non-string item %r; ignoring it",
                    item,
                )
                continue
            value = item.strip()
            if not value:
                continue
            if value not in _VALID_TRANSPORTS:
                logger.warning(
                    "supported_transports carries unknown transport %r; keeping it "
                    "for probe attempts but it will not match known transports",
                    value,
                )
            normalized.append(value)
        if not normalized:
            return None
        return normalized
    logger.warning(
        "supported_transports has unusable type %s; treating as no declaration",
        type(raw).__name__,
    )
    return None
