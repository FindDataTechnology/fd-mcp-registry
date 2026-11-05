"""Unit tests for the spine repository write forwarding (task 2.3) and the
factory/Mongo structural guard (tasks 2.1 factory switch + 2.4).

Covers:
- write methods forward to the spine gate with the acting identity (actor
  contextvar) and the contract's body shapes,
- gate errors are carried verbatim (SpineGateError with status/code),
- the factory serves spine repos when SPINE_REPLICA_MODE=1 and DocumentDB
  repos otherwise,
- the legacy Mongo registration repositories refuse to construct in spine
  mode (SUPPLY_LEGACY_DIRECT_WRITE structural failure),
- the SpineSkillRepository read path assembles SkillCard models.
"""

import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from registry.repositories import factory  # noqa: E402
from registry.repositories.spine.actor_context import (  # noqa: E402
    current_actor,
    install_default_actor,
    set_acting_actor,
)
from registry.repositories.spine.adapter import SpineAdapter  # noqa: E402
from registry.repositories.spine.projection_client import (  # noqa: E402
    SpineProjectionClient,
)
from registry.repositories.spine.replica import SpineReplicaStore  # noqa: E402
from registry.repositories.spine.server_repository import (  # noqa: E402
    SpineServerRepository,
    SpineStructuralError,
)
from registry.repositories.spine.skill_repository import (  # noqa: E402
    SpineSkillRepository,
)
from registry.repositories.spine.sync_scheduler import (  # noqa: E402
    SpineReplicaSyncScheduler,
)
from registry.repositories.spine.write_gate import (  # noqa: E402
    SpineGateError,
)
from registry.schemas.skill_models import SkillCard  # noqa: E402

_MCP_ROW: dict[str, Any] = {
    "id": "/law-bench",
    "display_name": "Law Bench",
    "description": "legal benchmarks",
    "proxy_pass_url": "http://upstream:8000/mcp",
    "num_tools": 2,
    "tool_list": [{"name": "t1", "description": "d1"}, {"name": "t2"}],
    "tags": ["legal"],
    "license": "MIT",
    "provider_organization": "finddata",
    "deployment": "remote",
    "metadata": {"credential_ref": "lex", "tool_overrides": {"t2": {"blocked": True}}},
    "visibility": "public",
    "groups": [],
    "is_enabled": 1,
    "created_at": 1700000000000,
    "updated_at": 1700000001000,
    "updated_by": "ops@finddata",
}

_SKILL_ROW: dict[str, Any] = {
    "name": "/skills/pdf-processing",
    "display_name": "pdf-processing",
    "description": "process pdfs",
    "repository_url": "https://github.com/x/y",
    "author": "someone",
    "version": "1.2.0",
    "tags": ["docs"],
    "license": "MIT",
    "metadata": {"skill_md_url": "https://github.com/x/y/SKILL.md"},
    "visibility": "public",
    "groups": [],
    "is_enabled": 1,
    "created_at": 1700000000000,
    "updated_at": 1700000001000,
    "updated_by": "ops@finddata",
}


