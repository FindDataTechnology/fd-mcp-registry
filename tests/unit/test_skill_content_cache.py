"""Tests for the skill-content read cache (openspec: cache-skill-content-reads).

The cache exists because the same SKILL.md was re-downloaded on every read: measured
0.9s-30s per call on production, and batched reads timed out together. These tests pin
the three behaviours that make it safe, not just that it stores things.
"""

import asyncio

import httpx
import pytest

from registry.services import skill_content_cache as cache


def _resp(body: bytes = b"# skill", status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, request=httpx.Request("GET", "https://x/y"))


@pytest.fixture(autouse=True)
def _clean():
    """Every test starts with an empty cache and the shipped defaults."""
    cache.clear()
    yield
    cache.clear()


def _set(monkeypatch, ttl=300, concurrency=8):
    from registry.core.config import settings

    monkeypatch.setattr(settings, "skill_content_cache_ttl_seconds", ttl, raising=False)
    monkeypatch.setattr(settings, "skill_content_fetch_concurrency", concurrency, raising=False)


async def test_second_read_does_not_call_upstream(monkeypatch):
    """A cached read must not reach the fetcher at all."""
    _set(monkeypatch)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return _resp(b"first")

    first = await cache.fetch_cached("skills/a", None, fetcher)
    second = await cache.fetch_cached("skills/a", None, fetcher)

    assert calls == 1
    assert first.content == second.content == b"first"


async def test_companion_resources_are_cached_per_resource(monkeypatch):
    """The SKILL.md and a companion file are distinct keys and must not collide."""
    _set(monkeypatch)
    bodies = {"md": b"main", "ref": b"reference"}

    async def fetcher_for(key):
        async def f():
            return _resp(bodies[key])

        return f

    md = await cache.fetch_cached("skills/a", None, await fetcher_for("md"))
    ref = await cache.fetch_cached("skills/a", "docs/ref.md", await fetcher_for("ref"))

    assert md.content == b"main"
    assert ref.content == b"reference"


async def test_expiry_refetches(monkeypatch):
    """Past the TTL the entry is gone and the upstream is consulted again."""
    _set(monkeypatch, ttl=1)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return _resp(b"v%d" % calls)

    await cache.fetch_cached("skills/a", None, fetcher)
    assert calls == 1

    # Move the entry's deadline into the past rather than sleeping a second.
    key = ("skills/a", "")
    expires_at, response = cache._CACHE[key]
    cache._CACHE[key] = (expires_at - 10, response)

    again = await cache.fetch_cached("skills/a", None, fetcher)
    assert calls == 2
    assert again.content == b"v2"


async def test_ttl_zero_disables_the_cache(monkeypatch):
    """TTL 0 is the escape hatch for chasing drift: every read hits upstream."""
    _set(monkeypatch, ttl=0)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        return _resp()

    await cache.fetch_cached("skills/a", None, fetcher)
    await cache.fetch_cached("skills/a", None, fetcher)

    assert calls == 2
    assert cache.stats()["entries"] == 0


async def test_failures_are_not_cached(monkeypatch):
    """One 502 must not freeze into five minutes of 'this skill is broken'."""
    _set(monkeypatch)
    fail = True

    async def fetcher():
        if fail:
            raise RuntimeError("upstream down")
        return _resp(b"recovered")

    with pytest.raises(RuntimeError):
        await cache.fetch_cached("skills/a", None, fetcher)

    assert cache.stats()["entries"] == 0

    fail = False
    ok = await cache.fetch_cached("skills/a", None, fetcher)
    assert ok.content == b"recovered"


async def test_invalidate_drops_every_entry_for_that_skill_only(monkeypatch):
    """Repointing a URL must take effect immediately, and must not touch neighbours."""
    _set(monkeypatch)

    async def md():
        return _resp(b"md")

    async def ref():
        return _resp(b"ref")

    async def other():
        return _resp(b"other")

    await cache.fetch_cached("skills/a", None, md)
    await cache.fetch_cached("skills/a", "docs/ref.md", ref)
    await cache.fetch_cached("skills/b", None, other)
    assert cache.stats()["entries"] == 3

    dropped = cache.invalidate("skills/a")

    assert dropped == 2
    assert cache.get("skills/a", None) is None
    assert cache.get("skills/a", "docs/ref.md") is None
    assert cache.get("skills/b", None) is not None, "a neighbour must survive invalidation"


async def test_concurrent_reads_are_bounded(monkeypatch):
    """A burst queues on the semaphore instead of stampeding the upstream."""
    _set(monkeypatch, concurrency=3)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fetcher(n):
        nonlocal in_flight, peak

        async def f():
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return _resp(b"n%d" % n)

        return f

    await asyncio.gather(
        *(cache.fetch_cached(f"skills/s{i}", None, await fetcher(i)) for i in range(12))
    )

    assert peak <= 3, f"upstream concurrency peaked at {peak}, above the bound of 3"


async def test_queued_readers_reuse_the_first_fetch(monkeypatch):
    """N readers of one uncached skill must produce ONE upstream fetch, not N.

    This is the re-check inside the semaphore: without it, the readers that queued
    behind the first one would each fetch again once they got the lock.
    """
    _set(monkeypatch, concurrency=2)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return _resp(b"once")

    await asyncio.gather(*(cache.fetch_cached("skills/same", None, fetcher) for _ in range(8)))

    # Measured before the single-flight map landed: 8 calls for 8 readers. The
    # semaphore bounds DISTINCT keys; deduplicating one key needs the in-flight map.
    assert calls == 1


async def test_leader_failure_wakes_every_follower(monkeypatch):
    """A failing leader must propagate to all followers and leave no in-flight entry."""
    _set(monkeypatch)
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        raise RuntimeError("upstream down")

    results = await asyncio.gather(
        *(cache.fetch_cached("skills/bad", None, boom) for _ in range(5)),
        return_exceptions=True,
    )

    assert all(isinstance(r, RuntimeError) for r in results)
    assert calls == 1, "followers must await the leader, not retry the upstream"
    assert cache._INFLIGHT == {}, "a failed fetch must not leave an in-flight entry"


def test_conftest_clears_the_cache_between_tests():
    """The autouse fixture in tests/conftest.py must keep tests hermetic.

    Without it, two tests that call ``get_skill_content`` with the same path --
    exactly what test_skill_routes_github_auth.py does (both use ``test/skill``)
    -- collide: the first stores its response, the second is served from cache,
    never calls the mocked upstream, and its ``mock_client.get.call_args``
    assertion fails depending on test order. This test pins the fixture itself,
    so removing it is caught here rather than as an order-dependent CI flake.
    """
    from registry.services import skill_content_cache as cache

    cache.put("test/skill", None, _resp(b"cached"))
    assert cache.stats()["entries"] == 1

    # A following test in the same process must start clean. Simulate the
    # fixture's between-test invocation and assert the entry is gone.
    fixture = __import__("tests.conftest", fromlist=["_reset_skill_content_cache"])._reset_skill_content_cache
    gen = fixture()
    next(gen)  # enter
    try:
        assert cache.stats()["entries"] == 0, "the fixture must clear on enter"
    finally:
        next(gen, None)  # exit
    assert cache.stats()["entries"] == 0, "the fixture must clear on exit"
