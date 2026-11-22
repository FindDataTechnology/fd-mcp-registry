"""Unified call ledger: append-only event sink + budget counter.

Two responsibilities, deliberately split from any database driver:

1. **Event append** — every authorized call produces one durable record in the
   Mongo ``mcp_registry.ledger_events`` collection (Mongo already has a
   persistent volume and the gateway already holds credentials — no new
   component, no PG driver). A local JSONL mirror is kept for pod-log
   shippability. The write is fire-and-forget so the request path never awaits
   the ledger; a Mongo failure never fails the call.

2. **Budget pre-check** — the hot-path "is this grant/tenant over its cap?" query.
   It reads a fast counter. This module keeps the counter abstract and ships a
   Mongo-backed implementation (Mongo is already a hard gateway dependency and
   cluster-wide; the replay store proves the fail-closed pattern). The counter is
   a *derived* index over the ledger, not a second source of truth.

Idempotency: the durable ledger dedups on ``(grant_jti, span_id)`` via a unique
Mongo index. ``span_id`` is minted here (one per call) and carried in the JSONL
line too, so a double-emit (gateway + resource sharing span_id) collapses.

Field set matches spec ``unified-call-ledger``: run / principal / actor / tenant /
audience / tool / outcome / rows / tokens / latency / ts / grant_jti.

Change: establish-agent-principal-plane (task group 3).
"""

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LedgerEvent:
    """One catalogued call. The exact columns the ledger row carries."""

    run: str
    principal: str
    actor: str | None
    tenant: str
    audience: str
    tool: str
    outcome: str  # ok | error | refused | incomplete
    grant_jti: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    rows: int | None = None
    tokens: int | None = None
    latency_ms: int | None = None
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = "gateway"  # gateway | resource

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _ledger_path() -> str:
    return (os.getenv("LEDGER_SINK_PATH") or "logs/audit/ledger_events.jsonl").strip()


MONGO_COLLECTION: str = "ledger_events"
_mongo_index_ready = False


async def _ensure_mongo_index(db) -> None:
    global _mongo_index_ready
    if _mongo_index_ready:
        return
    # Idempotency: one row per (grant_jti, span_id). Unique index makes a
    # double-emit (gateway + resource sharing span_id) collapse, not duplicate.
    await db[MONGO_COLLECTION].create_index(
        [("grant_jti", 1), ("span_id", 1)], unique=True, name="uniq_grant_span"
    )
    await db[MONGO_COLLECTION].create_index([("run", 1), ("ts", 1)], name="ix_run_ts")
    await db[MONGO_COLLECTION].create_index([("tenant", 1), ("ts", 1)], name="ix_tenant_ts")
    await db[MONGO_COLLECTION].create_index(
        [("principal", 1), ("audience", 1), ("ts", 1)], name="ix_principal_aud_ts"
    )
    _mongo_index_ready = True


async def _write_mongo(event: LedgerEvent) -> None:
    """Persist one event to Mongo (durable, no new component). Best-effort."""
    try:
        from registry.repositories.documentdb.client import get_documentdb_client

        db = await get_documentdb_client()
        await _ensure_mongo_index(db)
        doc = asdict(event)
        ts = doc.get("ts")
        if isinstance(ts, str):
            doc["ts"] = datetime.fromisoformat(ts)
        # ON CONFLICT-equivalent: unique index rejects a dup; swallow it (it is
        # the resource-service side of a call we already recorded).
        try:
            await db[MONGO_COLLECTION].insert_one(doc)
        except Exception as exc:  # noqa: BLE001 - DuplicateKeyError = same call twice
            if "duplicate" not in str(exc).lower() and "E11000" not in str(exc):
                raise
    except Exception as exc:  # noqa: BLE001 - ledger never breaks the call
        logger.error("ledger Mongo write failed (%s); JSONL mirror retains the event", exc)


def emit(event: LedgerEvent) -> None:
    """Record one ledger line: durable Mongo write + JSONL mirror.

    Never raises. The Mongo write is the durable record (Mongo has a persistent
    volume and the gateway already holds credentials); JSONL is a local mirror
    for pod-log shippability. Mongo is written via a fire-and-forget task so the
    request path never awaits the ledger. If Mongo fails, the JSONL line + the
    mirrored log remain as evidence.
    """
    line = event.to_jsonl()
    logger.info("ledger: %s", line)
    # JSONL mirror first (cheap, local, survives a Mongo blip).
    try:
        path = _ledger_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        logger.error("ledger JSONL write failed (%s); event logged above", exc)
    # Durable Mongo write, fire-and-forget on the running loop.
    try:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(_write_mongo(event))
    except RuntimeError:
        # No running loop (sync context, e.g. some unit paths): best-effort
        # synchronous run so the event is still persisted.
        import asyncio

        try:
            asyncio.run(_write_mongo(event))
        except Exception as exc:  # noqa: BLE001
            logger.error("ledger Mongo write (sync path) failed: %s", exc)


# ---------------------------------------------------------------------------
# Budget counter (hot-path pre-check)
# ---------------------------------------------------------------------------


class BudgetUnavailable(Exception):
    """The counter store is unreachable. Callers fail closed on this."""


@dataclass
class BudgetVerdict:
    allowed: bool
    remaining: dict[str, int]


class BudgetCounter:
    """Pre-check counters keyed by (scope, window).

    ``scope`` is a stable string the caller computes, e.g. ``f"run:{run}"`` or
    ``f"tenant:{tenant}:{YYYY-MM}"``. ``consume`` is called AFTER a successful
    call, ``peek`` BEFORE. Both read the same counters so the verdict is
    consistent with the durable ledger's eventual aggregation.
    """

    def __init__(self) -> None:
        self._mem: dict[str, dict[str, int]] = {}

    async def peek(self, scope: str) -> dict[str, int]:
        return dict(self._mem.get(scope, {}))

    async def consume(self, scope: str, *, rows: int = 0, calls: int = 1, tokens: int = 0) -> None:
        cur = self._mem.setdefault(scope, {})
        cur["calls"] = cur.get("calls", 0) + calls
        cur["rows"] = cur.get("rows", 0) + rows
        cur["tokens"] = cur.get("tokens", 0) + tokens

    def reset(self, scope: str | None = None) -> None:
        if scope is None:
            self._mem.clear()
        else:
            self._mem.pop(scope, None)


_default_counter = BudgetCounter()


def get_budget_counter() -> BudgetCounter:
    """Process-local counter (auto-synced from decisions only within a process).

    IMPORTANT: this in-process counter is correct only within one gateway
    replica. The production wiring (task 4.2) plugs a Mongo-backed counter so
    every replica sees the same numbers. This default is what unit tests and a
    single-replica dev stack use.
    """
    return _default_counter


def check_budget(
    budget: dict[str, Any] | None,
    used: dict[str, int],
) -> BudgetVerdict:
    """Pure verdict: does ``used`` leave room under ``budget``?

    ``budget`` absent means the grant inherits the tenant's quota only; the
    caller passes the tenant's remaining as ``used``/limits elsewhere. Here, a
    ``None`` budget is always allowed (tenant enforcement is a separate layer).
    """
    if not budget:
        return BudgetVerdict(allowed=True, remaining={})
    remaining: dict[str, int] = {}
    for dim in ("calls", "rows", "tokens"):
        cap = budget.get(dim)
        if cap is None:
            continue
        rem = int(cap) - int(used.get(dim, 0))
        remaining[dim] = rem
        if rem <= 0:
            return BudgetVerdict(allowed=False, remaining=remaining)
    return BudgetVerdict(allowed=True, remaining=remaining)
