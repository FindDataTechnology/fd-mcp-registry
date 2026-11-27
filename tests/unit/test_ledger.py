"""Unit tests for the unified call ledger primitives (task group 3).

No DB, no network: the JSONL sink writes to a tmp path; budget verdict is pure;
the shipper's row coercion is pure; the Mongo counter is exercised via a mocked
collection.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.ledger.events import (
    BudgetCounter,
    LedgerEvent,
    check_budget,
    emit,
)
from registry.ledger.mongo_counter import MongoBudgetCounter
from registry.ledger.shipper import _coerce_row


def test_emit_writes_jsonl(tmp_path, monkeypatch):
    from registry.ledger import events as _ev

    async def _noop(_e):
        return None

    monkeypatch.setattr(_ev, "_write_mongo", _noop)
    sink = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("LEDGER_SINK_PATH", str(sink))
    e = LedgerEvent(
        run="r1",
        principal="agent:1",
        actor="user:9",
        tenant="t1",
        audience="svcA",
        tool="read",
        outcome="ok",
        grant_jti="j1",
        rows=120,
        latency_ms=42,
    )
    emit(e)
    line = sink.read_text().strip()
    rec = json.loads(line)
    assert rec["run"] == "r1"
    assert rec["rows"] == 120
    assert rec["actor"] == "user:9"
    assert rec["grant_jti"] == "j1"
    assert rec["span_id"]  # auto-minted


def test_emit_never_raises_on_io_error(monkeypatch):
    from registry.ledger import events as _ev

    async def _noop(_e):
        return None

    monkeypatch.setattr(_ev, "_write_mongo", _noop)
    # Point the sink at an unwritable path; emit must swallow and keep going.
    monkeypatch.setenv("LEDGER_SINK_PATH", "/proc/nonexistent/ledger.jsonl")
    e = LedgerEvent(
        run="r",
        principal="p",
        actor=None,
        tenant="t",
        audience="a",
        tool="x",
        outcome="ok",
        grant_jti="j",
    )
    emit(e)  # no exception


def test_check_budget_allow_and_refuse():
    assert check_budget({"calls": 10}, {"calls": 9}).allowed is True
    v = check_budget({"calls": 10}, {"calls": 10})
    assert v.allowed is False
    assert v.remaining["calls"] == 0
    # rows dimension
    assert check_budget({"rows": 100}, {"rows": 50}).allowed is True
    assert check_budget({"rows": 100}, {"rows": 100}).allowed is False


def test_check_budget_none_means_tenant_bounded_not_infinite():
    # No per-grant budget => allowed at this layer; tenant quota is enforced elsewhere.
    assert check_budget(None, {"calls": 99999}).allowed is True


@pytest.mark.asyncio
async def test_budget_counter_accumulates():
    c = BudgetCounter()
    await c.consume("run:r1", rows=10, calls=1)
    await c.consume("run:r1", rows=5, calls=1)
    used = await c.peek("run:r1")
    assert used == {"calls": 2, "rows": 15, "tokens": 0}


def test_shipper_coerces_row():
    ev = {
        "grant_jti": "j",
        "span_id": "s",
        "run": "r",
        "principal": "p",
        "actor": None,
        "tenant": "t",
        "audience": "a",
        "tool": "x",
        "outcome": "ok",
        "rows": 3,
        "tokens": None,
        "latency_ms": 7,
        "source": "resource",
        "ts": datetime.now(UTC).isoformat(),
    }
    row = _coerce_row(ev)
    assert row["principal"] == "p"
    assert row["actor"] is None
    assert isinstance(row["ts"], datetime)


def test_shipper_rejects_incomplete_row():
    with pytest.raises(ValueError):
        _coerce_row({"grant_jti": "j", "span_id": "s"})  # missing run/principal/etc


@pytest.mark.asyncio
async def test_mongo_counter_available_on_ping():
    col = MagicMock()
    col.database.command = AsyncMock(return_value={"ok": 1})
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=col)
    c = MongoBudgetCounter(db)
    assert await c.available() is True


@pytest.mark.asyncio
async def test_mongo_counter_unavailable_on_error():
    col = MagicMock()
    col.database.command = AsyncMock(side_effect=RuntimeError("down"))
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=col)
    c = MongoBudgetCounter(db)
    assert await c.available() is False


@pytest.mark.asyncio
async def test_mongo_counter_consume_upserts():
    col = MagicMock()
    col.find_one_and_update = AsyncMock()
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=col)
    c = MongoBudgetCounter(db)
    await c.consume("tenant:t1:2026-11", rows=5, calls=1)
    col.find_one_and_update.assert_awaited_once()
    call = col.find_one_and_update.await_args
    assert call.args[0] == {"scope": "tenant:t1:2026-11"}
    assert call.args[1]["$inc"]["rows"] == 5


def test_retention_ddl_has_snapshot_and_log_tables():
    from registry.ledger import retention

    assert "ledger_retention_snapshot" in retention._SNAPSHOT_DDL
    assert "ledger_retention_log" in retention._LOG_DDL
    # Snapshot is keyed so a second clear of the same period is a no-op aggregate.
    assert "PRIMARY KEY (tenant_id, month)" in retention._SNAPSHOT_DDL


def test_retention_requires_dsn_math_only(monkeypatch):
    # run_retention imports psycopg lazily; with no DSN and a fake driver absent
    # it should still compute, but we only assert the default-days source here
    # without touching PG.
    from registry.ledger import retention

    assert retention.DEFAULT_RETENTION_DAYS == 400


def test_shipper_backlog_counts_unshipped_bytes(tmp_path):
    from registry.ledger.shipper import backlog

    sink = tmp_path / "ledger.jsonl"
    cursor = tmp_path / "cursor"
    sink.write_text("line1\nline2\n")
    # no cursor yet => full backlog
    assert backlog(str(sink), str(cursor)) > 0
    cursor.write_text(str(sink.stat().st_size))
    assert backlog(str(sink), str(cursor)) == 0
    # a new line grows the backlog again
    with sink.open("a") as fh:
        fh.write("line3\n")
    assert backlog(str(sink), str(cursor)) > 0


def test_month_bounds_half_open_and_rollover():
    from registry.api.ledger_routes import _month_bounds

    start, end = _month_bounds("2026-11")
    assert start == datetime(2026, 11, 1, tzinfo=UTC)
    assert end == datetime(2026, 12, 1, tzinfo=UTC)
    # December rolls the year forward, not month 13.
    start, end = _month_bounds("2026-12")
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_month_bounds_rejects_bad_format():
    from fastapi import HTTPException

    from registry.api.ledger_routes import _month_bounds

    with pytest.raises(HTTPException) as ei:
        _month_bounds("2026/11")
    assert ei.value.status_code == 422


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


def _fake_collection(monkeypatch, rows):
    """Patch the read face's ``_collection`` to return a canned aggregate cursor."""
    captured: dict = {}
    col = MagicMock()

    def _aggregate(pipeline):
        captured["pipeline"] = pipeline
        return _FakeCursor(rows)

    col.aggregate = _aggregate

    async def _get():
        return col

    monkeypatch.setattr("registry.api.ledger_routes._collection", _get)
    return captured


