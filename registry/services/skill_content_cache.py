"""In-process cache and concurrency bound for skill content reads.

Why this exists (openspec: cache-skill-content-reads): ``GET /api/skills/{path}/content``
re-downloaded the whole SKILL.md from the forge on every call. Measured on the
production registry 2026-11-22, the same skill read five times in a row took
1369ms, 1949ms, 5987ms, 2960ms and 16421ms -- same bytes, same network. Batching
made it worse: 42 concurrent reads produced 20 false negatives, every one of them
``operation was aborted due to timeout``.

That path is also how an agent runner resolves its skills at cold start
(``agent-runner/registry.js`` ``fetchSkillContent``), so its jitter surfaces to a
visitor as "this turn ended in an error".

Two mechanisms, deliberately small:

* a TTL cache keyed by ``(normalized skill path, resource)`` -- one entry holds one
  URL's bytes, never another skill's;
* a semaphore bounding concurrent upstream fetches, so a burst queues instead of
  stampeding the forge and timing out together.

Failures are never stored. Caching an error would freeze one hiccup into five
minutes of "this skill is broken" -- so the write happens only on the success path.

Cache invalidation is NOT left to the TTL: every skill mutation drops that path's
entries immediately (see ``invalidate``). Stale-until-TTL after an edit is exactly
the "the UI says it changed but it did not" failure that content-drift detection
exists to prevent.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..common.log_redaction import redact_url
from ..core.config import settings

# (normalized skill path, resource or "") -> (expires_at_monotonic, response)
_CACHE: dict[tuple[str, str], tuple[float, httpx.Response]] = {}

# One semaphore for the process. Created lazily because it must be built inside a
# running loop; a module-level asyncio.Semaphore() at import time binds to the
# wrong loop under some test harnesses.
_SEMAPHORE: asyncio.Semaphore | None = None
_SEMAPHORE_LIMIT: int | None = None

# Single-flight map: (path, resource) -> Future of the one in-progress fetch.
# Without it, N concurrent readers of one uncached skill fetch N times -- the
# semaphore bounds different keys, it does not deduplicate the same key.
_INFLIGHT: dict[tuple[str, str], asyncio.Future] = {}


def _cache_ttl() -> float:
    """Seconds a content entry stays fresh. 0 (or less) disables the cache."""
    return float(getattr(settings, "skill_content_cache_ttl_seconds", 300) or 0)


def _concurrency_limit() -> int:
    """Max simultaneous upstream fetches. Guarded to >= 1."""
    return max(1, int(getattr(settings, "skill_content_fetch_concurrency", 8) or 1))


def _semaphore() -> asyncio.Semaphore:
    """The process-wide upstream semaphore, rebuilt if the limit changed."""
    global _SEMAPHORE, _SEMAPHORE_LIMIT
    limit = _concurrency_limit()
    if _SEMAPHORE is None or _SEMAPHORE_LIMIT != limit:
        _SEMAPHORE = asyncio.Semaphore(limit)
        _SEMAPHORE_LIMIT = limit
    return _SEMAPHORE


def _key(skill_path: str, resource: str | None) -> tuple[str, str]:
    return (skill_path, resource or "")


def get(skill_path: str, resource: str | None) -> httpx.Response | None:
    """Return a cached response, or None when absent/expired/cache-disabled."""
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    entry = _CACHE.get(_key(skill_path, resource))
    if entry is None:
        return None
    expires_at, response = entry
    if time.monotonic() >= expires_at:
        _CACHE.pop(_key(skill_path, resource), None)
        return None
    return response


def put(skill_path: str, resource: str | None, response: httpx.Response) -> None:
    """Store a SUCCESSFUL response. Callers must not invoke this on failure."""
    ttl = _cache_ttl()
    if ttl <= 0:
        return
    _CACHE[_key(skill_path, resource)] = (time.monotonic() + ttl, response)


def invalidate(skill_path: str) -> int:
    """Drop every entry for one skill (its SKILL.md and all companion files).

    Called from the three mutation points (PUT, delete, register) so an edit takes
    effect on the very next read instead of after the TTL. Returns the number of
    entries dropped, which the tests assert on.
    """
    prefix = (skill_path, "")
    dropped = [k for k in _CACHE if k[0] == prefix[0]]
    for k in dropped:
        _CACHE.pop(k, None)
    return len(dropped)


def clear() -> None:
    """Drop everything. Used by tests and by a manual cache flush."""
    _CACHE.clear()
    _INFLIGHT.clear()


def stats() -> dict[str, Any]:
    """Entry count and the configured knobs -- cheap to expose on /health."""
    return {
        "entries": len(_CACHE),
        "ttl_seconds": _cache_ttl(),
        "fetch_concurrency": _concurrency_limit(),
    }


async def fetch_cached(
    skill_path: str,
    resource: str | None,
    fetcher,
) -> httpx.Response:
    """Return a cached response or run ``fetcher`` under the concurrency bound.

    ``fetcher`` is an async callable returning ``httpx.Response``; it is only
    called on a miss. Its exceptions propagate untouched and leave no cache entry.

    Single-flight: concurrent readers of the SAME key share one upstream request.
    The semaphore alone is not enough -- it bounds how many different keys fetch at
    once, but eight readers of one uncached skill would each queue, each get the
    lock after the first finished, and each fetch again (measured: 8 calls for 8
    readers). An in-flight map keyed the same way as the cache makes the followers
    await the leader's result instead.
    """
    hit = get(skill_path, resource)
    if hit is not None:
        return hit

    key = _key(skill_path, resource)
    leader = _INFLIGHT.get(key)
    if leader is not None:
        # Someone is already fetching this exact content; await theirs.
        return await leader

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _INFLIGHT[key] = fut
    try:
        async with _semaphore():
            hit = get(skill_path, resource)
            if hit is not None:
                fut.set_result(hit)
                return hit
            response = await fetcher()
            put(skill_path, resource, response)
            fut.set_result(response)
            return response
    except BaseException as exc:  # noqa: BLE001 - re-raised below, after waking followers
        if not fut.done():
            fut.set_exception(exc)
            # Mark retrieved so asyncio does not warn about an unhandled exception
            # when every follower also propagates it.
            fut.exception()
        raise
    finally:
        _INFLIGHT.pop(key, None)


__all__ = [
    "clear",
    "fetch_cached",
    "get",
    "invalidate",
    "put",
    "stats",
]

# ``redact_url`` is imported for callers that log around this module; keep the
# reference so linters do not flag an unused import if the logging path moves.
_ = redact_url
