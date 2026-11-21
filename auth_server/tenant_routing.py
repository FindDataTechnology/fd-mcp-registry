"""Tenant-aware upstream selection at the /validate hop.

Upstream today is one URL per registered server, bound into the mcp-proxy token at
/validate. Multi-tenant routing needs a tenant to land on ITS instance of a
shared service. Rather than a second server store keyed by tenant, a tenant-
specific upstream lives as ``metadata.tenant_upstreams: {<tenant_id>: <url>}`` on
the existing registry entry.

Resolution (spec ``mcp-gateway-routing``):
- ``shared`` tenant  → the entry's normal upstream (NO override applied).
- ``dedicated_cell`` / ``dedicated_fleet`` tenant WITH an override for that
  server → the tenant's upstream.
- dedicated tenant WITHOUT an override → leave the default (a dedicated tenant
  that has not provisioned its instance still reaches the shared one; provisioning
  is what adds the override). This is NOT a silent fallback to a wrong instance:
  there is no other instance, so the default IS the only one.

A tenant whose tier cannot be resolved never reaches here (issuance refuses it).
This function is pure over (entry, tenant, tier) so it is unit-testable without a
live registry.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def select_upstream(
    *,
    default_upstream: str,
    entry_metadata: dict[str, Any] | None,
    tenant_id: str | None,
    isolation_tier: str | None,
) -> str:
    """Return the upstream URL this call should be bound to.

    Only a dedicated-tier tenant with an explicit override changes the default.
    Everything else is byte-identical to prior behavior.
    """
    if not tenant_id or not isolation_tier:
        return default_upstream
    if isolation_tier not in ("dedicated_cell", "dedicated_fleet"):
        return default_upstream
    overrides = (entry_metadata or {}).get("tenant_upstreams") or {}
    if not isinstance(overrides, dict):
        return default_upstream
    chosen = overrides.get(tenant_id)
    if not chosen:
        return default_upstream
    logger.info(
        "tenant upstream override: tenant=%s tier=%s server upstream -> %s",
        tenant_id,
        isolation_tier,
        chosen,
    )
    return chosen
