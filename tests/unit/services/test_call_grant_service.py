"""Unit tests for registry.services.call_grant_service (ecosystem-bridge 3.3).

The Motor collection is mocked: counter semantics (refuse does not charge,
month windows, read-only usage) are exercised without a live MongoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.services.call_grant_service import CallGrantService, current_month


def _collection_with_counter(count: int | None) -> MagicMock:
    """假件按 (owner, month) 过滤查询——月份窗独立性的关键。"""
    collection = MagicMock()

    async def _find_one(query):
        if query.get("month") == "2026-10":
            return None if count is None else {"owner": "u", "month": "2026-10", "count": count}
        return None  # 其它月份窗尚无档

    collection.find_one = AsyncMock(side_effect=_find_one)
    collection.update_one = AsyncMock()
    collection.create_index = AsyncMock()
    return collection


def _service(count: int | None) -> tuple[CallGrantService, MagicMock]:
    collection = _collection_with_counter(count)
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=collection)
    return CallGrantService(db), collection


@pytest.mark.asyncio
async def test_under_limit_admits_and_charges() -> None:
    service, collection = _service(count=3)
    verdict = await service.check_and_consume("u", limit=5)
    assert (verdict.allowed, verdict.used, verdict.limit) == (True, 4, 5)
    collection.update_one.assert_awaited_once()
    args = collection.update_one.await_args.args
    assert args[0] == {"owner": "u", "month": verdict.month}
    assert args[1]["$inc"] == {"count": 1}


@pytest.mark.asyncio
async def test_at_limit_refuses_without_charging() -> None:
    service, collection = _service(count=5)
    verdict = await service.check_and_consume("u", limit=5)
    assert not verdict.allowed
    assert verdict.used == 5  # 原值，未 +1：拒收不计数
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_month_windows_are_independent() -> None:
    service, _ = _service(count=5)
    verdict = await service.check_and_consume("u", limit=5, month="2026-11")
    assert verdict.allowed and verdict.month == "2026-11"


@pytest.mark.asyncio
async def test_no_document_counts_as_zero() -> None:
    service, collection = _service(count=None)
    verdict = await service.check_and_consume("u", limit=1)
    assert verdict.allowed and verdict.used == 1
    collection.update_one.assert_awaited_once()  # upsert 起始档


@pytest.mark.asyncio
async def test_usage_is_read_only() -> None:
    service, collection = _service(count=7)
    assert await service.usage("u") == 7
    collection.update_one.assert_not_awaited()


def test_current_month_utc() -> None:
    assert current_month(datetime(2026, 10, 7, 23, 59, tzinfo=timezone.utc)) == "2026-10"
    assert current_month(datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)) == "2027-01"
