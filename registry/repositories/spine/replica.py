"""Local read-only replica of the spine projection (design D2).

The registry's registration reads serve from this replica; it is overwritten
ONLY by :meth:`SpineReplicaStore.replace_snapshot` from a successful projection
pull, so the single-writer property holds structurally (the puller is the one
writer; the repositories are readers).

Carrier: Python stdlib ``sqlite3`` (zero new dependencies, WAL mode). Rows are
stored as the projection's raw JSON payloads keyed by the spine primary key —
the mapping to legacy Mongo document shapes happens at read time in
:mod:`.mapping`, so a spine contract addition never strands stored rows.

Sync-state table records freshness honestly: ``as_of`` (spine clock at pull),
``pulled_at`` (local clock), ``stale`` flag, consecutive failure count and the
last error line. ``replace_snapshot`` flips stale off; ``mark_pull_failed``
flips it on while keeping the previous rows (last-good semantics).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spine_mcp_servers (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS spine_skills (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS spine_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    as_of INTEGER,
    pulled_at INTEGER,
    stale INTEGER NOT NULL DEFAULT 1,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    mcp_count INTEGER NOT NULL DEFAULT 0,
    skill_count INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO spine_sync_state (id, stale) VALUES (1, 1);
"""


class SpineReplicaStore:
    """SQLite-backed read-only replica of the spine projection payload."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # Single-writer store: one connection guarded by a lock. check_same_thread
        # is safe because every access serializes on _lock.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection (tests / shutdown)."""
        with self._lock:
            self._conn.close()

    def replace_snapshot(
        self,
        mcp_rows: list[dict[str, Any]],
        skill_rows: list[dict[str, Any]],
        as_of: int | None,
    ) -> None:
        """Atomically overwrite the replica with a fresh projection payload.

        Rows must be JSON-serializable — projection payloads always are (they
        come off ``response.json()``); a row that isn't raises ``TypeError``
        before any write lands and the previous snapshot survives untouched
        (last-good).

        Args:
            mcp_rows: Raw mcp_servers projection rows (spine field names).
            skill_rows: Raw skills projection rows (spine field names).
            as_of: Spine-side snapshot timestamp (epoch ms) from the payload.
        """
        now_ms = int(time.time() * 1000)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute("DELETE FROM spine_mcp_servers")
                self._conn.execute("DELETE FROM spine_skills")
                self._conn.executemany(
                    "INSERT INTO spine_mcp_servers (id, payload, updated_at) VALUES (?, ?, ?)",
                    [
                        (str(row.get("id")), json.dumps(row), _row_ts(row))
                        for row in mcp_rows
                    ],
                )
                self._conn.executemany(
                    "INSERT INTO spine_skills (id, payload, updated_at) VALUES (?, ?, ?)",
                    [
                        (str(row.get("name")), json.dumps(row), _row_ts(row))
                        for row in skill_rows
                    ],
                )
                self._conn.execute(
                    "UPDATE spine_sync_state SET as_of = ?, pulled_at = ?, stale = 0, "
                    "consecutive_failures = 0, last_error = NULL, mcp_count = ?, skill_count = ? "
                    "WHERE id = 1",
                    (as_of, now_ms, len(mcp_rows), len(skill_rows)),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def mark_pull_failed(self, error: str) -> None:
        """Record a failed pull; rows stay last-good and the replica is stale."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "UPDATE spine_sync_state SET stale = 1, "
                "consecutive_failures = consecutive_failures + 1, last_error = ?, "
                "pulled_at = pulled_at WHERE id = 1",
                (error[:500],),
            )
            self._conn.commit()
            logger.warning(
                "Spine replica pull failed (replica stays last-good, stale): %s", error
            )

    def status(self) -> dict[str, Any]:
        """Replica freshness/sync state for the health surface.

        Returns:
            Dict with stale, as_of, pulled_at, consecutive_failures, last_error,
            mcp_count, skill_count, and empty (no snapshot ever pulled).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT as_of, pulled_at, stale, consecutive_failures, last_error, "
                "mcp_count, skill_count FROM spine_sync_state WHERE id = 1"
            ).fetchone()
        keys = (
            "as_of",
            "pulled_at",
            "stale",
            "consecutive_failures",
            "last_error",
            "mcp_count",
            "skill_count",
        )
        state = dict(zip(keys, row, strict=True))
        state["empty"] = state["pulled_at"] is None
        return state

    def list_mcp_rows(self) -> list[dict[str, Any]]:
        """All stored mcp rows (raw spine shape), ordered by spine id."""
        return self._list("spine_mcp_servers")

    def get_mcp_row(self, server_id: str) -> dict[str, Any] | None:
        """One stored mcp row by spine id, or None."""
        return self._get("spine_mcp_servers", server_id)

    def list_skill_rows(self) -> list[dict[str, Any]]:
        """All stored skill rows (raw spine shape), ordered by spine id."""
        return self._list("spine_skills")

    def get_skill_row(self, skill_id: str) -> dict[str, Any] | None:
        """One stored skill row by spine id, or None."""
        return self._get("spine_skills", skill_id)

    def _list(self, table: str) -> list[dict[str, Any]]:
        if table not in ("spine_mcp_servers", "spine_skills"):  # nosec B608 - allowlisted
            raise ValueError(f"Unknown replica table: {table}")
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT payload FROM {table} ORDER BY id"  # nosec B608 - allowlisted
            )
            return [json.loads(payload) for (payload,) in cursor.fetchall()]

    def _get(self, table: str, row_id: str) -> dict[str, Any] | None:
        if table not in ("spine_mcp_servers", "spine_skills"):  # nosec B608 - allowlisted
            raise ValueError(f"Unknown replica table: {table}")
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT payload FROM {table} WHERE id = ?",  # nosec B608 - allowlisted
                (row_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])


def _row_ts(row: dict[str, Any]) -> int:
    """Extract the row's updated_at (epoch ms) for indexing; 0 when absent."""
    value = row.get("updated_at")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
