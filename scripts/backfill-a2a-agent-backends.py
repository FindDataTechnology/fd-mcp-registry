#!/usr/bin/env python3
"""Backfill ``proxy_pass_url`` for A2A agents whose backend points at the gateway.

WHY (change: fix-a2a-agent-backend-self-loop, 2026-11-21)
    Every A2A agent in this deployment carries ``proxy_pass_url = None`` while its
    advertised ``url`` is the gateway's own public entry
    (``https://mcp.finddatatech.cloud/agent/<path>/``). The nginx generator falls
    back to ``url`` when ``proxy_pass_url`` is empty, so the backend became the
    gateway: a forwarded request re-enters this gateway instead of reaching the
    executor. Measured end to end — the runner logged the A2A method ZERO times
    while the caller saw a plausible answer.

    This script writes the real backend address into ``proxy_pass_url`` so the
    fallback never applies again.

WHY DRY-RUN IS THE DEFAULT
    This is an unattended write to a live registry. Getting a backend wrong makes
    an agent WORSE (a self-loop at least always fails the same way; a wrong
    address fails differently per call). So: print every row first, and require
    ``--apply`` plus a backup file before writing anything.

WHY THE DRY-RUN AND THE WRITE SHARE ONE FUNCTION
    ``derive_backend_for`` is the only place an address is decided. A dry-run that
    uses a different ("approximate") derivation is the family of bug this repo
    already has on record: two paths look right, the one that actually runs was
    never checked.

WHAT IT WILL NOT DO
    Guess. A row whose real backend cannot be derived is left exactly as it is,
    with a named reason, and the run continues — an all-or-nothing failure would
    mean one underivable row blocks the fix for everyone else.

Usage (inside the registry pod — the environment already has the DB and settings):

    # 1) See the full plan. Writes nothing.
    python scripts/backfill-a2a-agent-backends.py

    # 2) Apply, saving the old values first (needed for --revert).
    python scripts/backfill-a2a-agent-backends.py --apply --backup /tmp/a2a-backfill.json

    # 3) Undo, if a value turns out wrong.
    python scripts/backfill-a2a-agent-backends.py --revert /tmp/a2a-backfill.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
for _path in (_REPO_ROOT, _SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)


def derive_backend_for(
    agent: Any,
    *,
    runner_endpoints: dict[str, str],
    gateway_hosts: frozenset[str],
) -> dict[str, Any]:
    """The ONE derivation of an agent's real backend address.

    Used by ``--apply``, by the dry-run, and (via ``resolve_backend_address``)
    by every other consumer. Never invents a value: a row that cannot be mapped
    to a known runner endpoint comes back ``derivable: False`` with a reason.

    Args:
        agent: The agent card (only ``path`` is read — the address comes from
            the deployment side, not from what the row advertises).
        runner_endpoints: ``agent path -> backend base URL``, built from the
            runner's own health face. The KEY is the registry path so the two
            sides are matched by identity, never by guessing at names.
        gateway_hosts: To refuse writing a value that would recreate the loop.
    """
    from registry.core.a2a_backend import resolve_backend_address

    path = getattr(agent, "path", None) or ""
    candidate = runner_endpoints.get(path)
    if not candidate:
        return {
            "derivable": False,
            "reason": (
                "no runner endpoint for this path — the agent is not in any "
                "runner's health face (undeployed, renamed, or a different host)"
            ),
            "target": None,
        }
    # Never write a value that is itself a self-loop; that would look fixed
    # while changing nothing.
    verdict = resolve_backend_address(
        proxy_pass_url=candidate, url=None, gateway_hosts=gateway_hosts
    )
    if not verdict.ok:
        return {
            "derivable": False,
            "reason": f"derived address is itself {verdict.status}: {verdict.reason}",
            "target": candidate,
        }
    return {"derivable": True, "reason": None, "target": verdict.url}


async def _load_agents() -> list[Any]:
    from registry.services.agent_service import agent_service

    return await agent_service.get_all_agents()


async def _write_backend(path: str, value: str) -> None:
    from registry.services.agent_service import agent_service

    await agent_service.update_agent(path, {"proxy_pass_url": value})


def _parse_runner_endpoints(raw: str) -> dict[str, str]:
    """Parse the ``--runner-endpoints`` JSON file: ``{agentPath: baseUrl}``.

    Deliberately a FILE, not something this script derives from a cluster API
    call: the mapping between a registry path and a runner port lives in the
    deployment (``manager.health().agents[].port``), and a script that silently
    queried a different source than the operator expects is how "the fix ran
    but nothing changed" happens. The operator produces the file; the script
    just applies it.
    """
    data = json.loads(Path(raw).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"--runner-endpoints must be a JSON object, got {type(data).__name__}")
    return {str(k): str(v) for k, v in data.items()}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--runner-endpoints",
        required=False,
        help="JSON file {agentPath: baseUrl} built from the runner's health face",
    )
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--backup", default=None, help="path to save old values before applying")
    parser.add_argument("--revert", default=None, help="restore from a backup file")
    args = parser.parse_args(argv)

    from registry.core.a2a_backend import gateway_hosts as gateway_hosts_fn, resolve_backend_address
    from registry.core.config import settings

    hosts = gateway_hosts_fn(
        getattr(settings, "registry_url", None),
        [h for h in (getattr(settings, "gateway_alias_hosts", None) or []) if h],
    )

    if args.revert:
        backup = json.loads(Path(args.revert).read_text(encoding="utf-8"))
        logger.info("reverting %d agent(s) from %s", len(backup), args.revert)
        for path, original in backup.items():
            await _write_backend(path, original)
            logger.info("  restored %s -> %r", path, original)
        return 0

    if not args.runner_endpoints:
        raise SystemExit("--runner-endpoints is required unless --revert is given")

    endpoints = _parse_runner_endpoints(args.runner_endpoints)
    agents = await _load_agents()
    a2a_agents = [a for a in agents if (getattr(a, "supported_protocol", "") or "").lower() == "a2a"]
    logger.info("found %d A2A agent(s) of %d total", len(a2a_agents), len(agents))

    planned: list[tuple[str, str, str]] = []  # (path, old, new)
    skipped: list[tuple[str, str]] = []  # (path, reason)
    for agent in a2a_agents:
        path = getattr(agent, "path", "") or ""
        current = getattr(agent, "proxy_pass_url", None)
        verdict = resolve_backend_address(
            proxy_pass_url=current,
            url=getattr(agent, "url", None),
            gateway_hosts=hosts,
        )
        if verdict.ok:
            skipped.append((path, "already ok"))
            continue
        derived = derive_backend_for(agent, runner_endpoints=endpoints, gateway_hosts=hosts)
        if not derived["derivable"]:
            skipped.append((path, derived["reason"]))
            continue
        planned.append((path, current or "", derived["target"]))

    print()
    print(f"PLAN — {len(planned)} to change, {len(skipped)} skipped")
    print("-" * 78)
    for path, old, new in planned:
        print(f"  {path}\n      from {old or '<none>'}\n      to   {new}")
    for path, reason in skipped:
        print(f"  SKIP {path}: {reason}")
    print("-" * 78)

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    if not args.backup:
        raise SystemExit("--apply requires --backup <file> so the change can be reverted")
    backup = {path: old for path, old, _ in planned}
    Path(args.backup).write_text(json.dumps(backup, indent=2), encoding="utf-8")
    logger.info("wrote backup of %d old value(s) to %s", len(backup), args.backup)

    written = 0
    for path, _old, new in planned:
        try:
            await _write_backend(path, new)
            written += 1
            logger.info("  wrote %s -> %s", path, new)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            logger.error("  FAILED %s: %s", path, exc)
    print(f"\nAPPLIED — {written}/{len(planned)} written. Revert: --revert {args.backup}")
    return 0 if written == len(planned) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
