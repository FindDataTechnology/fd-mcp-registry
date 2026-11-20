"""A2A agent backend addressing: is the backend a real host, or the gateway itself?

WHY THIS EXISTS (2026-11-21, production):
    Every A2A agent in this deployment had ``proxy_pass_url = None`` while its
    advertised ``url`` was ``https://mcp.finddatatech.cloud/agent/<path>/`` — the
    gateway's OWN public entry. The generator's fallback

        backend_url = proxy_pass_url or agent.url

    therefore pointed the backend at the gateway. A forwarded request carries
    ``Authorization`` into ``/validate``, passes, and is proxied to the gateway's
    own public entry — where it starts over. A self-referential hop.

    The symptom is the worst kind: the facade gets a plausible JSON-RPC answer,
    the registry's health check is green (it probes the gateway, which is always
    up), and the request never reaches the executor. Measured end to end: the
    runner logged the A2A method ZERO times while the caller saw success.

THREE CLASSES, NOT ONE BOOLEAN — each has a different remedy:
    ``ok``        a reachable, non-gateway host: usable.
    ``missing``   no backend address at all: the configuration was never finished.
    ``self_loop`` the backend IS the gateway: a known value filled in wrong.
Collapsing them into one flag sends an operator to the wrong layer.

THE JUDGMENT IS A HOSTNAME COMPARISON, NOT A PREFIX ONE:
    ``https://gw/agent/x`` and ``https://gw/agent/y`` share no prefix, yet both
    are the gateway. Comparing hosts is the only shape that cannot miss.

AND IT MUST NOT CONSULT ``health_status``:
    the health probe is itself built from the same (broken) fallback — it probes
    the gateway, which answers 200 forever. Using it to judge self-loop is
    circular. Everything here is derived from the addresses alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

BACKEND_OK = "ok"
BACKEND_MISSING = "missing"
BACKEND_SELF_LOOP = "self_loop"

#: Why a non-``ok`` verdict was reached, in words an operator can act on.
REASONS = {
    BACKEND_MISSING: "no backend address (neither proxy_pass_url nor url is set)",
    BACKEND_SELF_LOOP: (
        "backend points at the gateway itself — a forwarded request re-enters "
        "this gateway instead of reaching the agent's executor; set proxy_pass_url "
        "to the runner's agent address"
    ),
}


@dataclass(frozen=True)
class BackendVerdict:
    """The judged backend address plus why it got that verdict."""

    status: str
    url: str | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.status == BACKEND_OK


def _host_of(url: object) -> str | None:
    """Lower-cased ``host[:port]`` of a URL string, or None when absent/unparseable.

    THE PORT IS PART OF THE IDENTITY. Two agents on one machine differ only by
    port (the runner publishes 8891..8922 on the same host); judging by hostname
    alone would call every one of them "the gateway" whenever the gateway shares
    that host — measured: a fixture at ``localhost:9000`` versus a gateway at
    ``localhost:8000`` was falsely flagged as a self-loop.

    The default port for the scheme is normalized away so ``https://gw`` and
    ``https://gw:443`` compare equal.

    Deliberately does NOT resolve DNS: the judgment must work for a host that
    does not resolve (that is exactly what a self-loop looks like from the
    outside), and a string compare needs no network.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None
    port = parsed.port
    if port is not None:
        default = {"http": 80, "https": 443}.get((parsed.scheme or "").lower())
        if port != default:
            return f"{host}:{port}"
    return host


def gateway_hosts(registry_url: str | None, extra: Iterable[str] | None = None) -> frozenset[str]:
    """The set of hostnames that mean "this gateway".

    One primary (``settings.registry_url``) plus any declared aliases. A set, not
    a single value: a deployment can be reachable under more than one name, and
    judging only the primary would let a self-loop in through an alias.
    """
    hosts: set[str] = set()
    primary = _host_of(registry_url)
    if primary:
        hosts.add(primary)
    for alias in extra or ():
        host = _host_of(alias)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def resolve_backend_address(
    *,
    proxy_pass_url: object,
    url: object,
    gateway_hosts: frozenset[str],
) -> BackendVerdict:
    """Pick the backend address and say whether it is usable.

    Order matches the generator and the health probe, on purpose — three call
    sites reading the same rule is what keeps "health says fine" and "the route
    works" from disagreeing (this codebase already has the sibling bug on record
    as issue #1724).

    The ``proxy_pass_url or url`` fallback is KEPT: it exists for agents
    registered before reverse-proxy mode, whose ``url`` IS their real backend.
    What is added is the precondition that the fallback only applies when that
    ``url`` is not the gateway — which is decidable without knowing the history.
    """
    candidate = (proxy_pass_url or url or "") if isinstance(proxy_pass_url or url, str) else ""
    chosen = candidate.strip().rstrip("/")
    if not chosen:
        return BackendVerdict(BACKEND_MISSING, None, REASONS[BACKEND_MISSING])

    host = _host_of(chosen)
    if host and host in gateway_hosts:
        return BackendVerdict(BACKEND_SELF_LOOP, chosen, REASONS[BACKEND_SELF_LOOP])

    return BackendVerdict(BACKEND_OK, chosen, None)
