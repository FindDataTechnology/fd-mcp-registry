# PR draft — branch `logto-support` (9 commits on top of upstream/main)

Compare URL (ready to open):
https://github.com/agentic-community/mcp-gateway-registry/compare/main...law-ai-official:mcp-gateway-registry:logto-support

---

## Title

feat(auth): Logto identity provider support (OAuth login + IAM role/group management)

## Summary

Adds `AUTH_PROVIDER=logto`:

- **`auth_server/providers/logto.py`** — full `AuthProvider` implementation for a
  self-hosted Logto: OIDC discovery, authorization-code login with state/nonce,
  logout, and JWT validation via the IdP's JWKS with groups read from token claims.
  Registers the factory branch alongside keycloak/auth0/cognito/entra/okta/pingfederate.
- **Scope fixes learned the hard way** (each its own commit):
  - first-party apps must NOT request `profile`/`email` — Logto rejects them and
    returns full userinfo anyway;
  - the reserved scope `all` is what makes `/oidc/me` include username + roles.
- **nginx templates** — `/oauth2/login/logto` + `/oauth2/callback/logto` location
  blocks in both templates; they must live outside the keycloak conditional region
  or the rendered template strips them.
- **`registry/utils/logto_admin.py` + LogtoIAMManager** — the IAMManager protocol
  mapped onto Logto roles (groups == roles), via the Management API with a
  token-cached `client_credentials` client (resource + `scope=all` both mandatory),
  401 refresh-retry, and paging. Fixes: with `AUTH_PROVIDER=logto` the IAM routes
  previously fell back to `KeycloakIAMManager` → DNS failure → groups list 502.
  Reuses the deployment's existing `LOGTO_M2M_*` credentials by default;
  `LOGTO_MANAGEMENT_M2M_*` are optional dedicated overrides.

## Verification

- Running in production since 2026-08 (login, JWT validation, roles-as-groups,
  Management API group sync — all verified live against a self-hosted Logto).
- Tests: 9 new unit tests (`tests/unit/test_logto_iam_manager.py`: mapping shapes,
  diff logic, payloads, factory). Full unit suite on this branch:
  **7363 passed, 34 skipped** (67% coverage, above the 35% gate).

## Configuration

`LOGTO_URL` / `LOGTO_EXTERNAL_URL` (localhost defaults), `LOGTO_CLIENT_ID/SECRET`,
`LOGTO_M2M_CLIENT_ID/SECRET`, optional `LOGTO_MANAGEMENT_M2M_*` and
`LOGTO_MANAGEMENT_RESOURCE` (default: Logto's fixed `https://default.logto.app/api`).
Wired into `docker-compose.yml`, `docker-compose.prebuilt.yml`, and
`auth_server/oauth2_providers.yml`.

---

# Optional issue draft (open before the PR if maintainers prefer a proposal first)

Title: Add Logto as an auth provider (AUTH_PROVIDER=logto)

Body: We run a self-hosted Logto (https://github.com/logto-io/logto) in front of
mcp-gateway-registry and had to patch the auth server locally: a Logto
AuthProvider, a factory branch (the IAM routes otherwise fall back to the
Keycloak manager and 502), nginx location blocks for the login/callback routes,
and a LogtoIAMManager that maps registry groups onto Logto roles via the
Management API. The work is production-verified since 2026-08. Would a PR adding
this upstream be welcome? Happy to split it into provider / IAM parts if that
suits review better.
