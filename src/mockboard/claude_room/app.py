"""`python -m mockboard.claude_room`: Mock Board with Claude as the brain, as a local web app.

One server on 127.0.0.1 (default port 7870), the voice bridge itself, extended with:

    GET  /         the start page: the mode, the form (or a study topic), "Start with Claude"
    WS   /start    the start page's control channel: {"type": "start", ...} starts Claude; the
                   server answers with notes, errors, and {"type": "room", "url": ...} when Claude
                   has opened the voice room, where the page then goes
    GET  /room, WS /ws    the voice room (`mcp/voice_bridge.py`)

Only a page served by this server may start a run (the Origin is checked), and one run at a time:
each run uses the person's own Claude plan and Gnani credits.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mockboard.claude_room.runner import Agent, ClaudeRun, claude_agent
from mockboard.mcp.safety import RefusedInput, check_form, check_topic
from mockboard.mcp.service import BoardService
from mockboard.mcp.voice_bridge import FakeSpeech, VoiceBridge, build_page

logger = logging.getLogger("mockboard.claude_room")

START_PAGE = Path(__file__).parent / "static" / "start.html"
DEFAULT_PORT = 7870
PARTNER_LANGUAGES = ("hinglish", "english", "hindi", "tanglish")


class ClaudeRoomBridge(VoiceBridge):
    def __init__(self, service: BoardService, *, port: int = DEFAULT_PORT,
                 fake_speech: bool = False, agent: Agent = claude_agent,
                 settings: Any = None) -> None:
        super().__init__(settings, (lambda s: FakeSpeech()) if fake_speech else None, port)
        self.service = service
        self.agent = agent
        self.run: ClaudeRun | None = None
        from mockboard.config import REPO_ROOT

        self.log_dir = REPO_ROOT / "traces"  # each sitting's log, for diagnosis (never committed)

    # -- HTTP: the start page, then the voice room's own routes ---------------------------

    def _http(self, connection: Any, request: Any) -> Any:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        path = urlparse(request.path).path
        if path == "/start":
            return None
        if path == "/":
            body = build_page(START_PAGE, self.port).encode("utf-8")
            return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8"),
                                                ("Cache-Control", "no-store"),
                                                ("Content-Length", str(len(body)))]), body)
        return super()._http(connection, request)

    async def _handle(self, ws: Any) -> None:
        if urlparse(ws.request.path).path == "/start":
            await self._control(ws)
        else:
            await super()._handle(ws)

    # -- the start page's control channel -----------------------------------------------

    def _origin_ok(self, ws: Any) -> bool:
        origin = ws.request.headers.get("Origin", "")
        return origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    async def _control(self, ws: Any) -> None:
        if not self._origin_ok(ws):
            await ws.close(4403, "start from Mock Board's own page")
            return

        async def send(event: dict[str, Any]) -> None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(event, ensure_ascii=False))

        try:
            async for data in ws:
                try:
                    msg = json.loads(data) if isinstance(data, str) else {}
                except ValueError:
                    msg = {}
                if msg.get("type") == "start":
                    await self._start(msg, send)
                elif msg.get("type") == "stop" and self.run is not None:
                    await self.run.stop()
                    await send({"type": "note", "text": "Stopped."})
        except Exception:  # the start page closed; the run goes on in the voice room
            pass

    async def _start(self, msg: dict[str, Any], send: Any) -> None:
        if self.run is not None and self.run.status in ("starting", "running"):
            await send({"type": "error", "text": "A sitting is already running. Finish or stop "
                                                 "it first."})
            return
        mode = "partner" if msg.get("mode") == "partner" else "upsc"
        try:
            if mode == "partner":
                language = msg.get("language", "hinglish")
                if language not in PARTNER_LANGUAGES:
                    raise RefusedInput("language: choose one from the list")
                payload = {"topic": check_topic(msg.get("topic")), "language": language}
            else:
                payload = check_form(msg.get("form")).model_dump(exclude_none=True)
        except RefusedInput as exc:
            await send({"type": "error", "text": str(exc)})
            return

        async def notify(name: str, args: dict, result: dict, is_error: bool) -> None:
            await self._on_tool(name, result, is_error, send)

        async def on_note(text: str) -> None:
            await send({"type": "note", "text": text})
            room = self.room(self.run.session_id) if self.run and self.run.session_id else None
            if room is not None:
                await room.publish({"type": "agent", "text": text[:600]})

        self.run = ClaudeRun(self.service, mode, payload, notify, on_note, agent=self.agent)
        await send({"type": "note", "text": "Claude is reading your form…" if mode == "upsc"
                    else "Claude is reading your topic…"})
        self.run.start()

    async def _on_tool(self, name: str, result: dict, is_error: bool, send: Any) -> None:
        if is_error:
            return
        if name in ("start_voice_interview", "start_voice_partner") and "voice" in result:
            path = urlparse(result["voice"]["voice_url"])
            await send({"type": "room", "url": f"{path.path}?{path.query}"})
            return
        room = self.room(result.get("session_id", "")) if isinstance(result, dict) else None
        if room is None:
            return
        if name == "check_scores":
            await room.publish({"type": "report", "scores": result.get("scores", []),
                                "dropped": len(result.get("dropped_quotes", []))})
        elif name == "check_notes":
            await room.publish({"type": "report", "notes": result.get("points", result)})

    async def aclose(self) -> None:
        if self.run is not None:
            await self.run.stop()
        await super().aclose()


async def serve(port: int, fake_speech: bool) -> None:
    service = BoardService()
    bridge = ClaudeRoomBridge(service, port=port, fake_speech=fake_speech)
    service.voice = bridge
    await bridge.start()
    speech = "fake (free; type your answers)" if fake_speech else "Gnani Prisma and Timbre"
    print(f"\nMock Board with Claude as the brain: open http://localhost:{bridge.port} in Chrome."
          f"\nSpeech: {speech}. Claude: your own Claude Code login. Ctrl+C stops it.\n",
          flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await service.aclose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mockboard.claude_room",
                                description="Mock Board, with Claude as the brain (local).")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--fake-speech", action="store_true",
                   help="no Gnani: a short tone for each line, and you type your answers (free)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.port, args.fake_speech))
    return 0
