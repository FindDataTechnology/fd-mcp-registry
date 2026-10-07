"""Tests for lifecycle status filtering and validation."""

import pytest

from registry.repositories.documentdb.search_repository import _build_status_filter
from registry.schemas.registry_card import _validate_lifecycle_status


class TestBuildStatusFilter:
    """Tests for _build_status_filter MongoDB filter builder."""

    def test_default_excludes_draft_and_deprecated_and_disabled(self):
        """Default call excludes draft, deprecated, and disabled."""
        result = _build_status_filter()
        assert "$and" in result
        conditions = result["$and"]
        assert len(conditions) == 2

        # First condition: status filtering
        status_cond = conditions[0]
        assert "$or" in status_cond
        status_nin = status_cond["$or"][0]
        assert "draft" in status_nin["status"]["$nin"]
        assert "deprecated" in status_nin["status"]["$nin"]

        # Second condition: enabled filtering
        enabled_cond = conditions[1]
        assert "$or" in enabled_cond

    def test_include_all_returns_empty_dict(self):
        """Including everything returns empty filter."""
        result = _build_status_filter(
            include_draft=True,
            include_deprecated=True,
            include_disabled=True,
        )
        assert result == {}

    def test_include_draft_only_excludes_deprecated(self):
        """Including draft still excludes deprecated."""
        result = _build_status_filter(include_draft=True)
        # Should have $and with status filter (deprecated only) and enabled filter
        assert "$and" in result
        status_cond = result["$and"][0]
        status_nin = status_cond["$or"][0]["status"]["$nin"]
        assert "deprecated" in status_nin
        assert "draft" not in status_nin

    def test_include_deprecated_only_excludes_draft(self):
        """Including deprecated still excludes draft."""
        result = _build_status_filter(include_deprecated=True)
        assert "$and" in result
        status_cond = result["$and"][0]
        status_nin = status_cond["$or"][0]["status"]["$nin"]
        assert "draft" in status_nin
        assert "deprecated" not in status_nin

    def test_include_disabled_still_filters_status(self):
        """Including disabled still filters draft and deprecated."""
        result = _build_status_filter(include_disabled=True)
        # Only status filter, no enabled filter
        assert "$or" in result
        status_nin = result["$or"][0]["status"]["$nin"]
        assert "draft" in status_nin
        assert "deprecated" in status_nin

    def test_documents_without_status_field_pass_through(self):
        """Filter allows documents without a status field (backwards compat)."""
        result = _build_status_filter()
        status_cond = result["$and"][0]
        # Second $or clause should be {"status": {"$exists": False}}
        exists_clause = status_cond["$or"][1]
        assert exists_clause == {"status": {"$exists": False}}

    def test_documents_without_is_enabled_field_pass_through(self):
        """Filter allows documents without is_enabled field (backwards compat)."""
        result = _build_status_filter()
        enabled_cond = result["$and"][1]
        exists_clause = enabled_cond["$or"][1]
        assert exists_clause == {"is_enabled": {"$exists": False}}

    def test_include_draft_and_deprecated_only_filters_disabled(self):
        """Including both draft and deprecated leaves only the disabled filter."""
        result = _build_status_filter(
            include_draft=True,
            include_deprecated=True,
        )
        # Only enabled filter remains, returned directly (not wrapped in $and)
        assert "$or" in result
        assert result["$or"][0] == {"is_enabled": True}


class TestValidateLifecycleStatus:
    """Tests for _validate_lifecycle_status function."""

    def test_valid_status_accepted(self):
        """Valid enum status is accepted."""
        result = _validate_lifecycle_status("active")
        assert result == "active"

    def test_status_normalized_to_lowercase(self):
        """Status input is normalized to lowercase."""
        result = _validate_lifecycle_status("ACTIVE")
        assert result == "active"

    def test_invalid_status_rejected(self):
        """Invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Invalid status"):
            _validate_lifecycle_status("unknown")

    def test_all_enum_values_accepted(self):
        """All LifecycleStatus enum values are accepted."""
        for status in ["active", "deprecated", "draft", "beta"]:
            result = _validate_lifecycle_status(status)
            assert result == status


class TestModelDefaults:
    """Tests for model default status values."""

    def test_agent_registration_defaults_to_draft(self):
        """New agent registrations default to draft status."""
        from registry.schemas.agent_models import AgentRegistrationRequest

        request = AgentRegistrationRequest(
            name="test-agent",
            url="https://example.com/agent",
            supportedProtocol="a2a",
        )
        assert request.status == "draft"

    def test_agent_card_defaults_to_active(self):
        """Existing agent cards default to active (backwards compat)."""
        from registry.schemas.agent_models import AgentCard

        card = AgentCard(
            name="test-agent",
            description="A test agent",
            url="https://example.com/agent",
            version="1.0.0",
        )
        assert card.status == "active"

    def test_skill_registration_defaults_to_draft(self):
        """New skill registrations default to draft status."""
        from registry.schemas.skill_models import SkillRegistrationRequest

        request = SkillRegistrationRequest(
            name="test-skill",
            description="A test skill",
            skill_md_url="https://github.com/test/skill/blob/main/SKILL.md",
        )
        assert request.status == "draft"

    def test_skill_card_defaults_to_active(self):
        """Existing skill cards default to active (backwards compat)."""
        from registry.schemas.skill_models import SkillCard

        card = SkillCard(
            name="test-skill",
            description="A test skill",
            path="/skills/test-skill",
            skill_md_url="https://example.com/SKILL.md",
        )
        assert card.status == "active"
