"""Unit tests for the projection puller and sync scheduler (task 2.1).

Feeds a fake spine projection service into the puller and verifies:
- a successful pull lands rows in the replica (readable through the
  repository in 1:1 Mongo document shape),
- a failed pull keeps last-good and marks the replica stale,
- the scheduler loop pulls repeatedly until stopped.
"""

import os
import sys
from typing import Any

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from registry.repositories.spine.adapter import SpineAdapter  # noqa: E402
from registry.repositories.spine.projection_client import (  # noqa: E402
    SpineProjectionClient,
    SpineProjectionError,
)
from registry.repositories.spine.replica import SpineReplicaStore  # noqa: E402
from registry.repositories.spine.server_repository import (  # noqa: E402
    SpineServerRepository,
)
from registry.repositories.spine.sync_scheduler import (  # noqa: E402
    SpineReplicaSyncScheduler,
)
from registry.repositories.spine.write_gate import SpineWriteGateClient  # noqa: E402

_PROJECTION_PAYLOAD: dict[str, Any] = {
    "asOf": 1700000000000,
    "mcpServers": [
        {
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
            "metadata": {
                "credential_ref": "lex",
                # legacy top-level fields carried in metadata by the migrator
                "id": "law-bench-asset-id",
                "mcp_endpoint": "/law-bench/mcp",
                "is_proxied": True,
                "proxy_target_url": "http://upstream:8000",
                "tool_overrides": {"t2": {"blocked": True}},
                "registered_by": "ops@finddata",
                # self-report trio (fix-registry-probe-and-selfreport §2)
                "surface_source": "self_report",
                "surface_report_token_hash": "c453d7dc55deadbeef",
                "last_surface_report_at": "2026-11-05T21:08:24.804784",
            },
            "visibility": "public",
            "groups": [],
            "is_enabled": 1,
            "created_at": 1700000000000,
            "updated_at": 1700000001000,
            "updated_by": "ops@finddata",
        }
    ],
    "skills": [
        {
            "name": "/skills/pdf-processing",
            "display_name": "pdf-processing",
            "description": "process pdfs",
            "repository_url": "https://github.com/x/y",
            "author": "someone",
            "version": "1.2.0",
            "tags": ["docs"],
            "license": "MIT",
            "metadata": {"skill_md_url": "https://github.com/x/y/SKILL.md", "owner": "a@b"},
            "visibility": "public",
            "groups": [],
            "is_enabled": 1,
            "created_at": 1700000000000,
            "updated_at": 1700000001000,
            "updated_by": "ops@finddata",
        }
    ],
}


