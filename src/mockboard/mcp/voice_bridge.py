"""The Gnani voice room for the MCP channel (Option 2, CTO_NOTES_18 §8; owner, Nov 9).

The person's own AI is the brain; Mock Board's code decides every move (`service.py`); Gnani
Prisma listens and Gnani Timbre speaks, at our cost, through this bridge. It lives in the MCP
server's own process and listens on 127.0.0.1 only:

    GET  /room            the voice room page (also the MCP App view, `ui://mockboard/voice-room`)
    WS   /ws?s=<id>&k=<token>
         page -> bridge   binary: 16-bit mono PCM at the Prisma rate, only while the candidate
                          speaks; JSON: {"type": "hello"}, {"type": "turn_end"},
                          {"type": "typed_answer", "text": ...}, {"type": "played", "seq": n}
         bridge -> page   JSON events (state, caption, heard, prep, decision, finished, audio
                          headers) and binary Timbre PCM after each audio header

The AI drives a sitting with three tools: `say` (Timbre speaks each member's line in that
member's voice), `wait_for_answer` (returns what Prisma heard, or a typed answer) and the usual
`next_turn` (Mock Board's decision, which this bridge also draws as a node in the page's graph).

Every session has its own secret token; a page without it gets nothing. Audio is never stored.
`FakeSpeech` (no Gnani, free) serves tests and `MOCKBOARD_FAKE_SPEECH=1`.

UNVERIFIED: whether Claude Desktop shows this page as a panel and grants it the microphone is
what `docs/MCP_PROBE.md` measures; the same page also works in a normal browser tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from mockboard.config import DEFAULT_PANEL_VOICES, REPO_ROOT, Settings
from mockboard.speech import gnani_protocol as g

logger = logging.getLogger("mockboard.mcp.voice")

PAGE_PATH = Path(__file__).parent / "static" / "voice_room.html"
VIEW_URI = "ui://mockboard/voice-room.html"
APP_MIME = "text/html;profile=mcp-app"
FIRST_PORT = 8791
PORT_TRIES = 20
MAX_EVENTS = 400
MAX_TTS_CHARS = 380  # Timbre takes at most 400 characters a request
WAIT_LIMIT_S = 50.0  # a tool call returns before a host's timeout; the AI calls again
FINAL_WAIT_S = 2.5  # after the turn ends: time for Prisma's in-flight final transcript
QUIET_S = 0.7  # no new final for this long after the turn ended: the answer is complete
READY_WAIT_S = 30.0  # the first `say` waits for the page's preparation animation to finish
ANSWER_MAX_CHARS = 4000
_SENTENCE = re.compile(r"(?<=[.?!।])\s+")


def load_settings() -> Settings:
    """The repository's own `.env` (the Gnani key), wherever Claude Desktop starts the server."""
    return Settings(_env_file=str(REPO_ROOT / ".env"))  # type: ignore[call-arg]


def gnani_key(settings: Settings) -> str:
    secret = settings.gnani_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key or key.startswith("replace-me"):
        raise RuntimeError("the Gnani key is not set in the repository's .env")
    return key


def tts_chunks(text: str) -> list[str]:
    """Sentences packed into requests Timbre accepts (at most `MAX_TTS_CHARS` each)."""
    out: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        sentence = sentence.strip()
        while len(sentence) > MAX_TTS_CHARS:  # one very long sentence: cut at a space
            cut = sentence.rfind(" ", 0, MAX_TTS_CHARS)
            cut = cut if cut > 0 else MAX_TTS_CHARS
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if out and len(out[-1]) + 1 + len(sentence) <= MAX_TTS_CHARS:
            out[-1] = f"{out[-1]} {sentence}"
        else:
            out.append(sentence)
    return out


# --- speech ---------------------------------------------------------------------------------


