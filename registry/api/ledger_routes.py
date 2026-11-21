"""Ledger read face: run/tenant/principal aggregations from the durable store.

These endpoints read the PG ledger (via the shipper's target DB). They are the
"who did what, how much" surface — NOT the settlement authority. Settlement
stays in the wire ledger (spec ``wire-billing``); this surface only provides
attribution. When PG is not configured the endpoints report unavailable (503)
rather than fabricate zeros.

Three aggregations, matching the three views in schema.sql. They are index-backed
and agree by construction (same base rows).

Change: establish-agent-principal-plane (task group 3).
"""

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from registry.auth.dependencies import nginx_proxied_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ledger", tags=["Ledger"])


def _pg_dsn() -> str:
    return (os.getenv("LEDGER_PG_DSN") or "").strip()


def _connect():  # noqa: ANN202 - returns a psycopg connection
    import psycopg  # local import: driver only needed when this route runs

    return psycopg.connect(_pg_dsn())


def _require_dsn() -> None:
    if not _pg_dsn():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ledger read face unavailable: LEDGER_PG_DSN not configured",
        )


@router.get("/run/{run_id}")
async def ledger_for_run(
    run_id: str,
    user_context: Any = Depends(nginx_proxied_auth),
) -> dict:
    """All calls in one run, plus totals. Read-only attribution."""
    _require_dsn()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT principal_id, actor_id, tenant_id, count(*), "
            "coalesce(sum(rows_returned),0), coalesce(sum(tokens),0) "
            "FROM ledger_events WHERE run_id = %s "
            "GROUP BY principal_id, actor_id, tenant_id",
            (run_id,),
        )
        rows = cur.fetchall()
    return {
        "run_id": run_id,
        "groups": [
            {
                "principal": r[0],
                "actor": r[1],
                "tenant": r[2],
                "calls": r[3],
                "rows": r[4],
                "tokens": r[5],
            }
            for r in rows
        ],
    }


@router.get("/tenant/{tenant_id}")
async def ledger_for_tenant(
    tenant_id: str,
    month: str = Query(..., description="YYYY-MM"),
    user_context: Any = Depends(nginx_proxied_auth),
) -> dict:
    _require_dsn()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(rows_returned),0), coalesce(sum(tokens),0) "
            "FROM ledger_events WHERE tenant_id = %s AND to_char(ts,'YYYY-MM') = %s",
            (tenant_id, month),
        )
        calls, rows_total, tokens_total = cur.fetchone()
    return {
        "tenant_id": tenant_id,
        "month": month,
        "calls": calls,
        "rows": rows_total,
        "tokens": tokens_total,
    }
