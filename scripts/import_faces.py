#!/usr/bin/env python3
"""Import declarative tool faces (``faces/*.yaml``) into the registry.

A tool face is a declarative aggregation of MCP tools served at
``/virtual/<face>``; see ``registry/schemas/face_manifest.py`` and the
``establish-tool-face-plane`` change. This script is the operator entry point:
it validates every face manifest and imports it into the registry's virtual
servers, idempotently.

WHY DRY-RUN IS THE DEFAULT
    Importing writes live virtual-server records and triggers nginx reload. A
    face with a wrong alias or backend changes what a paying customer can call.
    So this prints the plan for every face first; nothing is written without
    ``--apply``.

USAGE
    # Plan only (default): show what each face would do and stop.
    uv run python scripts/import_faces.py

    # Import one face for real.
    uv run python scripts/import_faces.py --apply --face wire-customer

    # Import every face in faces/ for real.
    uv run python scripts/import_faces.py --apply

    # Point at a different manifests directory.
    uv run python scripts/import_faces.py --dir /path/to/faces
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make the repo root importable so `registry` (a namespace package) resolves
# whether this runs from the repo root or elsewhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from registry.schemas.face_manifest import FaceManifest, load_face_manifest  # noqa: E402
from registry.services.face_manifest_service import (  # noqa: E402
    FaceImportPlan,
    apply_face_import,
    reconcile_faces,
)

_DEFAULT_FACES_DIR = _REPO_ROOT / "faces"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import tool-face manifests into registry virtual servers "
        "(dry-run by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python scripts/import_faces.py\n"
            "  uv run python scripts/import_faces.py --apply --face wire-customer\n"
            "  uv run python scripts/import_faces.py --apply"
        ),
    )
    parser.add_argument(
        "--dir",
        default=str(_DEFAULT_FACES_DIR),
        help="Directory of face manifests (default: <repo>/faces).",
    )
    parser.add_argument(
        "--face",
        default=None,
        help="Import only this face id (default: every *.yaml in --dir).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script only prints the plan.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Read-only drift report; exit 1 when any face drifts. Overrides --apply.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def _load_manifests(faces_dir: Path, only: str | None) -> list[FaceManifest]:
    """Load and validate every manifest in ``faces_dir`` (optionally one face)."""
    if not faces_dir.is_dir():
        raise SystemExit(f"faces dir not found: {faces_dir}")
    paths = sorted(faces_dir.glob("*.yaml"))
    if only is not None:
        paths = [p for p in paths if p.stem == only]
        if not paths:
            raise SystemExit(f"no manifest for face {only!r} in {faces_dir}")
    return [load_face_manifest(p) for p in paths]


async def _run(manifests: list[FaceManifest], apply: bool) -> int:
    """Plan (and optionally apply) each face; return a process exit code."""
    exit_code = 0
    for manifest in manifests:
        try:
            if apply:
                plan: FaceImportPlan = await apply_face_import(manifest, dry_run=False)
            else:
                # Read current state to plan accurately, but never write.
                plan = await apply_face_import(manifest, dry_run=True)
        except Exception as exc:  # surface per-face failure, keep going
            print(f"[FAIL] {manifest.face}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        verb = "WOULD" if not apply else ""
        detail = ", ".join(sorted(plan.changes)) if plan.changes else "-"
        print(f"[{plan.action.upper():6}] {manifest.face:24} {verb} {detail}".rstrip())
    return exit_code


async def _reconcile(manifests: list[FaceManifest]) -> int:
    """Report drift for each face; return non-zero when any face drifted."""
    drifted = False
    for r in await reconcile_faces(manifests):
        tag = "DRIFT" if r.drift else "OK"
        print(f"[{tag:5}] {r.face:24} {r.reason}")
        drifted = drifted or r.drift
    return 1 if drifted else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    import logging

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
    )
    manifests = _load_manifests(Path(args.dir), args.face)
    if args.reconcile:
        return asyncio.run(_reconcile(manifests))
    if not args.apply:
        print(f"(dry-run; {len(manifests)} face(s) planned. Re-run with --apply to write.)")
    return asyncio.run(_run(manifests, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
