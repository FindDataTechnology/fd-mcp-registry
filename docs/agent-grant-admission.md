# Agent Grant Admission

How an agent principal obtains and uses a delegation grant, so its calls are
attributable to `principal=agent` and `actor=<owner>` in the ledger — separate
from the human's own calls.

Introduced by `establish-agent-principal-plane` (task 6.3).

## Who is who

An agent registration carries `owner_principal_id` (a human). When the agent runs,
it requests a grant with `act = <owner id>`:

| field | user calling for self | agent acting for owner |
|---|---|---|
| grant `sub` | the user | the agent principal |
| grant `act` | absent | `{sub: <owner id>}` |
| ledger `principal` | the user | the agent |
| ledger `actor` | null | the human owner |

The ledger aggregates per-`principal`, so an agent's usage is one row and its
owner's is another — both under the same `tenant` and `run`.

## Getting a grant

```
POST /api/grants
Authorization: Bearer <console session or machine credential>
{
  "aud": "fd-cn-report",
  "run": "<run/turn id>",
  "scope": ["line:read"],
  "act": "<owner principal id>",     # only for agent principals; must equal owner
  "budget": {"calls": 500, "rows": 10000}
}
→ {"token": "...", "aud": "fd-cn-report", "exp": <epoch>}
```

The issuance endpoint enforces:
- the agent principal exists and is `active`;
- its tenant's `isolation_tier` resolves (else `tenant_tier_unresolved`);
- `act` equals the agent's `owner_principal_id` (else 403).

## Budget slicing (from the tenant plan)

The tenant's plan carries a monthly allowance. When an agent starts a run, take a
slice of the tenant's remaining allowance for `budget`, not the whole thing:

- one grant per `aud`; one run → N grants (one per downstream service);
- `budget.calls`/`budget.rows` cap this run only — the run never spends past them;
- a grant with no `budget` is bounded only by the tenant's own quota.

The gateway's budget gate (`grant_budget.py`) refuses `tools/call` once the run's
counter reaches the cap (402) and fails closed if the counter store is down (503).

## Revocation window

Revoking the agent (or its owner) takes effect within one grant TTL (≤1h). That
is the documented upper bound; the agent's next grant request after the revoke
fails immediately because the principal is no longer `active`.

## What the agent must NOT do

- Never present a grant to a service other than its `aud` (the line rejects a
  mismatched `aud`).
- Never reuse a grant across runs — `run` is per-turn; a new run gets a new grant
  so the ledger can group by run.
- Never hold the master seed or signing key; agents only ever receive grants.
