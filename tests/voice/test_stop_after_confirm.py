"""The End button lost its stop message (Nov 9, live, owner).

The page asks "End the interview now?" with `window.confirm()`, which freezes the page and its
1 s pinger. Pipecat 1.5.0's SmallWebRTCConnection treats a page with no ping for 3 s as gone and
queues its app messages until a reconnect, so the stop sat in a queue, no report was made and the
page waited on the interview screen. The page now sends a ping just before the stop, on the same
ordered data channel. The first test drives Pipecat's real message handler, so a Pipecat upgrade
that changes this rule fails here.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("pipecat")
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

JS = Path("src/mockboard/voice/static/app.js").read_text(encoding="utf-8")
STOP = json.dumps({"type": "mockboard", "event": "stop"})


class _Channel:
    """Stands in for aiortc's data channel: keeps the handlers Pipecat registers on it."""

    def __init__(self) -> None:
        self.handlers: dict = {}

    def on(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn

        return register


async def _deliveries(messages: list[str], *, silent_for_s: float) -> tuple[list, int]:
    connection = SmallWebRTCConnection()
    got: list = []

    @connection.event_handler("app-message")
    async def _record(_connection, message):
        got.append(message)

    connection._connect_invoked = True  # the pipeline is running, as in a sitting
    channel = _Channel()
    connection._pc.emit("datachannel", channel)
    connection._last_received_time = time.time() - silent_for_s  # the page was frozen
    for message in messages:
        await channel.handlers["message"](message)
    await asyncio.sleep(0.05)  # Pipecat runs app-message handlers as tasks
    return got, len(connection._pending_app_messages)


def test_pipecat_queues_a_stop_from_a_page_that_was_frozen_and_takes_it_after_a_ping():
    got, queued = asyncio.run(_deliveries([STOP], silent_for_s=10))
    assert got == [] and queued == 1  # the bug: lost until a reconnect
    got, queued = asyncio.run(_deliveries(["ping: 1", STOP], silent_for_s=10))
    assert got == [json.loads(STOP)] and queued == 0


def test_the_page_pings_right_before_it_sends_stop():
    start = JS.index("async function requestStop(")
    body = JS[start : JS.index("\n}\n", start)]
    ping = body.index('dc.send("ping: "')
    stop = body.index('dc.send(JSON.stringify({ type: "mockboard", event: "stop" }))')
    assert ping < stop
    assert body[ping:stop].count("dc.send") == 1  # nothing sent in between
