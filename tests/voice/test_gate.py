"""Defect D (Round 19): no session for a WebRTC connection that never connects, and one
connection per page. Pure asyncio with fake peer connections: runs in `make test`."""

from __future__ import annotations

import asyncio

from mockboard.voice.gate import PageSessions, wait_connected


class FakePC:
    def __init__(self):
        self.connectionState = "new"
        self._handlers = []

    def on(self, event, fn):
        self._handlers.append(fn)

    def remove_listener(self, event, fn):
        self._handlers.remove(fn)

    def set(self, state):
        self.connectionState = state
        for fn in list(self._handlers):
            fn()


class FakeConn:
    def __init__(self, pc_id):
        self.pc_id, self.pc = pc_id, FakePC()
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True
        self.pc.set("closed")


async def test_wait_connected_true_false_and_timeout():
    c = FakeConn("a")
    task = asyncio.create_task(wait_connected(c, 1.0))
    await asyncio.sleep(0.01)
    c.pc.set("connecting")
    c.pc.set("connected")
    assert await task and c.pc._handlers == []
    f = FakeConn("b")
    task = asyncio.create_task(wait_connected(f, 1.0))
    await asyncio.sleep(0.01)
    f.pc.set("failed")
    assert not await task
    assert not await wait_connected(FakeConn("c"), 0.05)


async def test_a_connection_that_never_connects_gets_no_session():
    ran = []

    async def run(conn):
        ran.append(conn.pc_id)

    gate = PageSessions(run, connect_timeout_s=0.1)
    ghost = FakeConn("ghost")
    await gate.offer(ghost, "page-1")
    await asyncio.sleep(0.25)
    assert ran == [] and gate.abandoned == ["ghost"] and ghost.disconnected
    assert "page-1" not in gate.pages


async def test_a_new_offer_from_the_same_page_replaces_the_old_one():
    ran, stop = [], asyncio.Event()

    async def run(conn):
        ran.append(conn.pc_id)
        await stop.wait()

    gate = PageSessions(run, connect_timeout_s=5)
    first = FakeConn("first")
    await gate.offer(first, "page-1")  # still connecting when the page retries
    second = FakeConn("second")
    await gate.offer(second, "page-1")
    second.pc.set("connected")
    await asyncio.sleep(0.05)
    assert first.disconnected and gate.replaced == ["first"]
    assert ran == ["second"] and gate.started == ["second"]
    # A running session is also replaced by a newer offer: its connection is closed.
    third = FakeConn("third")
    await gate.offer(third, "page-1")
    assert second.disconnected
    stop.set()
    await gate.close_all()


async def test_different_pages_do_not_affect_each_other():
    ran = []

    async def run(conn):
        ran.append(conn.pc_id)

    gate = PageSessions(run, connect_timeout_s=1)
    a, b = FakeConn("a"), FakeConn("b")
    await gate.offer(a, "p1")
    await gate.offer(b, "p2")
    a.pc.set("connected")
    b.pc.set("connected")
    await asyncio.sleep(0.05)
    assert sorted(ran) == ["a", "b"] and not a.disconnected and not b.disconnected


async def test_on_unstarted_fires_for_a_connection_that_never_connects():
    gone: list[str] = []

    async def run(_connection) -> None:
        raise AssertionError("no session for this one")

    gate = PageSessions(run, connect_timeout_s=0.01, on_unstarted=gone.append)
    connection = FakeConn("a")
    await gate.offer(connection, "page")
    await asyncio.gather(*gate.tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert gone == ["a"]
