"""Unit tests for the spine replica store (add-supply-store-unification 2.1).

Covers the read-only replica contract: snapshot replacement is the only
writer, failed pulls keep last-good and mark stale honestly, and the status
surface reports freshness without inventing health.
"""

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from registry.repositories.spine.replica import SpineReplicaStore  # noqa: E402

_MCP_ROW: dict[str, Any] = {
    "id": "/law-bench",
    "display_name": "Law Bench",
    "description": "legal benchmarks",
    "proxy_pass_url": "http://upstream:8000/mcp",
    "num_tools": 3,
    "tool_list": [{"name": "t1", "description": "d"}],
    "tags": ["legal"],
    "license": "MIT",
    "provider_organization": "finddata",
    "deployment": "remote",
    "metadata": {"credential_ref": "lex", "annotations": {"zh-CN": "法"},"surface": {}},
    "visibility": "public",
    "groups": [],
    "is_enabled": 1,
    "created_at": 1700000000000,
    "updated_at": 1700000001000,
    "updated_by": "ops@finddata",
}

_SKILL_ROW: dict[str, Any] = {
    "name": "/skills/pdf-processing",
    "display_name": "PDF Processing",
    "description": "process pdfs",
    "repository_url": "https://github.com/x/y",
    "author": "someone",
    "version": "1.2.0",
    "tags": ["docs"],
    "license": "MIT",
    "metadata": {"skill_md_url": "https://github.com/x/y/SKILL.md"},
    "visibility": "public",
    "groups": ["team-a"],
    "is_enabled": 0,
    "created_at": 1700000000000,
    "updated_at": 1700000002000,
    "updated_by": "ops@finddata",
}


@pytest.fixture
def store(tmp_path):
    replica = SpineReplicaStore(tmp_path / "replica.db")
    yield replica
    replica.close()


class TestReplicaSnapshot:
    async def test_initial_state_is_stale_and_empty(self, store):
        status = store.status()
        assert status["empty"] is True
        assert status["stale"] == 1
        assert status["mcp_count"] == 0

    async def test_replace_snapshot_is_the_only_write_path(self, store):
        store.replace_snapshot([_MCP_ROW], [_SKILL_ROW], as_of=1700000009000)
        status = store.status()
        assert status["stale"] == 0
        assert status["mcp_count"] == 1
        assert status["skill_count"] == 1
        assert status["as_of"] == 1700000009000

        # A second snapshot replaces wholesale (single-writer overwrite).
        store.replace_snapshot([], [], as_of=1700000010000)
        status = store.status()
        assert status["mcp_count"] == 0
        assert status["skill_count"] == 0

    async def test_get_rows_round_trip(self, store):
        store.replace_snapshot([_MCP_ROW], [_SKILL_ROW], as_of=1)
        assert store.get_mcp_row("/law-bench")["display_name"] == "Law Bench"
        assert store.get_mcp_row("/missing") is None
        assert store.get_skill_row("/skills/pdf-processing")["version"] == "1.2.0"
        assert len(store.list_mcp_rows()) == 1
        assert len(store.list_skill_rows()) == 1

    async def test_replace_snapshot_rejects_malformed_rows(self, store):
        # A row that cannot JSON-serialize must fail the whole snapshot before
        # any write lands: rollback keeps the previous copy (last-good).
        store.replace_snapshot([_MCP_ROW], [], as_of=1)
        broken = [{"id": "/bad", "tool_list": "not-a-jsonable-object-set"}]
        with pytest.raises(TypeError):
            store.replace_snapshot(
                [{"id": object()}], broken, as_of=2
            )
        # last-good intact
        assert store.get_mcp_row("/law-bench") is not None
        assert store.status()["stale"] == 0


class TestReplicaStaleSemantics:
    async def test_failed_pull_keeps_last_good_and_marks_stale(self, store):
        store.replace_snapshot([_MCP_ROW], [_SKILL_ROW], as_of=1700000009000)
        store.mark_pull_failed("projection unreachable: connect timeout")

        status = store.status()
        assert status["stale"] == 1
        assert status["consecutive_failures"] == 1
        assert "unreachable" in status["last_error"]
        # Rows survive: serving continues from the previous copy.
        assert store.get_mcp_row("/law-bench") is not None
        assert store.get_skill_row("/skills/pdf-processing") is not None
        assert status["mcp_count"] == 1

    async def test_consecutive_failures_accumulate_and_recovery_clears(self, store):
        store.mark_pull_failed("e1")
        store.mark_pull_failed("e2")
        assert store.status()["consecutive_failures"] == 2

        store.replace_snapshot([], [], as_of=42)
        status = store.status()
        assert status["stale"] == 0
        assert status["consecutive_failures"] == 0
        assert status["last_error"] is None

    async def test_status_never_invents_health_before_first_pull(self, store):
        status = store.status()
        assert status["empty"] is True
        assert status["stale"] == 1
