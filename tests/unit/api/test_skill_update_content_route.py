"""Route-level tests for skill content propagation (PUT + content endpoint).

Covers the fix-skill-content-propagation wire behavior:

* PUT /api/skills/{path} passes an inline ``skill_md_content`` through the
  request schema into the persisted update (the schema used to drop it
  silently), alongside the versioned ``skill_md_url`` the deploy side sends.
* A PUT without the body never carries a clearing key into the service.
* GET /api/skills/{path}/content serves the stored inline body directly
  (source=inline), with no URL fetch on that path.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api import skill_routes
from registry.api.skill_routes import router as skill_router
from registry.auth.dependencies import nginx_proxied_auth
from registry.schemas.skill_models import SkillCard

pytestmark = pytest.mark.unit

SKILL_PATH = "spider-heal-repair"

ADMIN_CTX: dict[str, Any] = {"username": "admin", "is_admin": True, "ui_permissions": {}}

_V1_URL = "https://facet.example/api/packs/p/versions/1/skills/spider-heal-repair.md"
_V2_URL = "https://facet.example/api/packs/p/versions/2/skills/spider-heal-repair.md"


def _skill(**overrides) -> SkillCard:
    data = {
        "path": f"/skills/{SKILL_PATH}",
        "name": SKILL_PATH,
        "description": "Repair spider units",
        "skill_md_url": _V1_URL,
        "skill_md_raw_url": _V1_URL,
        "owner": "admin",
    }
    data.update(overrides)
    return SkillCard(**data)


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(skill_router, prefix="/api")
    app.dependency_overrides[nginx_proxied_auth] = lambda: ADMIN_CTX
    return TestClient(app)


def _put(body: dict) -> tuple[TestClient, MagicMock]:
    service = MagicMock()
    service.get_skill = AsyncMock(return_value=_skill())
    service.update_skill = AsyncMock(return_value=_skill())
    gate = MagicMock(allowed=True, error_message=None)
    with (
        patch.object(skill_routes, "get_skill_service", return_value=service),
        patch.object(skill_routes, "check_registration_gate", AsyncMock(return_value=gate)),
    ):
        client = _make_client()
        resp = client.put(f"/api/skills/{SKILL_PATH}", json=body)
    assert resp.status_code == 200, resp.text
    return client, service


class TestPutCarriesInlineContent:
    """The deploy-side PUT shape reaches the service intact."""

    def test_deploy_put_carries_body_and_versioned_url(self):
        _, service = _put(
            {
                "name": SKILL_PATH,
                "description": "Repair spider units",
                "skill_md_url": _V2_URL,
                "skill_md_content": "# body v2\n",
            }
        )
        updates = service.update_skill.call_args.args[1]
        assert updates["skill_md_content"] == "# body v2\n"
        assert str(updates["skill_md_url"]) == _V2_URL

    def test_put_without_body_has_no_content_key(self):
        _, service = _put(
            {
                "name": SKILL_PATH,
                "description": "New description",
                "skill_md_url": _V1_URL,
            }
        )
        updates = service.update_skill.call_args.args[1]
        assert "skill_md_content" not in updates

    def test_put_empty_string_body_normalizes_to_none(self):
        # The schema normalizes "" to None; the value is then present-but-None
        # in the update dict (exclude_unset keeps explicitly supplied fields)
        # and the SERVICE guard drops it — so it can never clear stored content.
        _, service = _put(
            {
                "name": SKILL_PATH,
                "description": "Repair spider units",
                "skill_md_url": _V1_URL,
                "skill_md_content": "",
            }
        )
        updates = service.update_skill.call_args.args[1]
        assert updates.get("skill_md_content") is None


class TestContentEndpointInlinePreference:
    """GET .../content serves the stored inline body without fetching."""

    def test_inline_body_served_directly(self):
        service = MagicMock()
        stored = _skill(skill_md_content="# hardened body v2\n")
        service.get_skill = AsyncMock(return_value=stored)
        with patch.object(skill_routes, "get_skill_service", return_value=service):
            client = _make_client()
            resp = client.get(f"/api/skills/{SKILL_PATH}/content")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["content"] == "# hardened body v2\n"
        assert payload["source"] == "inline"
