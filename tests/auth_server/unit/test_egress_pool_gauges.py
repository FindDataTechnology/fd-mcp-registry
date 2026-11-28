"""Egress-pool gauges for the /metrics scrape.

Both numbers here were misread during the 2026-11-28 outage, and each misreading
points at the opposite action, so they are pinned separately.

``pool._requests`` holds active and queued requests together; only ``is_queued()``
entries are waiting for a slot. ``pool.connections`` holds every connection
object; only ``is_idle()`` ones are actually available. An exhausted pool reports
100 connections and 0 idle, which reads as "everything is free" if the gauge
counts them all.
"""

from types import SimpleNamespace

from auth_server.server import _egress_pool_gauges


class _Req:
    def __init__(self, queued: bool) -> None:
        self._queued = queued

    def is_queued(self) -> bool:
        return self._queued


class _Conn:
    def __init__(self, idle: bool) -> None:
        self._idle = idle

    def is_idle(self) -> bool:
        return self._idle


def _pool(
    max_connections: int = 10,
    requests: list | None = None,
    connections: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        _max_connections=max_connections,
        _requests=requests or [],
        connections=connections or [],
    )


def test_only_queued_requests_count_as_waiting() -> None:
    gauges = _egress_pool_gauges(_pool(requests=[_Req(True), _Req(False), _Req(True)]))

    assert gauges["waiting_requests"] == 2


def test_idle_connections_excludes_in_use_ones() -> None:
    gauges = _egress_pool_gauges(_pool(connections=[_Conn(True), _Conn(False), _Conn(True)]))

    assert gauges["idle_connections"] == 2
    assert gauges["connections_total"] == 3


def test_exhausted_pool_reports_zero_idle_and_full_total() -> None:
    pool = _pool(
        max_connections=100,
        requests=[_Req(True)] * 5,
        connections=[_Conn(False)] * 100,
    )

    gauges = _egress_pool_gauges(pool)

    assert gauges["idle_connections"] == 0
    assert gauges["connections_total"] == 100
    assert gauges["waiting_requests"] == 5


def test_absent_attributes_degrade_to_zero() -> None:
    gauges = _egress_pool_gauges(SimpleNamespace())

    assert gauges == {
        "max_connections": 0,
        "waiting_requests": 0,
        "idle_connections": 0,
        "connections_total": 0,
    }
