"""Shared spine adapter holder: replica + clients + scheduler in one place.

The factory constructs the repositories and the application lifespan starts
the scheduler from this single process-wide adapter, so the puller is the
replica's single writer by construction.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ...core.config import settings
from .projection_client import SpineProjectionClient
from .replica import SpineReplicaStore
from .sync_scheduler import SpineReplicaSyncScheduler
from .write_gate import SpineWriteGateClient

logger = logging.getLogger(__name__)


class SpineAdapter:
    """Bundle of the spine-side collaborators the repositories share."""

    def __init__(
        self,
        replica: SpineReplicaStore,
        projection: SpineProjectionClient,
        gate: SpineWriteGateClient,
        scheduler: SpineReplicaSyncScheduler,
    ):
        self.replica = replica
        self.projection = projection
        self.gate = gate
        self.scheduler = scheduler

    async def resync_after_write(self) -> None:
        """Best-effort immediate pull so reads reflect a just-landed write.

        Preserves the read-after-write behaviour callers had against the
        legacy store. A failed resync keeps the replica last-good/stale (the
        write still landed on the spine; the next scheduled pull converges).
        """
        try:
            await self.scheduler.pull_once()
        except Exception as exc:  # noqa: BLE001 - resync must never break a write
            logger.warning("Post-write replica resync failed (kept last-good): %s", exc)

    def status(self) -> dict[str, Any]:
        """Replica freshness state for the health/status surface."""
        return self.replica.status()


_adapter: SpineAdapter | None = None


def get_spine_adapter() -> SpineAdapter:
    """Process-wide spine adapter singleton (replica + clients + scheduler)."""
    global _adapter
    if _adapter is not None:
        return _adapter
    replica = SpineReplicaStore(settings.spine_replica_db_file)
    shared_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=4),
    )
    projection = SpineProjectionClient(
        base_url=settings.spine_projection_url,
        token=settings.spine_projection_token,
        client=shared_client,
    )
    gate = SpineWriteGateClient(
        base_url=settings.supply_store_write_url,
        token=settings.supply_store_write_token,
        client=shared_client,
    )
    scheduler = SpineReplicaSyncScheduler(
        replica=replica,
        client=projection,
        interval_seconds=settings.spine_replica_sync_interval,
    )
    _adapter = SpineAdapter(replica, projection, gate, scheduler)
    logger.info("Spine adapter initialized (replica=%s)", settings.spine_replica_db_file)
    return _adapter


def reset_spine_adapter() -> None:
    """Drop the singleton. USE ONLY IN TESTS."""
    global _adapter
    _adapter = None