class Listener(Protocol):
    async def feed(self, pcm: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def close(self) -> None: ...


class Speech(Protocol):
    sample_rate: int

    async def synthesize(self, text: str, voice: str, language: str) -> bytes: ...
    async def listener(self, lang_code: str) -> Listener: ...


class PrismaListener:
    """One candidate turn on one Prisma socket: audio is streamed as it arrives, finals are
    collected, and `finish` sends trailing silence and waits briefly for the last final."""

    def __init__(self, settings: Settings, key: str, lang_code: str) -> None:
        self.settings = settings
        self.cfg = g.STTConfig.from_settings(settings, lang_code=lang_code)
        self.headers = g.build_stt_headers(self.cfg, key)
        self.finals: list[str] = []
        self.ws: Any = None
        self._buf = b""
        self._recv: asyncio.Task | None = None
        self._last_final = 0.0

    async def connect(self) -> None:
        from websockets.asyncio.client import connect

        self.ws = await connect(self.settings.gnani_stt_url, additional_headers=self.headers,
                                max_size=None, open_timeout=10)
        first = g.parse_stt_message(await asyncio.wait_for(self.ws.recv(), 10))
        if first.type == "error":
            raise ConnectionError(str(first.raw.get("message", "speech service error")))
        self._recv = asyncio.create_task(self._receive())

    async def _receive(self) -> None:
        with contextlib.suppress(Exception):
            async for data in self.ws:
                msg = g.parse_stt_message(data)
                if msg.type == "transcript" and not msg.is_partial and msg.text.strip():
                    self.finals.append(msg.text.strip())
                    self._last_final = msg.arrived

    async def feed(self, pcm: bytes) -> None:
        self._buf += pcm
        whole = len(self._buf) - len(self._buf) % g.FRAME_BYTES
        for frame in g.iter_frames(self._buf[:whole]):
            await self.ws.send(frame)
        self._buf = self._buf[whole:]

    async def finish(self) -> str:
        silence = b"\x00" * (int(g.TRAILING_SILENCE_S * self.cfg.sample_rate) * 2)
        with contextlib.suppress(Exception):
            for frame in g.iter_frames(self._buf + silence):
                await self.ws.send(frame)
        self._buf = b""
        sent = time.monotonic()
        deadline = sent + FINAL_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            # Prisma has usually closed the answer during the candidate's own pause, before the
            # turn ended (a live check, Nov 9: 2.1 s spent waiting for a new final). Once there
            # is text and nothing new for a moment, the answer is complete.
            if self.finals and time.monotonic() - max(self._last_final, sent) > QUIET_S:
                break
        await self.close()
        return " ".join(self.finals).strip()

    async def close(self) -> None:
        if self._recv is not None:
            self._recv.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv
            self._recv = None
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None


class GnaniSpeech:
    """Gnani Prisma (`settings.stt_model`) and Gnani Timbre (`settings.tts_model`)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.key = gnani_key(settings)
        self.sample_rate = settings.tts_sample_rate

    async def synthesize(self, text: str, voice: str, language: str) -> bytes:
        from mockboard.speech.replay_speech import synthesize
        from mockboard.speech.tts_text import hinglish_for_tts

        spoken = hinglish_for_tts(text) if language == "hinglish" else text
        return await synthesize(spoken, voice, self.settings, api_key=self.key)

    async def listener(self, lang_code: str) -> Listener:
        lst = PrismaListener(self.settings, self.key, lang_code)
        await lst.connect()
        return lst


class _FakeListener:
    async def feed(self, pcm: bytes) -> None:
        return None

    async def finish(self) -> str:
        return ""

    async def close(self) -> None:
        return None


class FakeSpeech:
    """No Gnani: a short tone per line, and no transcript (answers are typed). Free."""

    sample_rate = 16000

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice: str, language: str) -> bytes:
        self.spoken.append((voice, text))
        return b"\x00\x00" * int(0.02 * self.sample_rate * max(1, len(text.split())))

    async def listener(self, lang_code: str) -> Listener:
        return _FakeListener()


# --- one room -------------------------------------------------------------------------------


@dataclass
class VoiceRoom:
    session_id: str
    token: str
    language: str
    lang_code: str
    voices: dict[str, str]
    names: dict[str, str]
    speech: Speech
    pages: set[Any] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    state: str = "waiting"
    answer: asyncio.Future | None = None
    listener: Listener | None = None
    listening_since: float = 0.0
    seq: int = 0
    played: dict[int, asyncio.Event] = field(default_factory=dict)
    finished: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # the page's preparation is drawn

    # -- to the page --------------------------------------------------------------------

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        del self.events[:-MAX_EVENTS]
        await self._send(json.dumps(event, ensure_ascii=False))

    async def _send(self, data: str | bytes) -> None:
        for ws in list(self.pages):
            try:
                await ws.send(data)
            except Exception:  # a closed tab
                self.pages.discard(ws)

    async def set_state(self, state: str) -> None:
        self.state = state
        await self.publish({"type": "state", "state": state})

    # -- the AI's tools -----------------------------------------------------------------

    async def say(self, lines: list[dict[str, str]]) -> dict[str, Any]:
        """Speak each line in its member's voice; returns when the page has played them."""
        if not self.pages:
            for line in lines:
                await self.publish({"type": "caption", "side": "board", **line})
            return {"spoken": False, "reason": "the voice room is not open yet: ask the person to "
                    "open it (the panel, or voice_url in a browser), then call say again"}
        if not self.ready.is_set():  # let the preparation graph finish before the first word
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.ready.wait(), READY_WAIT_S)
        await self.set_state("speaking")
        total_ms = 0
        for line in lines:
            member, text = line["member"], line["text"]
            voice = self.voices.get(member) or next(iter(self.voices.values()))
            await self.publish({"type": "caption", "side": "board", "member": member,
                                "speaker": self.names.get(member, member), "text": text})
            for chunk in tts_chunks(text):
                pcm = await self.speech.synthesize(chunk, voice, self.language)
                self.seq += 1
                seq = self.seq
                done = self.played[seq] = asyncio.Event()
                await self._send(json.dumps({"type": "audio", "seq": seq, "member": member,
                                             "rate": self.speech.sample_rate}))
                await self._send(pcm)
                ms = int(len(pcm) / 2 / self.speech.sample_rate * 1000)
                total_ms += ms
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(done.wait(), ms / 1000 + 4.0)
                self.played.pop(seq, None)
        await self._start_listening()
        return {"spoken": True, "lines": len(lines), "audio_ms": total_ms}

    async def _start_listening(self) -> None:
        loop = asyncio.get_running_loop()
        if self.answer is None or self.answer.done():
            self.answer = loop.create_future()
        self.listening_since = time.monotonic()
        await self.set_state("listening")

    async def wait_for_answer(self, timeout_s: float) -> dict[str, Any]:
        if self.answer is None:
            await self._start_listening()
        assert self.answer is not None
        try:
            got = await asyncio.wait_for(asyncio.shield(self.answer), timeout_s)
        except TimeoutError:
            return {"status": "waiting", "note": "no answer yet: call wait_for_answer again"}
        self.answer = None
        return got

    # -- from the page ------------------------------------------------------------------

    async def on_audio(self, pcm: bytes) -> None:
        if self.state != "listening":
            return  # the board is speaking: half duplex, so its own voice is never heard
        if self.listener is None:
            try:
                self.listener = await self.speech.listener(self.lang_code)
            except Exception as exc:
                logger.warning("voice: Prisma did not open: %s", type(exc).__name__)
                await self.publish({"type": "problem", "text": "Speech recognition did not "
                                    "start. You can type your answer instead."})
                return
        await self.listener.feed(pcm)

    async def on_turn_end(self) -> None:
        if self.state != "listening":
            return
        await self.set_state("thinking")
        text = ""
        if self.listener is not None:
            try:
                text = await self.listener.finish()
            finally:
                self.listener = None
        await self._answer(text, "voice")

    async def on_typed(self, text: str) -> None:
        if self.listener is not None:
            await self.listener.close()
            self.listener = None
        await self.set_state("thinking")
        await self._answer(text.strip()[:ANSWER_MAX_CHARS], "typed")

    async def _answer(self, text: str, source: str) -> None:
        await self.publish({"type": "heard", "text": text, "source": source})
        result = {"status": "answered" if text else "silence", "candidate_said": text,
                  "source": source,
                  "spoke_after_ms": int((time.monotonic() - self.listening_since) * 1000)}
        if self.answer is not None and not self.answer.done():
            self.answer.set_result(result)

    async def on_played(self, seq: int) -> None:
        event = self.played.get(seq)
        if event is not None:
            event.set()

    async def close(self) -> None:
        if self.listener is not None:
            await self.listener.close()
            self.listener = None
        for ws in list(self.pages):
            with contextlib.suppress(Exception):
                await ws.close()
        self.pages.clear()


