"""Unit tests for the spine write-gate HTTP client and env fail-fast guard.

- The gate client posts the contract's body shapes to the contract's routes
  and carries error responses back verbatim (status + SUPPLY_* code).
- SPINE_REPLICA_MODE=1 with missing endpoints fails Settings construction
  (fail-fast, egress-vault precedent); unset mode keeps booting unchanged.
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from registry.repositories.spine.write_gate import (  # noqa: E402
    SpineGateError,
    SpineGateUnreachableError,
    SpineWriteGateClient,
)


def gate_client(handler) -> SpineWriteGateClient:
    return SpineWriteGateClient(
        "http://facet.test",
        "write-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestGateClientRoutes:
    async def test_mcp_upsert_posts_contract_body(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        client = gate_client(handler)
        await client.mcp_upsert("ops@finddata", {"id": "/x", "proxy_pass_url": "http://u"})
        assert captured["url"] == "http://facet.test/api/supply-store/mcp-servers/upsert"
        assert captured["auth"] == "Bearer write-token"
        assert captured["body"] == {
            "actor": "ops@finddata",
            "source": "facade",
            "entry": {"id": "/x", "proxy_pass_url": "http://u"},
        }

    async def test_mcp_patch_and_visibility_and_delete_routes(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.path))
            return httpx.Response(200, json={"ok": True})

        client = gate_client(handler)
        await client.mcp_patch("a", "/x", {"display_name": "n"})
        await client.mcp_visibility("a", "/x", "private", ["g"])
        await client.mcp_delete("a", "/x")
        assert seen == [
            "/api/supply-store/mcp-servers/patch",
            "/api/supply-store/mcp-servers/visibility",
            "/api/supply-store/mcp-servers/delete",
        ]

    async def test_skill_routes_use_name_field(self):
        bodies: list[dict] = []
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            paths.append(str(request.url.path))
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        client = gate_client(handler)
        await client.skill_patch("a", "/skills/x", {"display_name": "n"})
        await client.skill_delete("a", "/skills/x")
        assert paths == [
            "/api/supply-store/skills/patch",
            "/api/supply-store/skills/delete",
        ]
        assert bodies[0]["name"] == "/skills/x"
        assert bodies[1]["name"] == "/skills/x"

    async def test_error_responses_carry_status_and_code_verbatim(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": "write gate requires the supply-store write token",
                    "code": "SUPPLY_UNAUTHORIZED",
                },
            )

        client = gate_client(handler)
        with pytest.raises(SpineGateError) as excinfo:
            await client.mcp_upsert("a", {"id": "/x"})
        assert excinfo.value.status == 401
        assert excinfo.value.code == "SUPPLY_UNAUTHORIZED"

    async def test_transport_failure_raises_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = gate_client(handler)
        with pytest.raises(SpineGateUnreachableError):
            await client.mcp_upsert("a", {"id": "/x"})


class TestEnvFailFast:
    @pytest.fixture(autouse=True)
    def _isolate_spine_env(self, monkeypatch):
        """Strip the ambient spine env so each test states its own mode.

        pydantic-settings reads SPINE_*/SUPPLY_STORE_* at Settings()
        construction; without this fixture a developer-exported
        SPINE_REPLICA_MODE=1 would leak into the "unset mode" case.
        """
        for name in (
            "SPINE_REPLICA_MODE",
            "SPINE_PROJECTION_URL",
            "SPINE_PROJECTION_TOKEN",
            "SUPPLY_STORE_WRITE_URL",
            "SUPPLY_STORE_WRITE_TOKEN",
            "SPINE_REPLICA_DB_PATH",
            "SPINE_REPLICA_SYNC_INTERVAL_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        yield

    def _settings(self, **overrides):
        from registry.core.config import Settings

        defaults = {
            "secret_key": "x" * 40,
            "auth_server_nginx_marker_secret": "y" * 40,
        }
        defaults.update(overrides)
        return Settings(**defaults)

    def test_unset_mode_boots_without_spine_env(self):
        settings = self._settings()
        assert settings.spine_replica_mode is False

    def test_mode_on_without_endpoints_fails_fast(self):
        with pytest.raises(ValueError, match="SPINE_REPLICA_MODE=1 requires"):
            self._settings(spine_replica_mode=True)

    def test_mode_on_with_all_endpoints_boots(self):
        settings = self._settings(
            spine_replica_mode=True,
            spine_projection_url="http://facet:3200",
            spine_projection_token="t1",
            supply_store_write_url="http://facet:3200",
            supply_store_write_token="t2",
        )
        assert settings.spine_replica_sync_interval > 0

    def test_mode_on_missing_one_token_names_it(self):
        with pytest.raises(ValueError, match="SUPPLY_STORE_WRITE_TOKEN"):
            self._settings(
                spine_replica_mode=True,
                spine_projection_url="http://facet:3200",
                spine_projection_token="t1",
                supply_store_write_url="http://facet:3200",
            )

    def test_sync_interval_defaults_to_health_check_beat(self):
        settings = self._settings(
            spine_replica_mode=True,
            spine_projection_url="http://facet:3200",
            spine_projection_token="t1",
            supply_store_write_url="http://facet:3200",
            supply_store_write_token="t2",
            health_check_interval_seconds=120,
        )
        assert settings.spine_replica_sync_interval == 120
        assert settings.spine_replica_db_file.endswith("spine_replica.db")

    def test_explicit_interval_and_db_path_win(self):
        settings = self._settings(
            spine_replica_mode=True,
            spine_projection_url="http://facet:3200",
            spine_projection_token="t1",
            supply_store_write_url="http://facet:3200",
            supply_store_write_token="t2",
            spine_replica_sync_interval_seconds=42,
            spine_replica_db_path="/tmp/custom.db",
        )
        assert settings.spine_replica_sync_interval == 42
        assert settings.spine_replica_db_file == "/tmp/custom.db"
