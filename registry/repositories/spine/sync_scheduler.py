"""Periodic projection puller — the replica's single writer (task 2.1).

Runs on the registry's existing refresh cadence (defaults to
``health_check_interval_seconds``; overridable via
``SPINE_REPLICA_SYNC_INTERVAL_SECONDS``). Each tick pulls the projection and
replaces the replica wholesale (~28 rows; incremental ``?since=`` pulls are
supported by the spine but deliberately unused here — last-good + stale is
the failure mode that matters, and full pulls make it exact).

Failure semantics (design D2): a failed pull keeps the previous rows
(last-good) and marks the replica stale — surfaced by
:meth:`SpineReplicaStore.status` (wired into ``/api/stats`` as
``spine_replica_status``) and logged at WARN. Staleness is never faked
healthy; the status surface states it explicitly.
"""

from __future__ import annotations

import asyncio
import logging

from ...core.config import settings
from .projection_client import SpineProjectionClient, SpineProjectionError
from .replica import SpineReplicaStore

logger = logging.getLogger(__name__)


class SpineReplicaSyncScheduler:
    """Background task that keeps the local replica fresh from the spine."""

    def __init__(
        self,
        replica: SpineReplicaStore,
        client: SpineProjectionClient,
        interval_seconds: float,
    ):
        self._replica = replica
        self._client = client
        self._interval = max(1.0, float(interval_seconds))
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the pull loop; the first pull runs immediately."""
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="spine-replica-sync")
        logger.info(
            "Spine replica sync started (interval=%.1fs, projection=%s)",
            self._interval,
            settings.spine_projection_url,
        )

    async def stop(self) -> None:
        """Stop the pull loop and wait for the current tick to finish."""
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def pull_once(self) -> bool:
        """Run one pull cycle. Returns True when the replica was refreshed."""
        try:
            payload = await self._client.pull()
        except SpineProjectionError as exc:
            self._replica.mark_pull_failed(str(exc))
            return False
        mcp_rows = payload.get("mcpServers") or []
        skill_rows = payload.get("skills") or []
        self._replica.replace_snapshot(
            mcp_rows=mcp_rows,
            skill_rows=skill_rows,
            as_of=payload.get("asOf"),
        )
        logger.info(
            "Spine replica refreshed: %d mcp rows, %d skill rows (asOf=%s)",
            len(mcp_rows),
            len(skill_rows),
            payload.get("asOf"),
        )
        return True

    async def _loop(self) -> None:
        while True:
            await self.pull_once()
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue
