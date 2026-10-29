"""Fake Gnani WebSocket servers that replay the documented message shapes (voice chunk V2).

No real Gnani logs exist yet (the owner's probe runs come later), so every message below is
copied from Gnani's docs, fetched Oct 25 and re-checked Oct 30, 2026:

* Prisma: https://docs.gnani.ai/api/STT/stt-websocket.md  (connected / processing /
  transcript / error / settings_updated; 1,024-byte binary frames; close 1008 for a bad
  header and 1001 at the 15-minute cap)
* Timbre: https://docs.gnani.ai/api/TTS/tts-websocket.md  (start / audio / complete / error)

Each server listens on 127.0.0.1 on a free port and records what the client sent (headers,
frame sizes, arrival times, JSON messages), so tests can assert on the wire contract. Only
`websockets` is needed here; the tests that drive Pipecat skip without the voice extra.

When the owner's probe logs exist, replace the shapes below with recorded ones.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

TS = "2026-10-30T10:00:00.000Z"


# --------------------------------------------------------------------------- Prisma shapes


def prisma_connected(sample_rate: int = 16000) -> dict[str, Any]:
    return {
        "type": "connected",
        "message": "Connected to Vachana STT streaming",
        "timestamp": TS,
        "config": {"sample_rate": sample_rate, "chunk_size": 512},
    }


def prisma_processing() -> dict[str, Any]:
    return {"type": "processing", "timestamp": TS}


def prisma_transcript(text: str, index: int, request_id: str = "req_fake") -> dict[str, Any]:
    return {
        "type": "transcript",
        "timestamp": TS,
        "text": text,
        "audio_duration_ms": 1830,
        "segment_id": f"{request_id}-seg-{index}",
        "segment_index": index,
        "latency": 212,
        "utterance_id": f"utt-{index}",
        "is_final": True,
        "output": {"literal": text},
    }


def prisma_error(message: str) -> dict[str, Any]:
    return {"type": "error", "timestamp": TS, "message": message}


def prisma_settings_updated(words: int, score: float) -> dict[str, Any]:
    return {
        "type": "settings_updated",
        "timestamp": TS,
        "config": {
            "language_code": "en-IN",
            "format": "verbatim",
            "itn_native_numerals": False,
            "substitution_enabled": False,
            "bias_word_count": words,
            "bias_score": score,
        },
    }


# --------------------------------------------------------------------------- Prisma server


@dataclass
class PrismaConn:
    """What one client connection sent."""

    headers: dict[str, str]
    frames: list[tuple[float, int]] = field(default_factory=list)  # (monotonic, bytes)
    texts: list[dict[str, Any]] = field(default_factory=list)
    ws: ServerConnection | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def json_types(self) -> list[str]:
        return [str(t.get("type")) for t in self.texts]


# A script decides how the fake server reacts: called after each binary frame with the
# connection record and the frame count; returns messages to send (dicts) or a close code.
Script = Callable[["FakePrisma", PrismaConn, int], Awaitable[None]]


class FakePrisma:
    """A fake Prisma streaming endpoint.

    By default, every `segment_frames` binary frames it sends `processing`, waits
    `transcript_delay_s`, then sends a `transcript` naming the segment. `on_connect` can
    refuse a connection (error + close 1008), and tests can push messages or close a socket
    at any moment through `send` / `close`."""

    def __init__(
        self,
        *,
        segment_frames: int = 0,
        transcript_delay_s: float = 0.05,
        refuse_with_1008: bool = False,
        reply_settings_updated: bool = True,
    ):
        self.segment_frames = segment_frames
        self.transcript_delay_s = transcript_delay_s
        self.refuse_with_1008 = refuse_with_1008
        self.reply_settings_updated = reply_settings_updated
        self.conns: list[PrismaConn] = []
        self.url = ""
        self._server = None
        self._tasks: set[asyncio.Task] = set()
        self.new_conn = asyncio.Condition()

    async def __aenter__(self) -> FakePrisma:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/stt/v3/stream"
        return self

    async def __aexit__(self, *exc) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)

    async def wait_for_conns(self, n: int, timeout: float = 5.0) -> PrismaConn:
        async with self.new_conn:
            await asyncio.wait_for(
                self.new_conn.wait_for(lambda: len(self.conns) >= n), timeout=timeout
            )
        return self.conns[n - 1]

    async def send(self, conn: PrismaConn, message: dict[str, Any]) -> None:
        assert conn.ws is not None
        await conn.ws.send(json.dumps(message))

    async def close(self, conn: PrismaConn, code: int = 1000, reason: str = "") -> None:
        assert conn.ws is not None
        await conn.ws.close(code=code, reason=reason)

    async def _handler(self, ws: ServerConnection) -> None:
        headers = {k.lower(): v for k, v in ws.request.headers.raw_items()}
        conn = PrismaConn(headers=headers, ws=ws)
        async with self.new_conn:
            self.conns.append(conn)
            self.new_conn.notify_all()
        try:
            if self.refuse_with_1008:
                await ws.send(json.dumps(prisma_error("Invalid lang_code")))
                await ws.close(code=1008, reason="Invalid lang_code")
                return
            await ws.send(json.dumps(prisma_connected()))
            segment = 0
            async for msg in ws:
                if isinstance(msg, bytes):
                    conn.frames.append((time.monotonic(), len(msg)))
                    n = len(conn.frames)
                    if self.segment_frames and n % self.segment_frames == 0:
                        segment += 1
                        self._spawn(self._segment(conn, segment))
                else:
                    obj = json.loads(msg)
                    conn.texts.append(obj)
                    if obj.get("type") == "update_settings" and self.reply_settings_updated:
                        bias = obj.get("bias", {})
                        await ws.send(
                            json.dumps(
                                prisma_settings_updated(
                                    len(bias.get("list", [])), bias.get("score", 0.0)
                                )
                            )
                        )
        except Exception:
            pass
        finally:
            conn.closed.set()

    async def _segment(self, conn: PrismaConn, index: int) -> None:
        assert conn.ws is not None
        with contextlib.suppress(Exception):
            await conn.ws.send(json.dumps(prisma_processing()))
            await asyncio.sleep(self.transcript_delay_s)
            await conn.ws.send(json.dumps(prisma_transcript(f"segment {index}", index)))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


# --------------------------------------------------------------------------- Timbre server


def timbre_start(request_id: str = "req_abc123") -> dict[str, Any]:
    return {"type": "start", "message": "Streaming started", "request_id": request_id}


def timbre_audio(index: int, pcm: bytes) -> dict[str, Any]:
    return {
        "type": "audio",
        "data": {
            "chunk_index": index,
            "audio": base64.b64encode(pcm).decode(),
            "is_final": False,
        },
    }


def timbre_complete(index: int) -> dict[str, Any]:
    return {"type": "complete", "data": {"chunk_index": index, "audio": "", "is_final": True}}


def timbre_error(message: str = "Invalid text or audio configuration.") -> dict[str, Any]:
    return {"type": "error", "message": message}


@dataclass
class TimbreConn:
    headers: dict[str, str]
    requests: list[dict[str, Any]] = field(default_factory=list)
    closed_at: float | None = None
    chunks_sent: int = 0


class FakeTimbre:
    """A fake Timbre WebSocket. For each request: `start`, then `chunks` audio messages of
    `chunk_bytes` PCM each, `chunk_gap_s` apart, then `complete`. A request whose text
    contains `error_marker` gets an `error` instead (the socket stays open, as documented)."""

    def __init__(
        self,
        *,
        chunks: int = 20,
        chunk_bytes: int = 1600,
        chunk_gap_s: float = 0.05,
        error_marker: str = "FAIL",
    ):
        self.chunks = chunks
        self.chunk_bytes = chunk_bytes
        self.chunk_gap_s = chunk_gap_s
        self.error_marker = error_marker
        self.conns: list[TimbreConn] = []
        self.url = ""
        self._server = None

    async def __aenter__(self) -> FakeTimbre:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/api/v1/tts"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)

    async def _handler(self, ws: ServerConnection) -> None:
        conn = TimbreConn(headers={k.lower(): v for k, v in ws.request.headers.raw_items()})
        self.conns.append(conn)
        try:
            async for msg in ws:
                req = json.loads(msg)
                conn.requests.append(req)
                if self.error_marker in req.get("text", ""):
                    await ws.send(json.dumps(timbre_error()))
                    continue
                await ws.send(json.dumps(timbre_start()))
                pcm = b"\x01\x00" * (self.chunk_bytes // 2)
                for i in range(1, self.chunks + 1):
                    await asyncio.sleep(self.chunk_gap_s)
                    await ws.send(json.dumps(timbre_audio(i, pcm)))
                    conn.chunks_sent += 1
                await ws.send(json.dumps(timbre_complete(self.chunks + 1)))
        except Exception:
            pass
        finally:
            conn.closed_at = time.monotonic()