class RecordingGate:
    """Captures gate calls; returns canned errors per route when set."""

    def __init__(self, response: dict[str, Any] | None = None, error: SpineGateError | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = response or {"ok": True}
        self.error = error

    async def mcp_upsert(self, actor, entry, source="facade"):
        self.calls.append(("mcp_upsert", {"actor": actor, "entry": entry, "source": source}))
        return self._verdict()

    async def mcp_patch(self, actor, server_id, fields, source="facade"):
        self.calls.append(
            ("mcp_patch", {"actor": actor, "id": server_id, "fields": fields, "source": source})
        )
        return self._verdict()

    async def mcp_delete(self, actor, server_id, source="facade"):
        self.calls.append(("mcp_delete", {"actor": actor, "id": server_id, "source": source}))
        return self._verdict()

    async def skill_upsert(self, actor, entry, source="facade"):
        self.calls.append(("skill_upsert", {"actor": actor, "entry": entry, "source": source}))
        return self._verdict()

    async def skill_patch(self, actor, skill_name, fields, source="facade"):
        self.calls.append(
            (
                "skill_patch",
                {"actor": actor, "name": skill_name, "fields": fields, "source": source},
            )
        )
        return self._verdict()

    async def skill_delete(self, actor, skill_name, source="facade"):
        self.calls.append(("skill_delete", {"actor": actor, "name": skill_name, "source": source}))
        return self._verdict()

    def _verdict(self):
        if self.error is not None:
            raise self.error
        return self.response


def build_adapter(tmp_path, gate: RecordingGate) -> SpineAdapter:
    replica = SpineReplicaStore(tmp_path / "replica.db")
    replica.replace_snapshot([_MCP_ROW], [_SKILL_ROW], as_of=1)
    projection = SpineProjectionClient("http://facet.test", "t")
    scheduler = SpineReplicaSyncScheduler(replica, projection, interval_seconds=3600)
    # Post-write resync would hit the network; the recording gate replaces the
    # real write path, so disable the resync for unit tests.
    scheduler.pull_once = lambda: _noop_true()  # type: ignore[method-assign]
    return SpineAdapter(replica, projection, gate, scheduler)


async def _noop_true() -> bool:
    return True


@pytest.fixture(autouse=True)
def _default_actor():
    install_default_actor("registry-service")
    yield
    install_default_actor("registry-service")


class TestWriteForwarding:
    async def test_server_create_forwards_upsert_with_actor(self, tmp_path):
        gate = RecordingGate()
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        server_info = {
            "path": "/new-server",
            "server_name": "New Server",
            "description": "d",
            "proxy_pass_url": "http://u/mcp",
            "tags": ["a"],
            "license": "MIT",
            "visibility": "public",
            "allowed_groups": ["g1"],
            "is_enabled": False,
            "id": "asset-123",
        }
        token = set_acting_actor("ops@finddata")
        try:
            assert await repo.create(server_info) is True
        finally:
            from registry.repositories.spine.actor_context import reset_acting_actor

            reset_acting_actor(token)

        route, payload = gate.calls[0]
        assert route == "mcp_upsert"
        assert payload["actor"] == "ops@finddata"
        assert payload["source"] == "facade"
        entry = payload["entry"]
        assert entry["id"] == "/new-server"
        assert entry["display_name"] == "New Server"
        assert entry["proxy_pass_url"] == "http://u/mcp"
        assert entry["groups"] == ["g1"]
        # legacy top-level `id` demoted into metadata for the spine
        assert entry["metadata"]["id"] == "asset-123"

    async def test_server_update_maps_document_fields_to_spine_columns(self, tmp_path):
        gate = RecordingGate()
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        merged = {
            "path": "/law-bench",
            "server_name": "Law Bench v2",
            "description": "updated",
            "visibility": "private",
            "allowed_groups": ["lex-ops"],
            "metadata": {"credential_ref": "lex"},
        }
        assert await repo.update("/law-bench", merged, updated_fields=["server_name", "visibility"]) is True
        route, payload = gate.calls[0]
        assert route == "mcp_patch"
        assert payload["id"] == "/law-bench"
        assert payload["fields"]["display_name"] == "Law Bench v2"
        assert payload["fields"]["visibility"] == "private"
        # untouched fields are NOT forwarded on a partial write
        assert "description" not in payload["fields"]
        assert "allowed_groups" not in payload["fields"]
        assert "groups" not in payload["fields"]

    async def test_server_update_on_unknown_path_returns_false(self, tmp_path):
        gate = RecordingGate()
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        assert await repo.update("/missing", {"server_name": "x"}) is False
        assert gate.calls == []

    async def test_set_state_forwards_is_enabled_patch(self, tmp_path):
        gate = RecordingGate()
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        assert await repo.set_state("/law-bench", False) is True
        route, payload = gate.calls[0]
        assert route == "mcp_patch"
        assert payload["fields"] == {"is_enabled": False}
        assert payload["actor"] == current_actor()

    async def test_set_tool_override_updates_metadata_tool_overrides(self, tmp_path):
        gate = RecordingGate()
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        override = {"blocked": True, "reason": "scan"}
        assert await repo.set_tool_override("/law-bench", "t1", override) is True
        route, payload = gate.calls[0]
        assert route == "mcp_patch"
        fields = payload["fields"]
        assert "metadata" in fields
        assert fields["metadata"]["tool_overrides"]["t1"] == override
        # existing override for t2 preserved
        assert fields["metadata"]["tool_overrides"]["t2"] == {"blocked": True}

    async def test_delete_forwards_and_maps_404_to_false(self, tmp_path):
        gate = RecordingGate()
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        assert await repo.delete("/law-bench") is True
        assert gate.calls[0][0] == "mcp_delete"

        missing_gate = RecordingGate(error=SpineGateError(404, "SUPPLY_NOT_FOUND", "no row"))
        repo2 = SpineServerRepository(build_adapter(tmp_path, missing_gate))
        assert await repo2.delete("/missing-path") is False

    async def test_gate_errors_are_carried_verbatim(self, tmp_path):
        gate = RecordingGate(
            error=SpineGateError(400, "SUPPLY_UNKNOWN_FIELD", "unknown mcp_servers patch field(s): frobnicate")
        )
        repo = SpineServerRepository(build_adapter(tmp_path, gate))
        with pytest.raises(SpineGateError) as excinfo:
            await repo.set_state("/law-bench", True)
        assert excinfo.value.status == 400
        assert excinfo.value.code == "SUPPLY_UNKNOWN_FIELD"
        assert "frobnicate" in excinfo.value.message

    async def test_skill_create_forwards_upsert_with_actor_and_card_roundtrip(self, tmp_path):
        gate = RecordingGate()
        repo = SpineSkillRepository(build_adapter(tmp_path, gate))
        from registry.schemas.skill_models import SkillCard

        card = SkillCard(
            path="/skills/pdf-processing",
            name="pdf-processing",
            description="process pdfs",
            skill_md_url="https://github.com/x/y/SKILL.md",
            repository_url="https://github.com/x/y",
            tags=["docs"],
            license="MIT",
            visibility="public",
            allowed_groups=[],
            is_enabled=True,
        )
        token = set_acting_actor("author@finddata")
        try:
            returned = await repo.create(card)
        finally:
            from registry.repositories.spine.actor_context import reset_acting_actor

            reset_acting_actor(token)

        route, payload = gate.calls[0]
        assert route == "skill_upsert"
        assert payload["actor"] == "author@finddata"
        entry = payload["entry"]
        # spine skills primary key = the legacy path
        assert entry["name"] == "/skills/pdf-processing"
        assert entry["display_name"] == "pdf-processing"
        assert entry["author"] is None or isinstance(entry["author"], str)
        assert isinstance(returned, SkillCard)

    async def test_skill_reads_assemble_skillcards_from_replica(self, tmp_path):
        repo = SpineSkillRepository(build_adapter(tmp_path, RecordingGate()))
        card = await repo.get("/skills/pdf-processing")
        assert card is not None
        assert card.path == "/skills/pdf-processing"
        assert card.name == "pdf-processing"
        assert card.is_enabled is True
        assert card.visibility == "public"
        assert card.metadata is not None
        assert card.metadata.author == "someone"
        assert card.metadata.version == "1.2.0"
        assert str(card.skill_md_url) == "https://github.com/x/y/SKILL.md"
        assert await repo.count() == 1
        assert (await repo.get_state("/skills/pdf-processing")) is True

        listing = await repo.list_all()
        assert len(listing) == 1
        assert isinstance(listing[0], SkillCard)

    async def test_skill_delete_unknown_returns_false_and_gate_delete_forwards(self, tmp_path):
        gate = RecordingGate()
        repo = SpineSkillRepository(build_adapter(tmp_path, gate))
        assert await repo.delete("/skills/absent") is False
        assert gate.calls == []
        assert await repo.delete("/skills/pdf-processing") is True
        assert gate.calls[0][0] == "skill_delete"
        assert gate.calls[0][1]["name"] == "/skills/pdf-processing"


class TestFactorySwitch:
    @pytest.fixture(autouse=True)
    def _restore_real_factory_getters(self, mock_all_repositories):
        """Restore the real factory getters for these tests.

        The root conftest's ``mock_all_repositories`` autouse fixture patches
        ``registry.repositories.factory.get_server_repository`` (and friends)
        to AsyncMocks; the factory-switch tests must exercise the real
        getters, so re-patch them with the originals captured at import time
        (before any fixture patching runs). Declaring the conftest fixture as
        a dependency pins the ordering — this restore always applies last.
        """
        from registry.repositories import factory as factory_module

        with (
            patch.object(
                factory_module,
                "get_server_repository",
                self._real_get_server_repository,
            ),
            patch.object(
                factory_module, "get_skill_repository", self._real_get_skill_repository
            ),
        ):
            yield

    _real_get_server_repository = staticmethod(factory.get_server_repository)
    _real_get_skill_repository = staticmethod(factory.get_skill_repository)

    async def test_factory_returns_spine_repos_when_mode_on(self, tmp_path, monkeypatch):
        import registry.core.config as config_module
        from registry.core.config import Settings
        from registry.repositories import factory

        test_settings = Settings(
            secret_key="x" * 40,
            auth_server_nginx_marker_secret="y" * 40,
            spine_replica_mode=True,
            spine_projection_url="http://facet.test",
            spine_projection_token="t1",
            supply_store_write_url="http://facet.test",
            supply_store_write_token="t2",
            spine_replica_db_path=str(tmp_path / "factory.db"),
        )
        monkeypatch.setattr(config_module, "settings", test_settings)
        # The factory (and the adapter) bind `settings` at import time; patch
        # those bindings too so the mode switch is visible everywhere.
        monkeypatch.setattr("registry.repositories.factory.settings", test_settings)
        monkeypatch.setattr(
            "registry.repositories.spine.adapter.settings", test_settings, raising=False
        )
        factory.reset_repositories()
        try:
            server_repo = factory.get_server_repository()
            from registry.repositories.spine.server_repository import (
                SpineServerRepository as Srv,
            )

            assert isinstance(server_repo, Srv)
            skill_repo = factory.get_skill_repository()
            from registry.repositories.spine.skill_repository import (
                SpineSkillRepository as Skl,
            )

            assert isinstance(skill_repo, Skl)
            # singleton stability
            assert factory.get_server_repository() is server_repo
        finally:
            factory.reset_repositories()

    async def test_factory_returns_documentdb_repos_when_mode_off(self, monkeypatch):
        import registry.core.config as config_module
        from registry.core.config import Settings
        from registry.repositories import factory

        test_settings = Settings(
            secret_key="x" * 40,
            auth_server_nginx_marker_secret="y" * 40,
            spine_replica_mode=False,
        )
        monkeypatch.setattr(config_module, "settings", test_settings)
        # The factory binds `settings` at import time; without this patch the
        # factory would read the process-boot settings object (which, under an
        # exported SPINE_REPLICA_MODE=1, says spine mode and takes the spine
        # branch regardless of this test's explicit off mode).
        monkeypatch.setattr("registry.repositories.factory.settings", test_settings)
        factory.reset_repositories()
        try:
            from unittest.mock import patch

            with (
                patch(
                    "registry.repositories.documentdb.server_repository.DocumentDBServerRepository"
                ) as srv_mock,
                patch(
                    "registry.repositories.documentdb.skill_repository.DocumentDBSkillRepository"
                ) as skl_mock,
            ):
                factory.get_server_repository()
                factory.get_skill_repository()
                assert srv_mock.called
                assert skl_mock.called
        finally:
            factory.reset_repositories()

    async def test_legacy_mongo_server_repository_refuses_to_construct_in_spine_mode(
        self, tmp_path, monkeypatch
    ):
        import registry.core.config as config_module
        from registry.core.config import Settings

        test_settings = Settings(
            secret_key="x" * 40,
            auth_server_nginx_marker_secret="y" * 40,
            spine_replica_mode=True,
            spine_projection_url="http://facet.test",
            spine_projection_token="t1",
            supply_store_write_url="http://facet.test",
            supply_store_write_token="t2",
        )
        monkeypatch.setattr(config_module, "settings", test_settings)

        from registry.repositories.documentdb.server_repository import (
            DocumentDBServerRepository,
        )
        from registry.repositories.documentdb.skill_repository import (
            DocumentDBSkillRepository,
        )

        with pytest.raises(RuntimeError, match="SUPPLY_LEGACY_DIRECT_WRITE"):
            DocumentDBServerRepository()
        with pytest.raises(RuntimeError, match="SUPPLY_LEGACY_DIRECT_WRITE"):
            DocumentDBSkillRepository()

    async def test_legacy_repos_construct_normally_without_spine_mode(self, monkeypatch):
        import registry.core.config as config_module
        from registry.core.config import Settings

        test_settings = Settings(
            secret_key="x" * 40,
            auth_server_nginx_marker_secret="y" * 40,
            spine_replica_mode=False,
        )
        monkeypatch.setattr(config_module, "settings", test_settings)

        from registry.repositories.documentdb.server_repository import (
            DocumentDBServerRepository,
        )

        # Construction must not connect (lazy collection acquisition) — only
        # the spine guard is evaluated at __init__ time.
        repo = DocumentDBServerRepository()
        assert repo is not None


class TestSpineStructuralError:
    async def test_structural_error_carries_legacy_direct_write_code(self):
        err = SpineStructuralError("no spine semantics for this operation")
        assert err.code == "SUPPLY_LEGACY_DIRECT_WRITE"
        assert "spine" in err.message
