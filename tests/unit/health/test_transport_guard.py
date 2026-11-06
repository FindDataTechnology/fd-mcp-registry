"""Regression pins for the supported_transports read-site guard.

(fix-registry-probe-and-selfreport task 1.4; A0 2026-11-07)

The incidents this pins shut down: a key-present-null
``supported_transports`` reached ``in`` membership checks as ``None``,
raised ``TypeError``, flipped the health verdict, had nginx comment the
location out, and surfaced as a gateway 405 (lex-ops 2026-11-04;
wire-ops + wanxing-ops 2026-11-07). Four shapes here — null, missing,
bare string, normal array — must each produce a deliberate, readable
verdict from the real probe entry point, never an exception.
"""

from unittest.mock import patch

import pytest

from registry.constants import HealthStatus
from registry.core.transport_guard import normalize_transports
from registry.health.service import HealthMonitoringService

PUBLIC_URL = "https://probe-guard.example.com/mcp"  # passes the SSRF guard


def _service() -> HealthMonitoringService:
    return HealthMonitoringService()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transports",
    [None, "missing", "streamable-http", ["streamable-http"]],
)
async def test_probe_entry_point_never_raises_on_declaration_shapes(transports):
    """The four declaration shapes each yield a verdict, never an exception."""
    svc = _service()
    info = {"proxy_pass_url": PUBLIC_URL, "path": "/probe-guard"}
    if transports != "missing":
        info["supported_transports"] = transports
    # No exception may escape for ANY shape — that is the regression itself.
    with patch("registry.health.service.validate_url", return_value=["93.184.216.34"]):
        is_healthy, detail = await svc._check_server_endpoint_transport_aware(
            None, PUBLIC_URL, info
        )
    assert isinstance(is_healthy, bool)
    assert isinstance(detail, str) and detail


@pytest.mark.asyncio
async def test_null_declaration_degrades_honestly_not_as_streamable():
    """Key-present-null must NOT be silently treated as streamable-http."""
    svc = _service()
    info = {
        "proxy_pass_url": PUBLIC_URL,
        "path": "/probe-guard",
        "supported_transports": None,
    }
    with patch("registry.health.service.validate_url", return_value=["93.184.216.34"]):
        is_healthy, detail = await svc._check_server_endpoint_transport_aware(
            None, PUBLIC_URL, info
        )
    assert is_healthy is False
    assert detail == HealthStatus.UNHEALTHY_NO_TRANSPORT_DECLARATION.value


@pytest.mark.asyncio
async def test_missing_declaration_degrades_honestly():
    """Key-absent is the same 'no declaration' verdict as null."""
    svc = _service()
    info = {"proxy_pass_url": PUBLIC_URL, "path": "/probe-guard"}
    with patch("registry.health.service.validate_url", return_value=["93.184.216.34"]):
        is_healthy, detail = await svc._check_server_endpoint_transport_aware(
            None, PUBLIC_URL, info
        )
    assert is_healthy is False
    assert detail == HealthStatus.UNHEALTHY_NO_TRANSPORT_DECLARATION.value


# ---------------------------------------------------------------------------
# normalize_transports unit pins (the guard function itself)
# ---------------------------------------------------------------------------


def test_normalize_missing_key_is_none():
    assert normalize_transports({}) is None


def test_normalize_none_value_is_none():
    assert normalize_transports({"supported_transports": None}) is None


def test_normalize_bare_string_is_single_element_list():
    assert normalize_transports(
        {"supported_transports": "streamable-http"}
    ) == ["streamable-http"]


def test_normalize_empty_string_is_none():
    assert normalize_transports({"supported_transports": ""}) is None


def test_normalize_empty_list_is_none():
    assert normalize_transports({"supported_transports": []}) is None


def test_normalize_normal_array_passthrough():
    assert normalize_transports(
        {"supported_transports": ["streamable-http", "sse"]}
    ) == ["streamable-http", "sse"]


def test_normalize_non_list_type_is_none():
    assert normalize_transports({"supported_transports": 42}) is None


def test_normalize_never_mutates_input():
    info = {"supported_transports": None}
    normalize_transports(info)
    assert info == {"supported_transports": None}


def test_normalize_filters_non_string_items():
    assert normalize_transports(
        {"supported_transports": ["streamable-http", 7, None]}
    ) == ["streamable-http"]