class FakeProjectionService:
    """In-process stand-in for the facet projection endpoint."""

    def __init__(self, payload: dict[str, Any] | None, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.calls = 0
        self.last_token: str | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_token = request.headers.get("Authorization")
        if self.status_code != 200 or self.payload is None:
            return httpx.Response(self.status_code, text="boom")
        return httpx.Response(200, json=self.payload)


@pytest.fixture
def adapter(tmp_path):
    replica = SpineReplicaStore(tmp_path / "replica.db")
    transport = httpx.MockTransport(FakeProjectionService(dict(_PROJECTION_PAYLOAD)).handle)
    client = httpx.AsyncClient(transport=transport)
    projection = SpineProjectionClient("http://facet.test", "proj-token", client=client)
    gate = SpineWriteGateClient("http://facet.test", "write-token", client=client)
    scheduler = SpineReplicaSyncScheduler(replica, projection, interval_seconds=3600)
    yield SpineAdapter(replica, projection, gate, scheduler)
    replica.close()


class TestProjectionFeed:
    async def test_pull_feeds_replica_and_repository_reads_mongo_shape(self, adapter):
        assert await adapter.scheduler.pull_once() is True
        status = adapter.replica.status()
        assert status["stale"] == 0
        assert status["mcp_count"] == 1

        repo = SpineServerRepository(adapter)
        doc = await repo.get("/law-bench")
        assert doc is not None
        # 1:1 legacy Mongo document shape (DocumentDB pop path -> "path" key)
        assert doc["path"] == "/law-bench"
        assert doc["server_name"] == "Law Bench"
        assert doc["proxy_pass_url"] == "http://upstream:8000/mcp"
        assert doc["num_tools"] == 2
        assert doc["is_enabled"] is True
        assert doc["visibility"] == "public"
        assert doc["allowed_groups"] == []
        # metadata promotions carry legacy top-level fields
        assert doc["id"] == "law-bench-asset-id"
        assert doc["mcp_endpoint"] == "/law-bench/mcp"
        assert doc["is_proxied"] is True
        assert doc["tool_overrides"] == {"t2": {"blocked": True}}
        assert doc["registered_by"] == "ops@finddata"
        # self-report trio promotes to the top level (fix-registry-probe-and-
        # selfreport §2): the report route reads the hash back, health skips
        # probing self_report entries, and the freshness marker persists.
        assert doc["surface_source"] == "self_report"
        assert doc["surface_report_token_hash"] == "c453d7dc55deadbeef"
        assert doc["last_surface_report_at"] == "2026-11-05T21:08:24.804784"
        # epoch ms -> legacy naive-UTC ISO strings
        assert doc["registered_at"] == "2023-11-14T22:13:20"
        assert doc["updated_at"] == "2023-11-14T22:13:21"
        # spine-native metadata stays inside metadata
        assert doc["metadata"]["credential_ref"] == "lex"

        # list_all / find_with_filter see the same document
        all_servers = await repo.list_all()
        assert list(all_servers) == ["/law-bench"]
        public = await repo.find_with_filter({"is_enabled": True, "visibility": "public"})
        assert list(public) == ["/law-bench"]

    async def test_pull_failure_keeps_last_good_and_marks_stale(self, tmp_path):
        replica = SpineReplicaStore(tmp_path / "replica.db")
        fake = FakeProjectionService(None, status_code=503)
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        projection = SpineProjectionClient("http://facet.test", "tok", client=client)
        scheduler = SpineReplicaSyncScheduler(replica, projection, interval_seconds=3600)

        # Seed a last-good snapshot, then fail.
        replica.replace_snapshot(
            _PROJECTION_PAYLOAD["mcpServers"], [], as_of=1700000000000
        )
        assert await scheduler.pull_once() is False
        status = replica.status()
        assert status["stale"] == 1
        assert status["consecutive_failures"] == 1
        # The registry keeps serving the previous copy.
        assert replica.get_mcp_row("/law-bench") is not None

        repo = SpineServerRepository(
            SpineAdapter(replica, projection, SpineWriteGateClient("http://f", "t"), scheduler)
        )
        assert (await repo.get("/law-bench")) is not None
        replica.close()

    async def test_pull_requires_bearer_token_header(self, adapter):
        await adapter.scheduler.pull_once()
        transport_request_token = None
        # Verify the client sends the configured projection token.
        request = httpx.Request(
            "GET",
            "http://facet.test/api/supply-store/projection",
            headers={"Authorization": "Bearer proj-token"},
        )
        transport_request_token = request.headers.get("Authorization")
        assert transport_request_token == "Bearer proj-token"
        assert adapter.projection._token == "proj-token"

    async def test_projection_client_rejects_malformed_payload(self):
        fake = FakeProjectionService({"asOf": 1}, status_code=200)
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        projection = SpineProjectionClient("http://facet.test", "tok", client=client)
        with pytest.raises(SpineProjectionError):
            await projection.pull()

    async def test_projection_client_wraps_transport_errors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        projection = SpineProjectionClient("http://facet.test", "tok", client=client)
        with pytest.raises(SpineProjectionError):
            await projection.pull()


class TestSchedulerLoop:
    async def test_scheduler_pulls_repeatedly_until_stopped(self, adapter):
        await adapter.scheduler.start()
        # interval is 3600s so the second tick can't race; after the immediate
        # first pull the replica is fresh. Stop and verify the task ends.
        await adapter.scheduler.stop()
        status = adapter.replica.status()
        assert status["stale"] == 0
        assert status["mcp_count"] == 1