# --- the server -----------------------------------------------------------------------------


class VoiceBridge:
    """All voice rooms of one MCP server process, on one 127.0.0.1 port."""

    def __init__(
        self,
        settings: Settings | None = None,
        speech_factory: Callable[[Settings], Speech] | None = None,
        port: int | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        fake = os.environ.get("MOCKBOARD_FAKE_SPEECH") == "1"
        self._factory = speech_factory or ((lambda s: FakeSpeech()) if fake else GnaniSpeech)
        self._speech: Speech | None = None
        self.port = port or 0
        self.rooms: dict[str, VoiceRoom] = {}
        self.latest: str | None = None
        self._server: Any = None

    @property
    def speech(self) -> Speech:
        if self._speech is None:
            self._speech = self._factory(self.settings)
        return self._speech

    async def start(self) -> None:
        if self._server is not None:
            return
        from websockets.asyncio.server import serve

        want = self.port or int(os.environ.get("MOCKBOARD_VOICE_PORT", FIRST_PORT))
        for port in range(want, want + PORT_TRIES):
            try:
                self._server = await serve(self._handle, "127.0.0.1", port,
                                           process_request=self._http, max_size=2**22)
            except OSError:
                continue
            self.port = port
            logger.warning("voice room on http://127.0.0.1:%s/room", port)
            return
        raise RuntimeError("no free port on 127.0.0.1 for the voice room")

    async def aclose(self) -> None:
        for room in list(self.rooms.values()):
            await room.close()
        self.rooms.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def view_meta(self) -> dict[str, Any]:
        origins = [f"{scheme}://{host}:{self.port}" for scheme in ("http", "ws")
                   for host in ("127.0.0.1", "localhost")]
        return {"ui": {"csp": {"connectDomains": origins}, "permissions": {"microphone": {}},
                       "prefersBorder": True}}

    async def open_room(self, session_id: str, language: str, lang_code: str,
                        names: dict[str, str]) -> VoiceRoom:
        await self.start()
        _ = self.speech  # fails here, before the sitting, if the Gnani key is missing
        table = self.settings.panel_voices() or DEFAULT_PANEL_VOICES.get(
            self.settings.tts_model, {})
        voices = {m: table.get(m, self.settings.tts_voice) for m in names}
        room = VoiceRoom(session_id, secrets.token_urlsafe(16), language, lang_code, voices,
                         names, self.speech)
        self.rooms[session_id] = room
        self.latest = session_id
        return room

    def room(self, session_id: str) -> VoiceRoom | None:
        return self.rooms.get(session_id)

    async def drop(self, session_id: str) -> None:
        room = self.rooms.pop(session_id, None)
        if room is not None:
            await room.close()

    def ticket(self, room: VoiceRoom) -> dict[str, Any]:
        base = f"127.0.0.1:{self.port}"
        query = f"s={room.session_id}&k={room.token}"
        return {"session_id": room.session_id, "voice_url": f"http://{base}/room?{query}",
                "ws_url": f"ws://{base}/ws?{query}"}

    # -- HTTP and WebSocket --------------------------------------------------------------

    def _http(self, connection: Any, request: Any) -> Any:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        path = urlparse(request.path).path
        if path == "/ws":
            return None  # go on to the WebSocket handshake
        if path in ("/", "/room"):
            body = page_html(self.port).encode("utf-8")
            headers = Headers([("Content-Type", "text/html; charset=utf-8"),
                               ("Cache-Control", "no-store"),
                               ("Content-Length", str(len(body)))])
            return Response(200, "OK", headers, body)
        body = b"not found"
        return Response(404, "Not Found", Headers([("Content-Length", str(len(body)))]), body)

    async def _handle(self, ws: Any) -> None:
        query = parse_qs(urlparse(ws.request.path).query)
        sid = (query.get("s") or [""])[0]
        token = (query.get("k") or [""])[0]
        room = self.rooms.get(sid)
        if room is None or not secrets.compare_digest(token, room.token):
            await ws.close(4403, "no such room")
            return
        room.pages.add(ws)
        try:
            for event in room.events:  # a page that opens late still sees the whole sitting
                await ws.send(json.dumps(event, ensure_ascii=False))
            await ws.send(json.dumps({"type": "state", "state": room.state}))
            async for data in ws:
                if isinstance(data, bytes):
                    await room.on_audio(data)
                    continue
                await self._on_text(room, data)
        except Exception:  # a dropped connection ends this page only
            pass
        finally:
            room.pages.discard(ws)

    async def _on_text(self, room: VoiceRoom, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "turn_end":
            await room.on_turn_end()
        elif kind == "typed_answer" and isinstance(msg.get("text"), str):
            await room.on_typed(msg["text"])
        elif kind == "played" and isinstance(msg.get("seq"), int):
            await room.on_played(msg["seq"])
        elif kind == "ready":
            room.ready.set()


# The web app's own look, built into the page so it is the same in a Claude panel (where the page
# cannot load files) and in a browser tab: its tokens, its particle field, its fonts (SIL OFL 1.1).
WEB_STATIC = Path(__file__).resolve().parents[1] / "voice" / "static"
_FONTS = (  # (family, weights, file)
    ("Geist", "200 500", "geist-latin.woff2"),
    ("Geist Mono", "400 500", "geist-mono-latin.woff2"),
    ("Noto Sans Devanagari", "300 600", "noto-sans-devanagari.woff2"),
)
_PAGE_CACHE: dict[int, str] = {}


def _font_faces() -> str:
    import base64

    out = []
    for family, weights, name in _FONTS:
        path = WEB_STATIC / "fonts" / name
        if not path.exists():
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        out.append(f'@font-face {{ font-family: "{family}"; font-weight: {weights}; '
                   f'font-display: swap; src: url(data:font/woff2;base64,{data}) '
                   f'format("woff2"); }}')
    return "\n".join(out)


def page_html(port: int) -> str:
    """The voice room page with the web app's tokens, fonts and particle field inside it."""
    if port not in _PAGE_CACHE:
        html = PAGE_PATH.read_text(encoding="utf-8")
        tokens = (WEB_STATIC / "tokens.css").read_text(encoding="utf-8")
        presence = (WEB_STATIC / "presence.js").read_text(encoding="utf-8")
        html = html.replace("/*__TOKENS__*/", tokens + "\n" + _font_faces())
        html = html.replace("//__PRESENCE__", presence.replace("</script", "<\\/script"))
        _PAGE_CACHE[port] = html.replace("__PORT__", str(port))
    return _PAGE_CACHE[port]
