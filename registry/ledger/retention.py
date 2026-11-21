"""Ledger retention: clear old detail rows, keep aggregates, leave evidence.

Spec ``unified-call-ledger``: detail expires per a configured retention; the
aggregates needed for billing/reconciliation MUST be persisted before detail is
cleared; the clear MUST be recorded so "this period's detail was removed by
policy" is provable, never a silent disappearance.

This module does the clear against the PG ledger and writes one retention-log row
per run. It is intended to run from a scheduler (CronJob/host unit), not the
gateway. Aggregate views (``ledger_by_tenant_month`` etc.) are materialized into
``ledger_retention_snapshot`` before deletion so post-clear queries still answer.

Change: establish-agent-principal-plane (task group 3).
"""

import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 400

_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS ledger_retention_snapshot (
    tenant_id  TEXT NOT NULL,
    month      TEXT NOT NULL,
    calls      BIGINT NOT NULL,
    rows_total BIGINT NOT NULL,
    tokens_total BIGINT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, month)
)
"""

_LOG_DDL = """
CREATE TABLE IF NOT EXISTS ledger_retention_log (
    id          BIGSERIAL PRIMARY KEY,
    cut_before  TIMESTAMPTZ NOT NULL,
    deleted     BIGINT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _dsn() -> str:
    return (os.getenv("LEDGER_PG_DSN") or "").strip()


def run_retention(*, retention_days: int | None = None, actor: str, reason: str) -> dict:
    """Snapshot aggregates older than the cutoff, then delete that detail.

    Returns {"cut_before", "deleted", "snapshotted"}. Idempotent: a second run
    with the same cutoff deletes nothing and snapshots nothing new.
    """
    import psycopg

    days = retention_days if retention_days is not None else int(
        os.getenv("LEDGER_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    )
    cut_before = datetime.now(UTC) - timedelta(days=days)

    with psycopg.connect(_dsn(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(_SNAPSHOT_DDL)
        cur.execute(_LOG_DDL)
        # Snapshot any (tenant, month) that has detail older than the cutoff and is
        # not yet snapshotted — aggregates survive the delete.
        cur.execute(
            """
            INSERT INTO ledger_retention_snapshot (tenant_id, month, calls, rows_total, tokens_total)
            SELECT tenant_id, to_char(ts,'YYYY-MM'), count(*),
                   coalesce(sum(rows_returned),0), coalesce(sum(tokens),0)
            FROM ledger_events
            WHERE ts < %s
            GROUP BY tenant_id, to_char(ts,'YYYY-MM')
            ON CONFLICT (tenant_id, month) DO NOTHING
            """,
            (cut_before,),
        )
        snapshotted = cur.rowcount
        cur.execute("DELETE FROM ledger_events WHERE ts < %s", (cut_before,))
        deleted = cur.rowcount
        cur.execute(
            "INSERT INTO ledger_retention_log (cut_before, deleted, actor, reason) "
            "VALUES (%s, %s, %s, %s)",
            (cut_before, deleted, actor, reason),
        )
        conn.commit()
    logger.info(
        "ledger retention: cut_before=%s deleted=%d snapshotted=%d",
        cut_before.isoformat(),
        deleted,
        snapshotted,
    )
    return {"cut_before": cut_before.isoformat(), "deleted": deleted, "snapshotted": snapshotted}
