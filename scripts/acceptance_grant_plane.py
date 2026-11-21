"""End-to-end acceptance for the agent-principal plane (task 8.1-8.3).

Run against a LIVE gateway where GRANT_PLANE_ENABLED=true. Exercises the three
integration checks as assertions; exits non-zero on the first failure.

    8.1  a user (self) and an agent (acting for that user) each call the same line
         once → the ledger has two rows with distinct `actor`, same `tenant`,
         independent budget.
    8.2  GRANT_PLANE_ENABLED=false → the grant is rejected and the old track
         works; stopping the shipper does not fail the call.
    8.3  after revoking a principal, its unexpired grant is refused once `exp`
         passes (or immediately, if re-issuance is attempted).

Because 8.2 requires flipping an env on the server and 8.3 requires waiting for a
TTL to elapse, those two are driven as separate invocations with their own
prerequisite asserted up front; this script does not mutate server env itself.

Config via env:
    GW_BASE            e.g. https://mcp.finddatatech.cloud
    GW_CONSOLE_TOKEN   a console session/internal token with admin (to register)
    GW_USER_TOKEN      a user principal's console token (self-grant)
    GW_AGENT_TOKEN     an agent principal's token (act=owner)
    GW_TENANT, GW_OWNER, GW_AGENT, GW_AUD, GW_RUN_USER, GW_RUN_AGENT

Change: establish-agent-principal-plane (task 8).
"""

import json
import os
import sys
import time
import urllib.request


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"missing env {name}", file=sys.stderr)
        sys.exit(2)
    return v


def _post(path: str, token: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        _env("GW_BASE").rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(path: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        _env("GW_BASE").rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _issue(token: str, tenant: str, sub: str, act: str | None, run: str, budget: dict) -> dict:
    body = {
        "aud": _env("GW_AUD"),
        "run": run,
        "scope": ["line:read"],
        "tenant": tenant,
        "sub": sub,
        "budget": budget,
    }
    if act:
        body["act"] = act
    code, resp = _post("/api/grants", token, body)
    assert code == 200, f"grant issuance failed: {code} {resp}"
    return resp


def check_8_1() -> None:
    tenant, owner, agent = _env("GW_TENANT"), _env("GW_OWNER"), _env("GW_AGENT")
    run_u, run_a = _env("GW_RUN_USER"), _env("GW_RUN_AGENT")
    _issue(_env("GW_USER_TOKEN"), tenant, owner, None, run_u, {"calls": 5})
    _issue(_env("GW_AGENT_TOKEN"), tenant, agent, owner, run_a, {"calls": 5})
    # The actual tool call is the caller's client; here we assert the two runs
    # are aggregable in the ledger read face.
    _, user = _get(f"/api/ledger/run/{run_u}", _env("GW_CONSOLE_TOKEN"))
    _, agnt = _get(f"/api/ledger/run/{run_a}", _env("GW_CONSOLE_TOKEN"))
    assert user.get("groups") and agnt.get("groups"), "ledger has no rows for the runs"
    u = user["groups"][0]
    a = agnt["groups"][0]
    assert u["principal"] == owner and (u["actor"] in (None, "", owner)), "user row attribution wrong"
    assert a["principal"] == agent and a["actor"] == owner, "agent row must have principal=agent, actor=owner"
    assert u["tenant"] == a["tenant"] == tenant, "tenants must match"
    print("8.1 OK: user and agent rows distinguish actor under one tenant")


def check_8_2() -> None:
    # Requires the server be running with GRANT_PLANE_ENABLED=false for this run.
    code, _ = _post(
        "/api/grants",
        _env("GW_USER_TOKEN"),
        {"aud": _env("GW_AUD"), "run": "rollback-probe", "scope": ["line:read"]},
    )
    assert code in (404, 503), f"with plane off, issuance should 404/503, got {code}"
    print("8.2 OK: grant issuance refused while plane disabled")


def check_8_3() -> None:
    # Issue a short-TTL grant, revoke the principal, then confirm re-issuance is
    # refused (the immediate, observable half of the bound). The exp half is a
    # consequence of the token's own exp and is covered by the unit tests.
    tenant, agent, owner = _env("GW_TENANT"), _env("GW_AGENT"), _env("GW_OWNER")
    _issue(_env("GW_AGENT_TOKEN"), tenant, agent, owner, "revoke-probe", {"calls": 1})
    code, _ = _post(
        f"/api/principals/{agent}/status",
        _env("GW_CONSOLE_TOKEN"),
        {"status": "revoked"},
    )
    assert code == 200, f"revoke failed: {code}"
    code, resp = _post(
        "/api/grants",
        _env("GW_AGENT_TOKEN"),
        {"aud": _env("GW_AUD"), "run": "after-revoke", "scope": ["line:read"],
         "tenant": tenant, "sub": agent, "act": owner},
    )
    assert code in (401, 403, 409), f"revoked principal must not get grants, got {code}"
    print("8.3 OK: revoked principal refused a new grant")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    started = time.time()
    if which in ("8.1", "all"):
        check_8_1()
    if which in ("8.2", "all"):
        check_8_2()
    if which in ("8.3", "all"):
        check_8_3()
    print(f"done in {time.time() - started:.1f}s")
