"""Unit tests for A2A backend addressing (change: fix-a2a-agent-backend-self-loop).

The rule these pin is the one that failed in production: a backend that names the
gateway is not a backend. Three classes, a hostname comparison, and no reliance on
health status (which is built from the same fallback and is therefore circular).
"""

from __future__ import annotations

import pytest

from registry.core.a2a_backend import (
    BACKEND_MISSING,
    BACKEND_OK,
    BACKEND_SELF_LOOP,
    gateway_hosts,
    resolve_backend_address,
)

GW = gateway_hosts("https://mcp.finddatatech.cloud")


def judge(proxy_pass_url=None, url=None, hosts=GW):
    return resolve_backend_address(proxy_pass_url=proxy_pass_url, url=url, gateway_hosts=hosts)


def test_proxy_pass_url_wins_when_present():
    v = judge(proxy_pass_url="http://100.64.0.14:8909", url="https://mcp.finddatatech.cloud/agent/x/")
    assert v.status == BACKEND_OK
    assert v.url == "http://100.64.0.14:8909"


def test_the_production_shape_is_self_loop():
    # Exactly what every agent in this deployment had: no proxy_pass_url, and the
    # advertised url is the gateway's own entry.
    v = judge(proxy_pass_url=None, url="https://mcp.finddatatech.cloud/agent/packs/X/reception/")
    assert v.status == BACKEND_SELF_LOOP
    assert "gateway itself" in v.reason


def test_fallback_still_works_for_a_real_backend():
    # Pre-reverse-proxy agents: url IS their backend. The fallback must not be
    # removed by the self-loop fix.
    v = judge(proxy_pass_url=None, url="http://10.0.0.5:8899")
    assert v.status == BACKEND_OK
    assert v.url == "http://10.0.0.5:8899"


def test_nothing_set_is_missing_not_self_loop():
    v = judge(proxy_pass_url=None, url=None)
    assert v.status == BACKEND_MISSING
    assert v.url is None


def test_prefix_difference_does_not_hide_a_self_loop():
    # `/agent/x` and `/agent/y` share no prefix; both are the gateway.
    for u in ("https://mcp.finddatatech.cloud/agent/x/", "https://mcp.finddatatech.cloud/other/y"):
        assert judge(url=u).status == BACKEND_SELF_LOOP, u


def test_same_host_different_port_is_a_different_endpoint():
    hosts = gateway_hosts("http://localhost:8000")
    # The runner publishes 8891..8922 on one host; every one of them is an agent,
    # not the gateway.
    assert judge(url="http://localhost:9000/test-agent", hosts=hosts).status == BACKEND_OK
    assert judge(url="http://localhost:8891/agent/x", hosts=hosts).status == BACKEND_OK
    # The gateway's own port is the gateway.
    assert judge(url="http://localhost:8000/agent/x", hosts=hosts).status == BACKEND_SELF_LOOP


def test_default_port_is_normalized(): 
    # `https://gw` and `https://gw:443` are the same endpoint.
    hosts = gateway_hosts("https://gw.example.com")
    assert judge(url="https://gw.example.com:443/agent/x", hosts=hosts).status == BACKEND_SELF_LOOP
    hosts80 = gateway_hosts("http://gw.example.com")
    assert judge(url="http://gw.example.com:80/agent/x", hosts=hosts80).status == BACKEND_SELF_LOOP


def test_an_alias_is_also_the_gateway():
    hosts = gateway_hosts("https://mcp.finddatatech.cloud", ["https://registry.internal.example"])
    assert judge(url="https://registry.internal.example/agent/x", hosts=hosts).status == BACKEND_SELF_LOOP
    # A non-gateway host stays ok even with aliases declared.
    assert judge(url="http://10.0.0.5:8899", hosts=hosts).status == BACKEND_OK


def test_case_and_scheme_are_not_identity_but_a_nondefault_port_is():
    hosts = gateway_hosts("https://MCP.FindDataTech.Cloud")
    # Same host, no port on the gateway side: the agent's explicit non-default
    # port makes it a DIFFERENT endpoint. This is the shape that broke the first
    # implementation: comparing hostname alone (which drops the port) flagged
    # `localhost:9000` as the gateway at `localhost:8000`, and two agents on one
    # machine would all have been "self-loops".
    assert judge(url="http://mcp.finddatatech.cloud:18080/agent/x", hosts=hosts).status == BACKEND_OK
    # Same host and no port: that IS the gateway, whatever the scheme.
    assert judge(url="http://mcp.finddatatech.cloud/agent/x", hosts=hosts).status == BACKEND_SELF_LOOP
    assert judge(url="https://mcp.finddatatech.cloud/agent/x", hosts=hosts).status == BACKEND_SELF_LOOP


def test_trailing_slash_is_normalized():
    v = judge(proxy_pass_url="http://10.0.0.5:8899/")
    assert v.url == "http://10.0.0.5:8899"


