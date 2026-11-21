"""Budget enforcement for delegation grants on the mcp-proxy hop.

Runs in the admission gates (after scope authorization, alongside the sub2api
preflight and call grant), BEFORE any outbound work — a refused request costs
nothing.

Rule (spec ``delegation-grant``): a grant with ``budget`` is a hard cap; the
call is refused 402 once the ledger aggregation shows the cap is spent. A grant
with no ``budget`` is bounded only by the tenant quota (enforced elsewhere), so
this module is a no-op for it. The counter store being unreachable is 503
(fail-closed) — never allowed.

The verdict is computed from the Mongo budget counter (cluster-wide, derived
index over the ledger). On an admitted call this module also ``consume``s the
counter and emits a ledger event, so the same numbers the pre-check reads are the
ones that grow.

Ships inert: only invoked when the caller's validation_result method is "grant",
which itself only exists when GRANT_PLANE_ENABLED is on.
"""

import logging
import time
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _scope_for(claims: dict[str, Any]) -> str:
    """Counter scope: per-run when a run id is present, else per-grant.

    Per-run is the right unit — an agent run is what the budget is meant to cap.
    """
    run = claims.get("run")
    if run:
        return f"run:{run}"
    return f"grant:{claims.get('jti', 'unknown')}"


async def enforce_grant_budget(
    claims: dict[str, Any],
    *,
    server_name: str,
    tool_name: str | None,
    request_body: Any,
) -> None:
    """Refuse if the grant's budget is spent; else consume one call.

    ``claims`` is the mcp-proxy token payload, which carries the grant claims
    forwarded at /validate (grant_aud/tenant/run/grant_jti + budget).
    """
    budget = claims.get("budget")
    if not budget:
        return  # tenant-bounded only; nothing to enforce at this layer

    # Only tools/call spends the budget; handshakes do not.
    method = _jsonrpc_method(request_body)
    if method != "tools/call":
        return

    from registry.ledger.events import check_budget
    from registry.ledger.mongo_counter import get_mongo_budget_counter

    counter = await get_mongo_budget_counter()
    if not await counter.available():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "budget_unavailable",
                "message": "预算判定不可用（fail-closed）",
            },
        )

    scope = _scope_for(claims)
    used = await counter.peek(scope)
    verdict = check_budget(budget, used)
    if not verdict.allowed:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "GRANT_EXHAUSTED",
                "message": "本次委托额度已用尽",
                "remaining": verdict.remaining,
            },
        )

    # Admit: consume one call now. rows/tokens are reconciled when the resource
    # service reports; the call counter grows immediately so concurrent calls in
    # one run see each other.
    await counter.consume(scope, calls=1)


def _jsonrpc_method(body: Any) -> str | None:
    if isinstance(body, dict):
        return body.get("method")
    return None
