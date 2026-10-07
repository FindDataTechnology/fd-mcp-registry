"""Tests for IDP_USER_GROUP_DEFAULTS on the user-group fallback (ecosystem-bridge 3.4).

Self-service registrants land with no groups and no idp_user_groups record;
the defaults give them a baseline community membership. An explicit record
(even empty) suppresses defaults; a store outage fails closed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth]


def _db_with(doc: dict | None, *, error: Exception | None = None) -> MagicMock:
    collection = MagicMock()
    if error is not None:
        collection.find_one = AsyncMock(side_effect=error)
    else:
        collection.find_one = AsyncMock(return_value=doc)
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=collection)
    return db


async def test_no_record_gets_defaults(monkeypatch) -> None:
    monkeypatch.setenv("IDP_USER_GROUP_DEFAULTS", "community")
    from auth_server.mongodb_groups_enrichment import enrich_user_groups_from_mongodb

    with patch(
        "auth_server.mongodb_groups_enrichment._get_mongodb",
        AsyncMock(return_value=_db_with(None)),
    ):
        assert await enrich_user_groups_from_mongodb("newbie", [], "logto") == ["community"]


async def test_unset_env_is_inert(monkeypatch) -> None:
    monkeypatch.delenv("IDP_USER_GROUP_DEFAULTS", raising=False)
    from auth_server.mongodb_groups_enrichment import enrich_user_groups_from_mongodb

    with patch(
        "auth_server.mongodb_groups_enrichment._get_mongodb",
        AsyncMock(return_value=_db_with(None)),
    ):
        assert await enrich_user_groups_from_mongodb("newbie", [], "logto") == []


async def test_explicit_empty_record_suppresses_defaults(monkeypatch) -> None:
    """运营者显式登记零组 = 决策，不补默认。"""
    monkeypatch.setenv("IDP_USER_GROUP_DEFAULTS", "community")
    from auth_server.mongodb_groups_enrichment import enrich_user_groups_from_mongodb

    with patch(
        "auth_server.mongodb_groups_enrichment._get_mongodb",
        AsyncMock(return_value=_db_with({"username": "x", "groups": [], "enabled": True})),
    ):
        assert await enrich_user_groups_from_mongodb("x", [], "logto") == []


async def test_real_record_wins_over_defaults(monkeypatch) -> None:
    monkeypatch.setenv("IDP_USER_GROUP_DEFAULTS", "community")
    from auth_server.mongodb_groups_enrichment import enrich_user_groups_from_mongodb

    with patch(
        "auth_server.mongodb_groups_enrichment._get_mongodb",
        AsyncMock(return_value=_db_with({"username": "legal1", "groups": ["legal"], "enabled": True})),
    ):
        assert await enrich_user_groups_from_mongodb("legal1", [], "logto") == ["legal"]


async def test_store_outage_fails_closed_no_defaults(monkeypatch) -> None:
    """库故障不得发组（fail-closed），哪怕默认组已配。"""
    monkeypatch.setenv("IDP_USER_GROUP_DEFAULTS", "community")
    from auth_server.mongodb_groups_enrichment import enrich_user_groups_from_mongodb

    with patch(
        "auth_server.mongodb_groups_enrichment._get_mongodb",
        AsyncMock(return_value=_db_with(None, error=RuntimeError("mongo down"))),
    ):
        assert await enrich_user_groups_from_mongodb("newbie", [], "logto") == []
