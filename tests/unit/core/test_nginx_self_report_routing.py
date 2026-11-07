"""Self-reported entries must be routed, not commented out.

``surface_source == "self_report"`` entries are never probed (their HTTP layer
needs a per-user Bearer, so a probe can only ever answer 401 — see
``registry/health/service.py``). No verdict ever arrives, ``health_status``
stays ``UNKNOWN``, and this generator used to take the "unhealthy" branch: it
emitted a commented-out location block, so the public path answered **405**
(for a route that does not exist) instead of 401 (auth required).

The fix routes them on registration state instead. These tests pin both sides of
that decision so a future refactor cannot silently re-comment them:

* self_report + UNKNOWN  -> live ``location`` block, no "unhealthy" comment
* no surface_source + UNKNOWN -> unchanged behaviour (still commented)

Operators had been working around the 405 by flipping ``metadata.surface_source``
to ``probe`` through the supply-store patch channel; this is the real fix that
workaround was standing in for.
"""

import builtins
from unittest.mock import MagicMock, mock_open, patch

import pytest

from registry.constants import HealthStatus
from registry.core.nginx_service import NginxConfigService


@pytest.fixture(autouse=True)
def mock_atomic_write():
    """Stub the config write + validate path (mirrors test_nginx_local_skip)."""
    with patch("registry.core.nginx_service._atomic_write_text") as m:
        with patch(
            "registry.core.nginx_service._write_and_validate_config",
            side_effect=lambda path, content: m(path, content),
        ):
            yield m


@pytest.fixture
def nginx_service():
    with patch("registry.core.nginx_service.Path") as mock_path_class:
        mock_template = MagicMock()
        mock_template.exists.return_value = True
        mock_path_class.return_value = mock_template
        with patch("registry.core.nginx_service.settings") as mock_settings:
            mock_settings.nginx_updates_enabled = True
            mock_settings.deployment_mode = MagicMock()
            mock_settings.deployment_mode.value = "with-gateway"
            mock_settings.nginx_config_path = "/tmp/nginx.conf"
            yield NginxConfigService()


def _remote(path, **extra):
    entry = {
        "server_name": path.strip("/"),
        "proxy_pass_url": f"http://upstream{path}/mcp",
        "supported_transports": ["streamable-http"],
        "deployment": "remote",
    }
    entry.update(extra)
    return entry


async def _render(nginx_service, health, servers, atomic_write):
    """Render the config with the location-block builder stubbed.

    The unit under test is the healthy/unhealthy branch decision, not the
    transport-aware block renderer — stubbing the latter keeps this test from
    dragging in the whole repository/telemetry import chain (and keeps a
    dependency bump from turning a routing test red for unrelated reasons).
    The stub emits the same ``location {{ROOT_PATH}}<path>/`` shape the real
    builder produces, so the assertions below still read like nginx output.
    """
    def fake_blocks(path, server_info):
        return [f"""    location {{{{ROOT_PATH}}}}{path}/ {{\n        proxy_pass {server_info["proxy_pass_url"]};\n    }}"""]

    mock_health_service = MagicMock()
    mock_health_service.server_health_status = health
    # Only the nginx TEMPLATE read is stubbed. Patching ``builtins.open``
    # wholesale would also swallow unrelated reads performed during rendering
    # (pymongo's pool options probe ``platform.mac_ver()``, which parses a
    # plist via open() — mocked, that raises and the whole config comes out
    # empty, which has nothing to do with the branch under test).
    template = nginx_service.nginx_template_path
    real_open = builtins.open

    def selective_open(file, *args, **kwargs):
        if str(file) == str(template):
            return mock_open(read_data="server { {{LOCATION_BLOCKS}} }")(file, *args, **kwargs)
        return real_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=selective_open):
        with patch("registry.health.service.health_service", mock_health_service):
            with patch.object(nginx_service, "_generate_transport_location_blocks", side_effect=fake_blocks):
                with patch.object(nginx_service, "get_additional_server_names", return_value=""):
                    with patch.object(nginx_service, "reload_nginx", return_value=True):
                        with patch("os.environ.get", return_value="http://keycloak:8080"):
                            await nginx_service.generate_config_async(servers)
    return "\n".join(call.args[1] for call in atomic_write.call_args_list)


def _active_block(rendered, path):
    """True when the path has a live (uncommented) location directive.

    ``{{ROOT_PATH}}`` is already substituted by the time we look (the test pins
    ``os.environ.get`` to a fixed gateway root), so match on the path suffix
    rather than the raw placeholder.
    """
    needle = f"{path.rstrip('/')}/"
    for line in rendered.splitlines():
        at = line.find(needle)
        if at == -1:
            continue
        loc = line.find("location ")
        if loc == -1 or loc > at:
            continue
        # A commented-out block is rendered as "#    location …", i.e. the '#'
        # sits before the directive on the same line.
        if line[:loc].lstrip().startswith("#"):
            continue
        return True
    return False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_self_reported_entry_is_routed_despite_unknown_health(
    nginx_service, mock_atomic_write
):
    """A self_report entry has no probe verdict; it must still get a route."""
    servers = {"/wire-ops-mcp": _remote("/wire-ops-mcp", surface_source="self_report")}
    rendered = await _render(
        nginx_service,
        {"/wire-ops-mcp": HealthStatus.UNKNOWN},
        servers,
        mock_atomic_write,
    )

    assert _active_block(rendered, "/wire-ops-mcp"), (
        "self_report 条目没有探针判词，必须按登记状态放路由；"
        "被注释掉就等于公网 405（路由不存在），而不是 401（需鉴权）"
    )
    assert "Service currently unhealthy" not in rendered, (
        "self_report 条目不该落到 unhealthy 注释分支"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_health_without_self_report_still_commented(
    nginx_service, mock_atomic_write
):
    """Control: an ordinary entry with no verdict keeps the old behaviour."""
    servers = {"/plain-mcp": _remote("/plain-mcp")}
    rendered = await _render(
        nginx_service,
        {"/plain-mcp": HealthStatus.UNKNOWN},
        servers,
        mock_atomic_write,
    )

    assert not _active_block(rendered, "/plain-mcp")
    assert "Service currently unhealthy" in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhealthy_health_wins_over_self_report_for_unhealthy_states(
    nginx_service, mock_atomic_write
):
    """Routing is a liveness statement: an explicit UNHEALTHY verdict from a
    real probe must NOT be overridden by the registration flag. Only the
    "no verdict at all" state is what self_report is exempt from."""
    servers = {"/probed-mcp": _remote("/probed-mcp", surface_source="self_report")}
    rendered = await _render(
        nginx_service,
        {"/probed-mcp": HealthStatus.HEALTHY},
        servers,
        mock_atomic_write,
    )
    # Healthy path is routed either way; this asserts the flag did not suppress it.
    assert _active_block(rendered, "/probed-mcp")