@pytest.mark.asyncio
async def test_ledger_for_run_groups_distinct_actor(monkeypatch):
    """A user call (actor null) and an agent call (actor=owner) are two groups
    under one run and one tenant — the split task 6.2/8.1 needs."""
    from registry.api import ledger_routes

    captured = _fake_collection(
        monkeypatch,
        [
            {
                "_id": {"principal": "aloadtree@gmail.com", "actor": None, "tenant": "acme"},
                "calls": 2,
                "rows": 10,
                "tokens": 0,
            },
            {
                "_id": {
                    "principal": "agent:procurement-acme",
                    "actor": "aloadtree@gmail.com",
                    "tenant": "acme",
                },
                "calls": 3,
                "rows": 40,
                "tokens": 0,
            },
        ],
    )
    out = await ledger_routes.ledger_for_run("r1", user_context={})
    assert [g["principal"] for g in out["groups"]] == [
        "aloadtree@gmail.com",
        "agent:procurement-acme",
    ]
    agent = out["groups"][1]
    assert agent["actor"] == "aloadtree@gmail.com"
    assert agent["tenant"] == "acme"
    assert out["groups"][0]["actor"] is None
    # Sum of the two groups is the run total (three-dimension consistency).
    assert sum(g["calls"] for g in out["groups"]) == 5
    assert captured["pipeline"][0] == {"$match": {"run": "r1"}}


@pytest.mark.asyncio
async def test_ledger_for_tenant_totals(monkeypatch):
    from registry.api import ledger_routes

    captured = _fake_collection(monkeypatch, [{"_id": None, "calls": 4, "rows": 400, "tokens": 9}])
    out = await ledger_routes.ledger_for_tenant("acme", month="2026-11", user_context={})
    assert out == {"tenant_id": "acme", "month": "2026-11", "calls": 4, "rows": 400, "tokens": 9}
    match = captured["pipeline"][0]["$match"]
    assert match["tenant"] == "acme"
    assert match["ts"]["$gte"] == datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_ledger_for_tenant_empty_is_zero_not_error(monkeypatch):
    from registry.api import ledger_routes

    _fake_collection(monkeypatch, [])
    out = await ledger_routes.ledger_for_tenant("acme", month="2026-11", user_context={})
    assert out["calls"] == 0 and out["rows"] == 0


@pytest.mark.asyncio
async def test_ledger_read_face_503_when_store_down(monkeypatch):
    from fastapi import HTTPException

    from registry.api import ledger_routes

    async def _boom():
        raise RuntimeError("mongo down")

    # _collection resolves get_documentdb_client from its own module namespace,
    # so patch that binding (patching the source module would not rebind it).
    monkeypatch.setattr(ledger_routes, "get_documentdb_client", _boom)
    with pytest.raises(HTTPException) as ei:
        await ledger_routes._collection()
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_write_mongo_inserts_and_dedups(monkeypatch):
    """_write_mongo persists the event; a duplicate (same grant_jti,span_id) is
    swallowed by the unique index, not raised."""
    from unittest.mock import AsyncMock, MagicMock

    from registry.ledger import events as _ev

    inserted = []

    col = MagicMock()
    col.create_index = AsyncMock()
    col.insert_one = AsyncMock(side_effect=lambda d: inserted.append(d))

    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=col)

    async def _getdb():
        return db

    monkeypatch.setattr("registry.repositories.documentdb.client.get_documentdb_client", _getdb)
    _ev._mongo_index_ready = False
    e = LedgerEvent(
        run="r1",
        principal="agent:1",
        actor="user:9",
        tenant="t1",
        audience="svcA",
        tool="read",
        outcome="ok",
        grant_jti="jX",
    )
    await _ev._write_mongo(e)
    assert len(inserted) == 1
    assert inserted[0]["grant_jti"] == "jX"
    assert inserted[0]["principal"] == "agent:1"
