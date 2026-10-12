"""Unit tests for skill_service content-propagation bookkeeping.

Covers the ``update_skill`` additions from the skill-content-propagation fix:

* ``skill_md_raw_url`` is re-derived from ``skill_md_url`` (same translation
  rule as the create path) whenever a PUT moves the URL — the content
  fallback can no longer keep pointing at a stale pack version.
* An update that supplies no inline body (absent / ``None``) never clears the
  stored one.
* A body that actually changes refreshes ``content_updated_at``.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from registry.schemas.skill_models import SkillCard
from registry.services.skill_service import SkillService

_V1_URL = "https://facet.example/api/packs/p/versions/1/skills/demo.md"
_V2_URL = "https://facet.example/api/packs/p/versions/2/skills/demo.md"
_GH_BLOB_URL = "https://github.com/acme/skills/blob/main/SKILL.md"


def _make_existing(**overrides) -> SkillCard:
    """A stored card as the create path leaves it (raw_url frozen at v1)."""
    data = {
        "path": "/skills/demo-skill",
        "id": "demo-skill",
        "name": "demo-skill",
        "description": "A demo skill.",
        "skill_md_url": _V1_URL,
        "skill_md_raw_url": _V1_URL,
        "skill_md_content": None,
    }
    data.update(overrides)
    return SkillCard(**data)


def _service_with(existing: SkillCard) -> tuple[SkillService, AsyncMock]:
    service = SkillService()
    repo = AsyncMock()
    repo.get.return_value = existing
    repo.update.return_value = existing
    service._repo = repo
    service._search_repo = AsyncMock()
    return service, repo


class TestUpdateSkillRawUrlDerivation:
    """update_skill re-derives skill_md_raw_url when skill_md_url moves."""

    @pytest.mark.asyncio
    async def test_url_move_refreshes_raw_url(self) -> None:
        service, repo = _service_with(_make_existing())

        await service.update_skill(
            "/skills/demo-skill", {"skill_md_url": _V2_URL, "skill_md_content": "body v2"}
        )

        persisted = repo.update.await_args.args[1]
        # Non-GitHub URL: translation is identity, but it FOLLOWS the new version.
        assert persisted["skill_md_raw_url"] == _V2_URL
        assert persisted["content_updated_at"] is not None

    @pytest.mark.asyncio
    async def test_github_url_move_translates_to_raw(self) -> None:
        service, repo = _service_with(_make_existing())

        await service.update_skill("/skills/demo-skill", {"skill_md_url": _GH_BLOB_URL})

        persisted = repo.update.await_args.args[1]
        assert "raw.githubusercontent.com" in persisted["skill_md_raw_url"]

    @pytest.mark.asyncio
    async def test_same_url_leaves_raw_url_untouched(self) -> None:
        """No URL change → no raw_url key in the persisted $set."""
        service, repo = _service_with(_make_existing())

        await service.update_skill("/skills/demo-skill", {"skill_md_url": _V1_URL})

        persisted = repo.update.await_args.args[1]
        assert "skill_md_raw_url" not in persisted

    @pytest.mark.asyncio
    async def test_update_without_url_never_reads_raw_url(self) -> None:
        """A body-only update (URL untouched) leaves raw_url out of the $set."""
        service, repo = _service_with(_make_existing())

        await service.update_skill("/skills/demo-skill", {"skill_md_content": "body v2"})

        persisted = repo.update.await_args.args[1]
        assert "skill_md_raw_url" not in persisted


class TestUpdateSkillInlineBody:
    """update_skill stores inline bodies but never clears them."""

    @pytest.mark.asyncio
    async def test_new_body_persisted_with_timestamp(self) -> None:
        service, repo = _service_with(_make_existing())

        before = datetime.now(UTC)
        await service.update_skill("/skills/demo-skill", {"skill_md_content": "body v2"})

        persisted = repo.update.await_args.args[1]
        assert persisted["skill_md_content"] == "body v2"
        assert persisted["content_updated_at"] >= before

    @pytest.mark.asyncio
    async def test_absent_body_not_persisted(self) -> None:
        """No body key → nothing content-related is $set."""
        service, repo = _service_with(_make_existing())

        await service.update_skill("/skills/demo-skill", {"description": "new text"})

        persisted = repo.update.await_args.args[1]
        assert "skill_md_content" not in persisted
        assert "content_updated_at" not in persisted

    @pytest.mark.asyncio
    async def test_none_body_is_dropped(self) -> None:
        """An explicit None body (route dump of null/'') is dropped, not $set."""
        service, repo = _service_with(_make_existing(skill_md_content="stored body"))

        await service.update_skill(
            "/skills/demo-skill", {"skill_md_url": _V2_URL, "skill_md_content": None}
        )

        persisted = repo.update.await_args.args[1]
        assert "skill_md_content" not in persisted

    @pytest.mark.asyncio
    async def test_identical_body_does_not_refresh_timestamp(self) -> None:
        existing = _make_existing(skill_md_content="same body")
        service, repo = _service_with(existing)

        await service.update_skill("/skills/demo-skill", {"skill_md_content": "same body"})

        persisted = repo.update.await_args.args[1]
        assert persisted["skill_md_content"] == "same body"
        assert "content_updated_at" not in persisted
