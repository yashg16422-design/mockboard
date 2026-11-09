"""No pipeline for a connection nobody uses (defect D, Round 19).

The owner's first run showed two peer connections from one page: the first offer never
connected (it timed out about 80 s later), yet a whole session pipeline had been built for it.
In real mode that pipeline opens a Prisma stream (billed while it streams) and a Timbre socket
for nobody. So a session starts only once its WebRTC connection really is `connected`:

* `wait_connected(connection, timeout_s)` watches the aiortc peer connection's state (the
  SmallWebRTC `connected` event fires only after a transport called `connect()`, which is too
  late) and returns True on `connected`, False on `failed`, `closed` or the timeout.
* `PageSessions` keeps at most one connection per browser page (`page_id`, sent by the page
  with its offer). A new offer from the same page closes the previous connection and its
  pipeline first. A connection that never connects is closed, and no session is built for it.

Pure asyncio plus duck typing on the connection (`.pc.connectionState`, `.pc.on`,
`.pc.remove_listener`, `.disconnect()`, `.pc_id`), so it is tested without a browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mockboard.voice.gate")  # stdlib: no voice extra needed

DONE_STATES = ("connected", "failed", "closed")


async def wait_connected(connection: Any, timeout_s: float) -> bool:
    pc = connection.pc
    changed = asyncio.Event()

    def check() -> None:
        if pc.connectionState in DONE_STATES:
            changed.set()

    pc.on("connectionstatechange", check)
    try:
        check()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout_s)
    finally:
        with contextlib.suppress(Exception):
            pc.remove_listener("connectionstatechange", check)
    return pc.connectionState == "connected"


@dataclass
class _Entry:
    connection: Any
    task: asyncio.Task | None = None
    started: bool = False  # the session (pipeline) was built


@dataclass
class PageSessions:
    """Starts `run(connection)` only for connections that connect; one per page."""

    run: Callable[[Any], Awaitable[None]]
    connect_timeout_s: float = 15.0
    pages: dict[str, _Entry] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    started: list[str] = field(default_factory=list)  # pc ids whose session was built
    abandoned: list[str] = field(default_factory=list)  # pc ids closed without a session
    replaced: list[str] = field(default_factory=list)  # pc ids closed by a newer offer
    on_unstarted: Callable[[str], None] | None = None  # a connection ended with no session

    async def offer(self, connection: Any, page_id: str | None) -> None:
        """Called with each new SmallWebRTC connection (the offer's callback). Never blocks."""
        if page_id and (old := self.pages.pop(page_id, None)) is not None:
            self.replaced.append(old.connection.pc_id)
            logger.info("[voice] new offer from the same page: closing %s", old.connection.pc_id)
            await self._close(old)
        entry = _Entry(connection)
        entry.task = asyncio.create_task(self._gate(entry, page_id))
        self.tasks.add(entry.task)
        entry.task.add_done_callback(self.tasks.discard)
        # A done callback also fires for a task cancelled before its first step.
        entry.task.add_done_callback(lambda _task: self._finished(entry))
        if page_id:
            self.pages[page_id] = entry

    def _finished(self, entry: _Entry) -> None:
        if not entry.started and self.on_unstarted is not None:
            self.on_unstarted(entry.connection.pc_id)

    async def _gate(self, entry: _Entry, page_id: str | None) -> None:
        conn = entry.connection
        try:
            if not await wait_connected(conn, self.connect_timeout_s):
                self.abandoned.append(conn.pc_id)
                logger.warning("[voice] %s did not connect (%s); no session was started for it",
                               conn.pc_id, conn.pc.connectionState)
                with contextlib.suppress(Exception):
                    await conn.disconnect()
                return
            entry.started = True
            self.started.append(conn.pc_id)
            await self.run(conn)
        finally:
            if page_id and self.pages.get(page_id) is entry:
                self.pages.pop(page_id, None)

    async def _close(self, entry: _Entry) -> None:
        # Disconnecting lets a running session end through its normal path (the transport's
        # on_client_disconnected); a gate still waiting is simply cancelled.
        with contextlib.suppress(Exception):
            await entry.connection.disconnect()
        task = entry.task
        if task is not None and not task.done() and not entry.started:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def close_all(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        for entry in list(self.pages.values()):
            with contextlib.suppress(Exception):
                await entry.connection.disconnect()
        self.pages.clear()
