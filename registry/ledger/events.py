"""Unified call ledger: append-only event sink + budget counter.

Two responsibilities, deliberately split from any database driver:

1. **Event append** — every authorized call produces one line on a local JSONL
   sink (mirror-first, best-effort, never fails the request). A separate shipper
   (out of this process, no gateway dependency) batches these into the durable PG
   ledger. The gateway image therefore needs no Postgres driver.

2. **Budget pre-check** — the hot-path "is this grant/tenant over its cap?" query.
   It reads a fast counter. This module keeps the counter abstract and ships a
   Mongo-backed implementation (Mongo is already a hard gateway dependency and
   cluster-wide; the replay store proves the fail-closed pattern). The counter is
   a *derived* index over the ledger, not a second source of truth.

Idempotency: the durable ledger dedups on ``(grant_jti, span_id)``. ``span_id``
is minted here (one per call) and carried both in the JSONL line and the
counter check, so the shipper and the aggregator agree on identity.

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


def emit(event: LedgerEvent) -> None:
    """Append one ledger line. Mirror to logger first, then best-effort file.

    Never raises: a ledger I/O failure must not fail the call it records. The
    mirrored line keeps the loss visible (and shippable from container logs).
    """
    line = event.to_jsonl()
    logger.info("ledger: %s", line)
    try:
        path = _ledger_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        logger.error("ledger JSONL write failed (%s); event logged above", exc)


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
