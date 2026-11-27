"""Ledger read face: run/tenant aggregations from the durable store.

The durable ledger is the Mongo ``ledger_events`` collection (see
``registry/ledger/events.py`` — the write path was moved to Mongo so the record
survives a pod restart on a volume; the PG+shipper design is retired). This read
face therefore aggregates the same Mongo collection it is written to, so the
numbers reported here are the numbers recorded.

These endpoints are the "who did what, how much" surface — NOT the settlement
authority. Settlement stays in the wire ledger (spec ``wire-billing``); this
surface only provides attribution. When Mongo is unreachable the endpoints
report unavailable (503) rather than fabricate zeros, matching the budget gate's
fail-closed posture.

Change: establish-agent-principal-plane (task group 3; read/write parity fix
so the aggregation views serve the store the calls actually land in).
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from registry.auth.dependencies import nginx_proxied_auth
from registry.ledger.events import MONGO_COLLECTION
from registry.repositories.documentdb.client import get_documentdb_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ledger", tags=["Ledger"])


async def _collection():
    """Return the Mongo ledger collection, or 503 if the store is unreachable."""
    try:
        db = await get_documentdb_client()
    except Exception as exc:  # noqa: BLE001 - auth/attribution surface fails closed
        logger.error("ledger read face unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ledger read face unavailable: ledger store unreachable",
        ) from exc
    return db[MONGO_COLLECTION]


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    """Parse ``YYYY-MM`` into a half-open [start, next-month) UTC window."""
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"month must be YYYY-MM, got {month!r}",
        ) from exc
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


@router.get("/run/{run_id}")
async def ledger_for_run(
    run_id: str,
    user_context: Any = Depends(nginx_proxied_auth),
) -> dict:
    """All calls in one run, grouped by principal/actor, plus totals."""
    coll = await _collection()
    pipeline = [
        {"$match": {"run": run_id}},
        {
            "$group": {
                "_id": {
                    "principal": "$principal",
                    "actor": "$actor",
                    "tenant": "$tenant",
                },
                "calls": {"$sum": 1},
                "rows": {"$sum": {"$ifNull": ["$rows", 0]}},
                "tokens": {"$sum": {"$ifNull": ["$tokens", 0]}},
            }
        },
        {"$sort": {"_id.principal": 1}},
    ]
    groups = []
    async for g in coll.aggregate(pipeline):
        groups.append(
            {
                "principal": g["_id"].get("principal"),
                "actor": g["_id"].get("actor"),
                "tenant": g["_id"].get("tenant"),
                "calls": g["calls"],
                "rows": g["rows"],
                "tokens": g["tokens"],
            }
        )
    return {"run_id": run_id, "groups": groups}


@router.get("/tenant/{tenant_id}")
async def ledger_for_tenant(
    tenant_id: str,
    month: str = Query(..., description="YYYY-MM"),
    user_context: Any = Depends(nginx_proxied_auth),
) -> dict:
    """One tenant's calls/rows/tokens for a calendar month."""
    start, end = _month_bounds(month)
    coll = await _collection()
    pipeline = [
        {"$match": {"tenant": tenant_id, "ts": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": None,
                "calls": {"$sum": 1},
                "rows": {"$sum": {"$ifNull": ["$rows", 0]}},
                "tokens": {"$sum": {"$ifNull": ["$tokens", 0]}},
            }
        },
    ]
    totals = {"calls": 0, "rows": 0, "tokens": 0}
    async for g in coll.aggregate(pipeline):
        totals = {"calls": g["calls"], "rows": g["rows"], "tokens": g["tokens"]}
    return {"tenant_id": tenant_id, "month": month, **totals}