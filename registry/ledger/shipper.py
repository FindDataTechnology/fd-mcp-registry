"""Standalone ledger shipper: JSONL -> PostgreSQL, batched, idempotent.

Runs OUT of the gateway process (a sidecar, a CronJob, or a host unit). Keeping
it a separate program is the point: the gateway image needs no Postgres driver,
and a PG outage only backs the file up — it never touches a live call.

Reads LEDGER_SINK_PATH (the same JSONL the gateway appends) line by line, inserts
each into ``ledger_events`` with ON CONFLICT DO NOTHING on ``(grant_jti,
span_id)``, and records how far it got in a cursor file so a restart resumes
rather than re-reads. Re-reading would be harmless (idempotent); the cursor just
saves work.

Outcome → real ledger row only for outcome in (ok, refused, incomplete); an
``error`` row is still kept (it happened and may be billable-adjacent).

Usage:
    python -m registry.ledger.shipper --dsn postgresql://... \
        [--sink logs/audit/ledger_events.jsonl] [--cursor .ledger_cursor] [--once]

The module also exposes ``ship_once(dsn, sink, cursor) -> int`` for tests.

Change: establish-agent-principal-plane (task group 3).
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ledger_shipper")

_INSERT = """
INSERT INTO ledger_events
  (grant_jti, span_id, run_id, principal_id, actor_id, tenant_id, audience,
   tool, outcome, rows_returned, tokens, latency_ms, source, ts)
VALUES
  (%(grant_jti)s, %(span_id)s, %(run)s, %(principal)s, %(actor)s, %(tenant)s,
   %(audience)s, %(tool)s, %(outcome)s, %(rows)s, %(tokens)s, %(latency_ms)s,
   %(source)s, %(ts)s)
ON CONFLICT (grant_jti, span_id) DO NOTHING
"""

_REQUIRED = ("grant_jti", "span_id", "run", "principal", "tenant", "audience", "tool", "outcome", "ts")


def _coerce_row(event: dict) -> dict:
    missing = [k for k in _REQUIRED if not event.get(k)]
    if missing:
        raise ValueError(f"ledger event missing required fields: {missing}")
    ts = event["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return {
        "grant_jti": event["grant_jti"],
        "span_id": event["span_id"],
        "run": event["run"],
        "principal": event["principal"],
        "actor": event.get("actor"),
        "tenant": event["tenant"],
        "audience": event["audience"],
        "tool": event["tool"],
        "outcome": event["outcome"],
        "rows": event.get("rows"),
        "tokens": event.get("tokens"),
        "latency_ms": event.get("latency_ms"),
        "source": event.get("source", "gateway"),
        "ts": ts,
    }


def _read_cursor(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_cursor(path: Path, offset: int) -> None:
    path.write_text(str(offset))


def backlog(sink: str, cursor: str) -> int:
    """Bytes between the cursor and the sink end — a readable 'lagging' gauge.

    A scheduler/health check reads this to decide whether the ledger is falling
    behind. Never raises; a missing cursor means 'everything not yet shipped'.
    """
    sink_path = Path(sink)
    if not sink_path.exists():
        return 0
    cursor_path = Path(cursor)
    try:
        offset = _read_cursor(cursor_path)
    except Exception:  # noqa: BLE001
        offset = 0
    return max(0, sink_path.stat().st_size - offset)


def ship_once(dsn: str, sink: str, cursor: str, *, batch: int = 1000) -> int:
    """Ship new lines from ``sink`` to PG. Returns rows attempted.

    Requires psycopg (installed in the shipper's own environment, not the
    gateway's). Incremental via a byte-offset cursor file.
    """
    import psycopg  # local import: only the shipper needs the driver

    sink_path = Path(sink)
    if not sink_path.exists():
        return 0
    cursor_path = Path(cursor)
    offset = _read_cursor(cursor_path)

    rows: list[dict] = []
    new_offset = offset
    with sink_path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            new_offset = fh.tell()
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                rows.append(_coerce_row(json.loads(line)))
            except (ValueError, json.JSONDecodeError) as exc:
                logger.warning("skip malformed ledger line: %s", exc)
            if len(rows) >= batch:
                break

    if not rows:
        _write_cursor(cursor_path, new_offset)
        return 0

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.executemany(_INSERT, rows)
        conn.commit()
    _write_cursor(cursor_path, new_offset)
    logger.info("shipped %d ledger rows (offset -> %d)", len(rows), new_offset)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ship gateway ledger JSONL into PostgreSQL.")
    p.add_argument("--dsn", default=os.getenv("LEDGER_PG_DSN", ""))
    p.add_argument("--sink", default=os.getenv("LEDGER_SINK_PATH", "logs/audit/ledger_events.jsonl"))
    p.add_argument("--cursor", default=os.getenv("LEDGER_CURSOR_PATH", ".ledger_cursor"))
    p.add_argument("--once", action="store_true", help="ship one batch and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if not args.dsn:
        logger.error("no DSN: pass --dsn or LEDGER_PG_DSN")
        return 2

    if args.once:
        n = ship_once(args.dsn, args.sink, args.cursor)
        return 0 if n >= 0 else 1

    # Continuous mode: poll. A production deployment uses a scheduler; this loop
    # is for a simple sidecar.
    import time

    while True:
        try:
            ship_once(args.dsn, args.sink, args.cursor)
        except Exception as exc:  # noqa: BLE001 - a poll loop must survive a blip
            logger.error("ship cycle failed: %s", exc)
        time.sleep(float(os.getenv("LEDGER_SHIP_INTERVAL", "10")))


if __name__ == "__main__":
    sys.exit(main())