def test_unparseable_value_is_not_silently_ok():
    # A non-string or blank value must land in `missing`, never in `ok`.
    assert judge(proxy_pass_url=123, url=None).status == BACKEND_MISSING
    assert judge(proxy_pass_url="   ", url="").status == BACKEND_MISSING


def test_gateway_hosts_empty_when_registry_url_unset():
    # No declared identity means nothing can be judged a self-loop. That is the
    # honest answer: the system cannot know its own name.
    hosts = gateway_hosts(None)
    assert hosts == frozenset()
    assert judge(url="https://anything.example/agent/x", hosts=hosts).status == BACKEND_OK


# ── 读面与生成期共用同一条规则 ──────────────────────────────────────────────
#
# The spec requires the READ face, the nginx generator and the health probe to
# agree. They cannot be made to agree by convention — they must call one
# function. These pin that the production shape lands in the same class at all
# three sites.

def test_production_shape_lands_in_self_loop_everywhere():
    """No proxy_pass_url + gateway url ⇒ self_loop, whoever asks."""
    hosts = gateway_hosts("https://mcp.finddatatech.cloud")
    v = resolve_backend_address(
        proxy_pass_url=None,
        url="https://mcp.finddatatech.cloud/agent/packs/Ut_bJMuW5KxAD83KImPmlg/reception/",
        gateway_hosts=hosts,
    )
    assert v.status == BACKEND_SELF_LOOP
    # The generator skips on `not verdict.ok`; the health probe returns
    # unhealthy on the same test; the read face reports `self_loop`. One verdict,
    # three consumers — this asserts the verdict, which is what they share.
    assert not v.ok


def test_three_classes_are_distinguishable_in_one_pass():
    hosts = gateway_hosts("https://gw.example.com")
    verdicts = [
        resolve_backend_address(proxy_pass_url=None, url=None, gateway_hosts=hosts),
        resolve_backend_address(proxy_pass_url=None, url="https://gw.example.com/agent/x", gateway_hosts=hosts),
        resolve_backend_address(proxy_pass_url="http://10.0.0.5:8899", url=None, gateway_hosts=hosts),
    ]
    assert [v.status for v in verdicts] == [BACKEND_MISSING, BACKEND_SELF_LOOP, BACKEND_OK]
    # Every non-ok class carries a reason; ok carries none.
    assert all(v.reason for v in verdicts if not v.ok)
    assert verdicts[-1].reason is None


# ── 写侧拒绝（tasks 3.1–3.2）─────────────────────────────────────────────────

from registry.core.a2a_backend import SelfLoopBackendError, assert_writable_backend  # noqa: E402


def test_registering_a_self_loop_backend_is_refused_by_name():
    hosts = gateway_hosts("https://mcp.finddatatech.cloud")
    with pytest.raises(SelfLoopBackendError) as exc:
        assert_writable_backend(
            path="/packs/X/reception",
            proxy_pass_url=None,
            url="https://mcp.finddatatech.cloud/agent/packs/X/reception/",
            gateway_hosts=hosts,
        )
    # The refusal names the agent and the reason, not just "invalid input".
    assert exc.value.path == "/packs/X/reception"
    assert "gateway itself" in str(exc.value)


def test_a_missing_backend_is_allowed_on_write():
    # Register the card first, point the backend later — the read face labels it
    # `missing`. Refusing this would block a legitimate onboarding order.
    hosts = gateway_hosts("https://gw.example.com")
    v = assert_writable_backend(path="/p", proxy_pass_url=None, url=None, gateway_hosts=hosts)
    assert v.status == BACKEND_MISSING


def test_a_real_backend_passes():
    hosts = gateway_hosts("https://gw.example.com")
    v = assert_writable_backend(
        path="/p", proxy_pass_url="http://10.0.0.5:8899", url=None, gateway_hosts=hosts
    )
    assert v.status == BACKEND_OK


def test_self_loop_error_is_a_value_error_so_existing_handlers_still_catch_it():
    # Backward compatibility: routes that only catch ValueError must not leak a
    # 500 just because the error grew a dedicated type.
    assert issubclass(SelfLoopBackendError, ValueError)


# ── 1.1 红证：读面契约（改动前无 backend 字段）────────────────────────────────

def test_agent_info_exposes_a_three_class_backend_block():
    """The read face must carry `backend`, and only for A2A agents.

    Before this change `AgentInfo` had no `backend` field at all — a caller could
    not tell a working agent from one whose backend is the gateway. This is the
    contract the read face now keeps.
    """
    from registry.schemas.agent_models import AgentInfo

    fields = AgentInfo.model_fields
    assert "backend" in fields
    assert fields["backend"].default is None
    assert "ok / missing / self_loop" in (fields["backend"].description or "")
