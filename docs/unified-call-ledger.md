# Unified Call Ledger

One row per authorized tool call: run, principal, actor, tenant, audience, tool,
outcome, rows, tokens, latency, ts, grant_jti, span_id. The answer to "who did
this, for whom, for which tenant, in which run, and how much did it cost".

Introduced by `establish-agent-principal-plane` (spec
`openspec/changes/establish-agent-principal-plane/specs/unified-call-ledger/`).

## Write path (never on the hot path)

```
call ──▶ gateway/resource service
             │  emit(LedgerEvent)  ── JSONL line + mirrored log
             ▼
      logs/audit/ledger_events.jsonl
             │  scripts/shipper (OUT of process, no gateway deps)
             ▼
      PostgreSQL ledger_events  (idempotent: PK (grant_jti, span_id))
```

- The gateway image needs **no** Postgres driver — the shipper is a separate
  program (`registry/ledger/shipper.py`, `--once` or continuous).
- `emit()` never raises: a ledger I/O failure cannot fail the call. Loss stays
  loud (mirrored log line) and shippable from container logs.
- Idempotent insert (`ON CONFLICT DO NOTHING`) means a resource service and the
  gateway can both emit the same call (shared `span_id`) and it collapses to one
  row — no double count.

## Budget pre-check

`budget` is a hard per-grant cap. Before `tools/call`, the gateway reads a fast
counter; over the cap → 402; counter store unreachable → 503 (fail-closed). A
grant with no `budget` inherits the tenant quota (never "unlimited").

The M32 counter is Mongo-backed (`registry/ledger/mongo_counter.py`) so every
gateway replica agrees; Mongo is already a hard dependency and the replay store
proves the fail-closed pattern. The counter is a **derived index** over the
ledger, reconciled against the PG aggregation at settle time — not a second
source of truth.

## Read face

```
GET /api/ledger/run/{run_id}                 per-principal totals for one run
GET /api/ledger/tenant/{tenant_id}?month=    one tenant's month
```

Aggregate views in `registry/ledger/schema.sql`: `ledger_by_run`,
`ledger_by_tenant_month`, `ledger_by_principal_tool` — all from the same base
rows, so they agree by construction.

## Settlement boundary

This ledger is **attribution**, not settlement. The wire ledger stays the
reconciliation authority (`wire-billing`). A difference is reported against the
wire ledger; this ledger never claims the authoritative monetary number.

## Retention

`registry/ledger/retention.py` snapshots aggregates older than the cutoff into
`ledger_retention_snapshot`, then deletes the old detail, then writes one
`ledger_retention_log` row — so "this period's detail was cleared by policy" is
provable, and post-clear period queries still answer. Runs from a scheduler, not
the gateway.
