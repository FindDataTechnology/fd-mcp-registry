"""Unit tests for registry.services.caller_map_service (add-customer-onboarding-automation 3.1).

The Motor collection is mocked (same discipline as test_patch_key_service):
upsert shapes, get_map projection/normalization, delete semantics.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.services.caller_map_service import CallerMapService


def _make_collection_mock() -> MagicMock:
    collection = MagicMock()
    collection.update_one = AsyncMock()
    collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    collection.create_index = AsyncMock()
    # find({}) returns an async-iterable cursor
    cursor = MagicMock()

    class _AsyncIter:
        def __init__(self, docs):
            self._docs = list(docs)

        def __aiter__(self):
            self._iter = iter(self._docs)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    cursor.__aiter__ = lambda self: _AsyncIter(cursor.docs).__aiter__()
    collection.find = MagicMock(return_value=cursor)
    return collection


@pytest.fixture
def collection() -> MagicMock:
    return _make_collection_mock()


@pytest.fixture
def service(collection) -> CallerMapService:
    return CallerMapService(MagicMock(__getitem__=MagicMock(return_value=collection)))


class TestUpsert:
    async def test_upsert_replaces_value(self, service, collection):
        await service.upsert(username="drillcust001", value="67")
        call = collection.update_one.await_args
        assert call.args[0] == {"_id": "drillcust001"}
        assert call.args[1]["$set"]["value"] == "67"
        assert call.kwargs.get("upsert") is True

    async def test_upsert_strips_and_rejects_blank(self, service):
        with pytest.raises(ValueError):
            await service.upsert(username="  ", value="67")
        with pytest.raises(ValueError):
            await service.upsert(username="u", value="")

    async def test_upsert_strips_whitespace(self, service, collection):
        await service.upsert(username=" drillcust001 ", value=" 67 ")
        assert collection.update_one.await_args.args[0] == {"_id": "drillcust001"}


class TestGetMap:
    async def test_get_map_projects_and_skips_junk(self, service, collection):
        collection.find.return_value.docs = [
            {"_id": "drillcust001", "value": "67"},
            {"_id": "lexdemocustomer", "value": "56"},
            {"_id": "blank", "value": "   "},  # blank value -> skipped
            {"_id": "", "value": "1"},  # blank id -> skipped
            {"_id": "novalue"},  # missing value -> skipped
        ]
        mapping = await service.get_map()
        assert mapping == {"drillcust001": "67", "lexdemocustomer": "56"}


class TestDelete:
    async def test_delete_true_when_removed(self, service, collection):
        assert await service.delete("drillcust001") is True
        assert collection.delete_one.await_args.args[0] == {"_id": "drillcust001"}

    async def test_delete_false_when_absent(self, service, collection):
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=0))
        assert await service.delete("nobody") is False
