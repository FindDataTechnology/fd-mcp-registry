-- Unified call ledger — durable store (PostgreSQL).
--
-- Sink for the gateway's JSONL ledger events, batched by the standalone shipper
-- (scripts/ledger_shipper.py). Not on any service's hot path.
--
-- Idempotency: primary key (grant_jti, span_id). The gateway emits one line per
-- call; a resource service may also emit, sharing the same span_id, so the two
-- sides collapse to one row. A duplicate delivery is a no-op via ON CONFLICT.
--
-- Read faces: by run (one agent turn's calls), by tenant+month (billing window),
-- by principal+tool (who called what). These are index-backed; an operator can
-- build further rollups on top.
--
-- Change: establish-agent-principal-plane (task group 3).

CREATE TABLE IF NOT EXISTS ledger_events (
    grant_jti    TEXT        NOT NULL,
    span_id      TEXT        NOT NULL,
    run_id       TEXT        NOT NULL,
    principal_id TEXT        NOT NULL,
    actor_id     TEXT,
    tenant_id    TEXT        NOT NULL,
    audience     TEXT        NOT NULL,
    tool         TEXT        NOT NULL,
    outcome      TEXT        NOT NULL,
    rows_returned INTEGER,
    tokens       INTEGER,
    latency_ms   INTEGER,
    source       TEXT        NOT NULL DEFAULT 'gateway',
    ts           TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grant_jti, span_id)
);

CREATE INDEX IF NOT EXISTS ix_ledger_run       ON ledger_events (run_id, ts);
CREATE INDEX IF NOT EXISTS ix_ledger_tenant    ON ledger_events (tenant_id, ts);
CREATE INDEX IF NOT EXISTS ix_ledger_principal ON ledger_events (principal_id, audience, ts);

-- Aggregate views the read face and the budget/结算 layer consume.

CREATE OR REPLACE VIEW ledger_by_run AS
SELECT run_id,
       tenant_id,
       principal_id,
       actor_id,
       count(*)                              AS calls,
       coalesce(sum(rows_returned), 0)       AS rows_total,
       coalesce(sum(tokens), 0)              AS tokens_total,
       min(ts)                               AS started_at,
       max(ts)                               AS ended_at
FROM ledger_events
GROUP BY run_id, tenant_id, principal_id, actor_id;

CREATE OR REPLACE VIEW ledger_by_tenant_month AS
SELECT tenant_id,
       to_char(ts, 'YYYY-MM')                AS month,
       count(*)                              AS calls,
       coalesce(sum(rows_returned), 0)       AS rows_total,
       coalesce(sum(tokens), 0)              AS tokens_total
FROM ledger_events
GROUP BY tenant_id, to_char(ts, 'YYYY-MM');

CREATE OR REPLACE VIEW ledger_by_principal_tool AS
SELECT principal_id,
       audience,
       tool,
       count(*)                              AS calls,
       coalesce(sum(rows_returned), 0)       AS rows_total
FROM ledger_events
GROUP BY principal_id, audience, tool;
