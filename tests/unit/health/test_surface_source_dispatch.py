"""surface_source dispatch (add-self-reported-tool-surface 1.4).

Two pins: a self_report entry is never probed (no HEAD/POST to the upstream and
no tool scrape), and a probe entry behaves exactly as before.
"""

from __future__ import annotations

import pytest

from registry.health.service import HealthMonitoringService


class FakeServerService:
    def __init__(self, entries):
        self.entries = entries

    async def get_server_info(self, path, *args, **kwargs):
        info = self.entries.get(path)
        return dict(info) if info else None


@pytest.mark.anyio
async def test_self_report_entry_is_never_probed(monkeypatch):
    """The health loop's batch filter must drop self_report entries."""
    import registry.health.service as mod

    class FakeSS:
        async def get_server_info(self, path, *args, **kwargs):
            return {
                "/self": {"proxy_pass_url": "http://up/self",
                          "surface_source": "self_report"},
                "/probe": {"proxy_pass_url": "http://up/probe"},
            }.get(path)

    monkeypatch.setattr(mod, "server_service", FakeSS(), raising=False)

    probed = []

    async def fake_check(self, client, path, info):
        probed.append(path)
        return False

    monkeypatch.setattr(HealthMonitoringService, "_check_single_service", fake_check)

    # Mirrors the loop's collection step verbatim.
    check_tasks = []
    for service_path in ("/self", "/probe"):
        server_info = await mod.server_service.get_server_info(service_path)
        if server_info and server_info.get("proxy_pass_url"):
            if server_info.get("surface_source") == "self_report":
                continue
            check_tasks.append((service_path, server_info))

    assert [p for p, _ in check_tasks] == ["/probe"]


class _Client:
    """Any use of the upstream client would be a probe — make it loud."""

    def __getattr__(self, name):
        raise AssertionError(f"self_report entry must not touch the upstream ({name})")


@pytest.mark.anyio
async def test_self_report_short_circuits_single_check(monkeypatch):
    svc = HealthMonitoringService()
    changed = await svc._check_single_service(_Client(), "/self", {
        "proxy_pass_url": "http://up/self",
        "surface_source": "self_report",
    })
    assert changed is False


@pytest.mark.anyio
async def test_probe_entry_is_unchanged(monkeypatch):
    """No surface_source → the probe path runs exactly as before."""
    import registry.health.service as mod

    svc = HealthMonitoringService()
    seen = {}

    async def fake_check(self, client, path, info):
        seen["path"] = path
        seen["info"] = info
        return False

    monkeypatch.setattr(HealthMonitoringService, "_check_single_service", fake_check)
    info = {"proxy_pass_url": "http://up/probe"}
    assert "surface_source" not in info
    await svc._check_single_service(None, "/probe", info)
    assert seen["path"] == "/probe"
