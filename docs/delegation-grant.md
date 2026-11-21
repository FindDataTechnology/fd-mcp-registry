# Delegation Grant

A short-lived, single-audience signed token that says **who** is calling, **on
whose behalf**, **for which tenant/run**, and **how much** they may spend. The
gateway is the sole issuer; every downstream line verifies it **offline**.

Introduced by `establish-agent-principal-plane` (spec
`openspec/changes/establish-agent-principal-plane/specs/delegation-grant/`).

## Shape

```
iss  mcp-gateway
sub  <principal id>            who is acting
act  {sub: <owner id>}         present only when an agent acts for its owner
aud  <single service>          NEVER a list; one run → N tokens
scope [..]                     e.g. ["line:read"]
tenant <tenant id>
run   <run/turn id>            every call in one run shares this
budget {calls?, rows?, tokens?} optional per-grant cap
exp, iat, jti                 exp-iat <= 3600s
kid  <aud>:<generation>       header
```

`act` is the RFC 8693 delegation shape: `sub` is the executor, `act.sub` is the
beneficiary. Usage can be aggregated separately for each.

## Signing & key derivation

- Asymmetric **Ed25519** so downstream verifies without the issuer's secret.
- One master seed (`GRANT_MASTER_SEED`, base64, ≥32 bytes). Per-audience key =
  `Ed25519(HKDF-SHA256(seed, info="grant-signing-v1:<aud>:<gen>"))`.
- `kid = "<aud>:<gen>"`. Bump `GRANT_KEY_GENERATION` to rotate; old kids stay in
  the JWKS until the longest grant `exp` passes.
- Deliberately **not** the HS256 internal-token key (`registry/auth/internal.py`):
  a symmetric key can't be handed to verify-only consumers.

> None of this is reachable until `GRANT_PLANE_ENABLED=true` and a seed is set.

## API

```
POST /api/grants   {aud, run, scope?, tenant?, sub?, act?, budget?, ttl_seconds?}
                   → {token, aud, exp}; 409 tenant_tier_unresolved /
                     principal_unregistered / principal_not_active; 503 if no seed
GET  /api/jwks?aud=<aud>[&aud=...]   → public verification keys (no full dump)
```

`act` is only accepted for an `agent` principal, and only equal to its
`owner_principal_id` (else 400 / 403).

## Verifying downstream

Import only the verifier, never the signer:

```python
from registry.auth.grant_verify import GrantVerifier, JWKSUnavailable
from registry.auth.grant import GrantVerificationError

verifier = GrantVerifier(jwks_url="https://mcp.finddatatech.cloud/api/jwks", aud="fd-cn-report")
try:
    claims = await verifier.verify(bearer_token, required_scope="line:read")
except GrantVerificationError:   # → 401: get a new grant
    ...
except JWKSUnavailable:          # → 503: try again later (NOT an auth failure)
    ...
```

The JWKS is cached for `jwks_ttl` (default 300s); a verify result is never
trusted past the token's own `exp`. Revocation therefore takes effect within one
grant TTL (≤1h) — the documented upper bound, not a bug.
