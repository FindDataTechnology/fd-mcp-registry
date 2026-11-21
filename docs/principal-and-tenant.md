# Principal & Tenant Plane

A caller identity model so the gateway can answer "who is calling, on whose
behalf, and for which tenant" from the request itself instead of inferring it
from a key string.

Introduced by the change `establish-agent-principal-plane`
(finddata `openspec/changes/establish-agent-principal-plane`).

## Three principal types, one namespace

| type | what it is | owner |
|---|---|---|
| `user` | a human (IdP subject) | — |
| `agent` | an AI principal acting for a human | **required** `owner_principal_id` |
| `service` | a machine identity (CI, cron, peer) | — |

`type` is an explicit field, never inferred from an id prefix. It is fixed at
registration and cannot change.

## Status is the one revocation entry point

`active` / `suspended` / `revoked`. `revoked` is terminal — no code path moves
a revoked principal back. Suspending a principal cascades `suspended` to the
agents it owns; **restoring it does not restore them** — each agent is restored
by an explicit action.

## Tenants declare an isolation tier; there is no default

A tenant carries the two configs shared across its principals — `billing_binding`
and `entitlements`. Principals inherit both; a principal never holds a duplicate.

`isolation_tier` ∈ `shared` / `dedicated_cell` / `dedicated_fleet`. It decides
**where** a request lands, not whether the engine is multi-tenant-aware. A
tenant with no tier has its principals refused at grant issuance with
`tenant_tier_unresolved` — never defaulted to `shared`.

## API

```
POST /api/principals/tenants                upsert a tenant (admin)
GET  /api/principals/tenants/{id}           read a tenant
GET  /api/principals/tenants/{id}/tier      resolve tier (409 tenant_tier_unresolved if unset)
POST /api/principals                        register a principal (admin)
GET  /api/principals                        list principals (tenant-scoped; admin needs ?tenant_id)
POST /api/principals/{id}/status            transition status (admin)
```

Storage: MongoDB collections `principals` / `tenants` (same client as the rest
of the registry). Code: `registry/schemas/principal.py`,
`registry/services/principal_service.py`, `registry/api/principal_routes.py`.
