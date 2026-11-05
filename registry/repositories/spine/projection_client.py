"""Pull client for the spine read-only projection endpoint.

Contract (facet ``registerSupplyStoreRoutes``; locked by tests there):
    GET {SPINE_PROJECTION_URL}/api/supply-store/projection[?since=<ms>]
    Authorization: Bearer {SPINE_PROJECTION_TOKEN}
Wrong/missing token -> 401 ``SUPPLY_UNAUTHORIZED``.

Payload: ``{"asOf": <epoch ms>, "mcpServers": [row...], "skills": [row...]}``
where rows use the spine column names (``id``/``name`` primary keys).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PULL_TIMEOUT_SECONDS = 30.0


class SpineProjectionError(Exception):
    """A projection pull failure (network, non-200, or malformed payload)."""


class SpineProjectionClient:
    """Async puller for the spine read-only projection."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def pull(self, since: int | None = None) -> dict[str, Any]:
        """Fetch one projection snapshot.

        Args:
            since: Optional incremental cursor (epoch ms). ``None`` pulls the
                full snapshot (the default; the registry's book is ~28 rows,
                so full pulls stay trivially cheap).

        Returns:
            The raw payload dict with ``asOf``, ``mcpServers``, ``skills``.

        Raises:
            SpineProjectionError: On any transport, status, or shape failure.
        """
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        client = self._client
        owned = client is None
        if owned:
            client = httpx.AsyncClient(timeout=_PULL_TIMEOUT_SECONDS)
        try:
            response = await client.get(
                f"{self._base_url}/api/supply-store/projection",
                params=params or None,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise SpineProjectionError(f"projection unreachable: {exc}") from exc
        finally:
            if owned:
                await client.aclose()

        if response.status_code != 200:
            raise SpineProjectionError(
                f"projection returned HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SpineProjectionError("projection returned non-JSON body") from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("mcpServers"), list
        ) or not isinstance(payload.get("skills"), list):
            raise SpineProjectionError("projection payload malformed (missing row arrays)")
        return payload
