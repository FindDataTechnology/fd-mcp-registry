# Gateway Grant Branch

Where the delegation-grant plane plugs into the existing request path. Nothing
here changes behavior unless `GRANT_PLANE_ENABLED=true`.

Introduced by `establish-agent-principal-plane` (task group 4).

## The two hops

```
client ──▶ nginx ──▶ /validate (auth_server)  ──▶ /mcp-proxy/{server} (auth_server)
                      │                            │
                      │ grant branch               │ budget gate
                      ▼                            ▼
                   verify grant offline         read counter, 402/503
                   set validation_result         forward to upstream
```

### /validate — grant branch

Inserted in the Bearer-token block, **before** the self-signed/provider checks,
guarded by `GRANT_PLANE_ENABLED` and a cheap `iss == mcp-gateway` prefilter
(`grant_accept.looks_like_grant`). A non-grant never enters the branch, so patch
keys and provider JWTs are byte-identical to before.

- Verification is **offline** against the registry JWKS (`GrantVerifier`), no
  callback. Invalid/expired/wrong-aud → 401. JWKS unreachable → **503** (retry,
  not re-mint).
- On success the grant becomes a normal `validation_result` with
  `method="grant"` and the grant's `scope` as `user_scopes`.
- **Scope authority**: a grant is not in the registry scope documents, so the
  per-server scope FGAC is **bypassed** for grants. The grant's signed
  `aud == server_name` is enforced instead (a mismatched aud → 403). Blocked-tool
  lists and the preflight/budget gates still run.

### /mcp-proxy — budget gate

After scope authorization, before any outbound work (`grant_budget.py`):
`tools/call` is charged against the grant's `budget` via the Mongo counter. Over
cap → 402 `GRANT_EXHAUSTED`; counter store down → 503 `budget_unavailable`
(fail-closed). `initialize`/`tools/list` and a grant with no `budget` are
untouched (tenant-bounded only).

### Tenant-aware upstream

In `_attach_mcp_proxy_token`'s async caller, `_tenant_upstream_for` overrides the
upstream for a dedicated-tier grant caller when the registry entry carries
`metadata.tenant_upstreams: {<tenant_id>: <url>}`. `shared` tenants and non-grant
callers are untouched; a dedicated tenant without an override keeps the default
(its only existing instance). Best-effort — a lookup failure never blocks the
mint.

## Switch

| env | default | effect |
|---|---|---|
| `GRANT_PLANE_ENABLED` | false | master switch for issuance, JWKS, and the /validate branch |
| `GRANT_MASTER_SEED` | empty | base64 seed; required only when the plane is on |

Off ⇒ the branch is a single `if` that never executes; existing paths unchanged.
A missing seed with the plane **on** fails issuance with 503, not at import — the
registry still boots without it.

## Not billing on issuance

Minting a grant emits no ledger event (test `test_issuance_does_not_emit_ledger`).
Only an actual `tools/call` is charged.
