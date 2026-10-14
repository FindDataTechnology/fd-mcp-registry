"""Unit tests for GET /api/public/summary (registry-product-home).

The summary serves the anonymous landing page. Contract under test:

- Anonymous access returns 200 with counts + featured cards.
- Counts cover ONLY public+enabled records.
- Non-public/disabled records never appear (no counts, no cards, no existence
  signal).
- Curated featured entries that are not currently public+enabled are silently
  dropped; the endpoint still returns the rest.
- Card bodies are allowlisted: no backend URLs or internal fields.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api import public_record_routes


def _client():
    app = FastAPI()
    app.include_router(public_record_routes.router, prefix="/api")
    return TestClient(app)


@pytest.fixture()
def pools():
    """One public+enabled record and one private record per entity kind."""
    servers = {
        "/weather/": {
            "path": "/weather/",
            "server_name": "Weather",
            "display_name": "Weather MCP",
            "description": "Realtime weather lookups",
            "tags": ["weather", "tools"],
            "is_enabled": True,
            "visibility": "public",
            "proxy_pass_url": "http://weather-backend:9000",
        },
        "/secret-srv/": {
            "path": "/secret-srv/",
            "server_name": "SecretSrv",
            "display_name": "Secret Server",
            "description": "Internal only",
            "tags": ["internal"],
            "is_enabled": True,
            "visibility": "private",
            "proxy_pass_url": "http://secret-backend:9000",
        },
    }
    agents = {
        "/writer/": {
            "path": "/writer/",
            "name": "writer",
            "display_name": "Writer Agent",
            "description": "Drafts long-form copy",
            "tags": ["writing"],
            "is_enabled": True,
            "visibility": "public",
            "url": "http://writer-backend:8080",
        },
    }
    skills = [
        SimpleNamespace(
            path="/pdf-processing",
            name="pdf-processing",
            display_name="PDF Processing",
            description="Split, merge and OCR PDFs",
            tags=["docs"],
            is_enabled=True,
            visibility="public",
            model_dump=lambda mode="json": {
                "path": "/pdf-processing",
                "name": "pdf-processing",
                "display_name": "PDF Processing",
                "description": "Split, merge and OCR PDFs",
                "tags": ["docs"],
                "is_enabled": True,
                "visibility": "public",
            },
        )
    ]
    return SimpleNamespace(servers=servers, agents=agents, skills=skills)


@pytest.fixture()
def patched(pools):
    # The mock simulates the DB-level filter, so a drift in the endpoint's
    # filter shape (e.g. dropping visibility="public") surfaces as a leak in
    # the leak tests instead of passing vacuously.
    async def _find_servers(filter_dict, limit=None, **kw):
        return {
            p: r
            for p, r in pools.servers.items()
            if all(r.get(k) == v for k, v in filter_dict.items())
        }

    async def _find_agents(filter_dict, limit=None, **kw):
        return {
            p: r
            for p, r in pools.agents.items()
            if all(r.get(k) == v for k, v in filter_dict.items())
        }

    async def _list_skills(include_disabled=False, visibility=None, registry_name=None, **kw):
        return [
            s
            for s in pools.skills
            if (include_disabled or s.is_enabled)
            and (visibility is None or s.visibility == visibility)
        ]

    server_repo = SimpleNamespace(find_with_filter=AsyncMock(side_effect=_find_servers))
    agent_repo = SimpleNamespace(find_with_filter=AsyncMock(side_effect=_find_agents))
    skill_repo = SimpleNamespace(list_filtered=AsyncMock(side_effect=_list_skills))
    with (
        patch.object(public_record_routes, "get_server_repository", return_value=server_repo),
        patch.object(public_record_routes, "get_agent_repository", return_value=agent_repo),
        patch.object(public_record_routes, "get_skill_repository", return_value=skill_repo),
    ):
        yield SimpleNamespace(server_repo=server_repo, agent_repo=agent_repo, skill_repo=skill_repo)


@pytest.fixture()
def featured_asset(tmp_path, monkeypatch):
    """Point the featured-asset path at a temp file and return its writer."""
    path = tmp_path / "featured.json"

    def write(doc):
        path.write_text(json.dumps(doc))

    monkeypatch.setattr(public_record_routes, "_FEATURED_JSON_PATH", str(path))
    return write


class TestSummaryCounts:
    def test_anonymous_200_with_counts(self, patched, featured_asset):
        resp = _client().get("/api/public/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"] == {"servers": 1, "agents": 1, "skills": 1}
        assert body["featured"] == []

    def test_non_public_never_leaks_into_counts(self, patched, featured_asset):
        with patch.object(
            public_record_routes,
            "_load_featured_config",
            return_value=[{"kind": "servers", "path": "/secret-srv/"}],
        ):
            body = _client().get("/api/public/summary").json()
        assert body["counts"]["servers"] == 1  # only /weather/
        assert all(card["path"] != "/secret-srv/" for card in body["featured"])


class TestSummaryFeatured:
    def test_featured_public_record_projects_allowlisted_card(self, patched, featured_asset):
        featured_asset(
            {
                "featured": [
                    {"kind": "servers", "path": "/weather/"},
                    {"kind": "skills", "path": "/pdf-processing"},
                ]
            }
        )
        body = _client().get("/api/public/summary").json()
        cards = {c["path"]: c for c in body["featured"]}
        assert set(cards) == {"/weather/", "/pdf-processing"}
        weather = cards["/weather/"]
        assert weather["displayName"] == "Weather MCP"
        assert weather["description"] == "Realtime weather lookups"
        assert weather["tags"] == ["weather", "tools"]
        # Allowlist: no backend URL / proxy fields anywhere in the card.
        assert "proxy_pass_url" not in json.dumps(weather)
        assert "backend" not in json.dumps(body)

    def test_curated_but_not_public_is_silently_dropped(self, patched, featured_asset):
        featured_asset(
            {
                "featured": [
                    {"kind": "servers", "path": "/secret-srv/"},  # private
                    {"kind": "servers", "path": "/ghost/"},  # absent
                    {"kind": "servers", "path": "/weather/"},  # fine
                ]
            }
        )
        body = _client().get("/api/public/summary").json()
        assert [c["path"] for c in body["featured"]] == ["/weather/"]

    def test_all_featured_invalid_still_returns_counts(self, patched, featured_asset):
        featured_asset({"featured": [{"kind": "agents", "path": "/ghost/"}]})
        body = _client().get("/api/public/summary").json()
        assert body["counts"] == {"servers": 1, "agents": 1, "skills": 1}
        assert body["featured"] == []

    def test_malformed_or_absent_asset_degrades_to_no_featured(self, patched, featured_asset):
        # Absent file.
        body = _client().get("/api/public/summary").json()
        assert body["featured"] == []
        # Malformed JSON.
        (feature_path := public_record_routes._FEATURED_JSON_PATH) and None
        with open(public_record_routes._FEATURED_JSON_PATH, "w") as f:
            f.write("{not json")
        body = _client().get("/api/public/summary").json()
        assert body["counts"]["servers"] == 1
        assert body["featured"] == []


class TestSummaryContractShape:
    def test_skill_card_has_no_sensitive_fields(self, patched, featured_asset):
        featured_asset({"featured": [{"kind": "skills", "path": "/pdf-processing"}]})
        card = _client().get("/api/public/summary").json()["featured"][0]
        for forbidden in ("allowed_groups", "owner", "auth_scheme", "skill_md_url"):
            assert forbidden not in card
