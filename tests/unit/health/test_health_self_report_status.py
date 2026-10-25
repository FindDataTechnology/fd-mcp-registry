"""Health-data semantics for surface_source='self_report' entries.

Companion to test_health_local_short_circuit.py: both are "the probe cannot
apply by design" families (local = no HTTP endpoint; self_report = anonymous
can only 401), and both must be named honestly in the dashboard read path
instead of leaking probe-world status.
"""

from unittest.mock import AsyncMock

import pytest

from registry.constants import HealthStatus
from registry.health.service import HealthMonitoringService


@pytest.mark.unit
class TestSelfReportHealthDataFast:
    """The fast health-data lookup is the read path used by list/dashboard
    endpoints. A self_report entry is probe-exempt by design, so cached probe
    status ('unknown', or a stale pre-exemption 'unhealthy') must not render —
    the entry shows healthy, with freshness carried by
    last_surface_report_at (projected separately), not by a probe time."""

    def test_self_report_shows_healthy_not_stale_cache(self):
        service = HealthMonitoringService()
        # Stale cache from before the exemption existed.
        service.server_health_status["/wire-build-mcp"] = "unhealthy: auth failed"
        server_info = {
            "is_enabled": True,
            "deployment": "remote",
            "surface_source": "self_report",
        }

        result = service._get_service_health_data_fast("/wire-build-mcp", server_info)

        assert result["status"] == HealthStatus.HEALTHY
        assert service.server_health_status["/wire-build-mcp"] == HealthStatus.HEALTHY
        # No probe happened, so no probe timestamp is invented.
        assert result["last_checked_iso"] is None

    def test_self_report_disabled_still_disabled(self):
        """Disabled wins over every provenance branch."""
        service = HealthMonitoringService()
        server_info = {
            "is_enabled": False,
            "deployment": "remote",
            "surface_source": "self_report",
        }

        result = service._get_service_health_data_fast("/wire-build-mcp", server_info)

        assert result["status"] == "disabled"

    def test_probe_entry_keeps_cached_status(self):
        """Sanity: probe (default) entries keep the cached probe status — the
        new branch must not swallow ordinary probe-world entries."""
        service = HealthMonitoringService()
        service.server_health_status["fd-open-data-mcp"] = "checking"
        server_info = {"is_enabled": True, "deployment": "remote"}

        result = service._get_service_health_data_fast("fd-open-data-mcp", server_info)

        assert result["status"] == "checking"


@pytest.mark.unit
@pytest.mark.asyncio
class TestSelfReportProbeExemption:
    """The per-service check short-circuits without touching probe state."""

    async def test_self_report_never_probed(self):
        service = HealthMonitoringService()
        client = AsyncMock()
        server_info = {
            "deployment": "remote",
            "proxy_pass_url": "https://upstream.example.com/mcp",
            "surface_source": "self_report",
        }

        changed = await service._check_single_service(client, "/wire-build-mcp", server_info)

        assert changed is False
        client.get.assert_not_called()
        client.post.assert_not_called()
        # No check timestamp: no check happened.
        assert "/wire-build-mcp" not in service.server_last_check_time
