"""The backfill's derivation (change: fix-a2a-agent-backend-self-loop, tasks 2.1).

The spec pins that the dry-run and the write share ONE derivation. These tests
call that function directly — the CLI wiring is thin and asserted separately by
the dry-run/apply contract in the script's own docstring.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from registry.core.a2a_backend import gateway_hosts

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backfill-a2a-agent-backends.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_a2a", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def backfill():
    return _load()


GW = gateway_hosts("https://mcp.finddatatech.cloud")


def test_a_known_path_derives_its_runner_endpoint(backfill):
    agent = SimpleNamespace(path="/packs/X/reception", url="https://mcp.finddatatech.cloud/agent/packs/X/reception/")
    out = backfill.derive_backend_for(
        agent, runner_endpoints={"/packs/X/reception": "http://100.64.0.14:8909"}, gateway_hosts=GW
    )
    assert out["derivable"] is True
    assert out["target"] == "http://100.64.0.14:8909"


def test_an_unknown_path_is_not_guessed(backfill):
    agent = SimpleNamespace(path="/packs/X/ghost", url="https://mcp.finddatatech.cloud/agent/packs/X/ghost/")
    out = backfill.derive_backend_for(agent, runner_endpoints={}, gateway_hosts=GW)
    assert out["derivable"] is False
    assert out["target"] is None
    assert "no runner endpoint" in out["reason"]


def test_a_derived_self_loop_is_refused(backfill):
    # If the mapping itself points at the gateway, writing it would look like a
    # fix while changing nothing.
    agent = SimpleNamespace(path="/p", url="https://mcp.finddatatech.cloud/agent/p/")
    out = backfill.derive_backend_for(
        agent, runner_endpoints={"/p": "https://mcp.finddatatech.cloud/agent/p/"}, gateway_hosts=GW
    )
    assert out["derivable"] is False
    assert "itself" in out["reason"]


def test_matching_is_by_path_identity_not_by_name_guessing(backfill):
    # Two agents with similar names must not be cross-assigned.
    a = SimpleNamespace(path="/packs/X/reception", url="https://gw/agent/packs/X/reception/")
    out = backfill.derive_backend_for(
        a,
        runner_endpoints={
            "/packs/Y/reception": "http://10.0.0.9:9999",  # same tail, different path
            "/packs/X/reception": "http://10.0.0.5:8899",
        },
        gateway_hosts=GW,
    )
    assert out["target"] == "http://10.0.0.5:8899"
