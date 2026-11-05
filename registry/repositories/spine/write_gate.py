"""HTTP client for the spine write gate (facade write semantics, design D3).

Registry write methods forward to the spine write gate and carry the gate's
validation/audit result back as-is: :class:`SpineGateError` re-raises the
gate's status/code/message verbatim so callers see the same error classes a
direct gate client would see (401 ``SUPPLY_UNIDENTIFIED_WRITE`` /
``SUPPLY_UNAUTHORIZED``, 400 ``SUPPLY_LEGACY_DIRECT_WRITE`` /
``SUPPLY_UNKNOWN_FIELD`` / ``SUPPLY_BAD_ENTRY``, 404 ``SUPPLY_NOT_FOUND``).

Routes (facet ``registerSupplyStoreRoutes``, contract locked by tests there):
    POST {base}/api/supply-store/mcp-servers/{upsert|patch|visibility|delete}
    POST {base}/api/supply-store/skills/{upsert|patch|visibility|delete}
Auth: ``Authorization: Bearer {SUPPLY_STORE_WRITE_TOKEN}``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_WRITE_TIMEOUT_SECONDS = 15.0


class SpineGateError(Exception):
    """A spine write-gate rejection, carried verbatim to the caller.

    Attributes:
        status: HTTP status returned by the gate.
        code: Machine-readable error code (``SUPPLY_*``).
        message: Human-readable gate message.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class SpineGateUnreachableError(Exception):
    """The spine write gate could not be reached at all.

    Distinct from :class:`SpineGateError`: the gate never rendered a verdict,
    so the write is *not* known to have landed. Callers surface this as a
    502-class failure rather than a validation result.
    """


class SpineWriteGateClient:
    """Thin async client over the spine write gate routes."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        client = self._client
        owned = client is None
        if owned:
            client = httpx.AsyncClient(timeout=_WRITE_TIMEOUT_SECONDS)
        try:
            response = await client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise SpineGateUnreachableError(
                f"spine write gate unreachable at {self._base_url}{path}: {exc}"
            ) from exc
        finally:
            if owned:
                await client.aclose()

        if response.status_code >= 400:
            body = _safe_json(response)
            raise SpineGateError(
                status=response.status_code,
                code=str(body.get("code", "SUPPLY_GATE_ERROR")),
                message=str(body.get("error", response.text[:200])),
            )
        return _safe_json(response)

    async def mcp_upsert(
        self,
        actor: str,
        entry: dict[str, Any],
        source: str = "facade",
    ) -> dict[str, Any] | None:
        """Create/replace one mcp_servers row through the gate."""
        return await self._post(
            "/api/supply-store/mcp-servers/upsert",
            {"actor": actor, "source": source, "entry": entry},
        )

    async def mcp_patch(
        self,
        actor: str,
        server_id: str,
        fields: dict[str, Any],
        source: str = "facade",
    ) -> dict[str, Any] | None:
        """Patch named columns of one mcp_servers row through the gate."""
        return await self._post(
            "/api/supply-store/mcp-servers/patch",
            {"actor": actor, "source": source, "id": server_id, "fields": fields},
        )

    async def mcp_visibility(
        self,
        actor: str,
        server_id: str,
        visibility: str | None,
        groups: list[str] | None,
        source: str = "facade",
    ) -> dict[str, Any] | None:
        """Set one mcp_servers row's visibility/groups through the gate."""
        return await self._post(
            "/api/supply-store/mcp-servers/visibility",
            {
                "actor": actor,
                "source": source,
                "id": server_id,
                "visibility": visibility,
                "groups": groups,
            },
        )

    async def mcp_delete(self, actor: str, server_id: str, source: str = "facade") -> None:
        """Delete one mcp_servers row through the gate."""
        await self._post(
            "/api/supply-store/mcp-servers/delete",
            {"actor": actor, "source": source, "id": server_id},
        )

    async def skill_upsert(
        self,
        actor: str,
        entry: dict[str, Any],
        source: str = "facade",
    ) -> dict[str, Any] | None:
        """Create/replace one skills row through the gate."""
        return await self._post(
            "/api/supply-store/skills/upsert",
            {"actor": actor, "source": source, "entry": entry},
        )

    async def skill_patch(
        self,
        actor: str,
        skill_name: str,
        fields: dict[str, Any],
        source: str = "facade",
    ) -> dict[str, Any] | None:
        """Patch named columns of one skills row through the gate."""
        return await self._post(
            "/api/supply-store/skills/patch",
            {"actor": actor, "source": source, "name": skill_name, "fields": fields},
        )

    async def skill_visibility(
        self,
        actor: str,
        skill_name: str,
        visibility: str | None,
        groups: list[str] | None,
        source: str = "facade",
    ) -> dict[str, Any] | None:
        """Set one skills row's visibility/groups through the gate."""
        return await self._post(
            "/api/supply-store/skills/visibility",
            {
                "actor": actor,
                "source": source,
                "name": skill_name,
                "visibility": visibility,
                "groups": groups,
            },
        )

    async def skill_delete(self, actor: str, skill_name: str, source: str = "facade") -> None:
        """Delete one skills row through the gate."""
        await self._post(
            "/api/supply-store/skills/delete",
            {"actor": actor, "source": source, "name": skill_name},
        )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}
