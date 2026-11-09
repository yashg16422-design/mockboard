"""Upgrade #3's speech: Timbre says the weakest question again in the member's voice, and Prisma
hears the second attempt live while the candidate speaks, so the transcript is ready about a
second after they stop (Nov 6 measurement on generated speech: median 680 ms).

Both use the project's Gnani wire protocol (`gnani_protocol`), the same one the voice loop and the
probes use. Every socket is closed on every path; nothing is stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import time
import wave

from mockboard.config import Settings
from mockboard.speech import gnani_protocol as g

__all__ = ["PrismaRelay", "synthesize", "wav_bytes"]

MAX_TTS_CHARS = 400


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    """16-bit mono PCM wrapped as a WAV file the browser can play."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


async def synthesize(text: str, voice: str, settings: Settings, *, api_key: str,
                     url: str | None = None, timeout_s: float = 20.0) -> bytes:
    """One Timbre request on its own socket; the raw 16-bit PCM. Raises on an error or timeout.

    Stops at the first terminal message: the live server sends a second "complete" with no
    audio (Round 44), which must not be read as the end of a later request."""
    from websockets.asyncio.client import connect

    if not text.strip() or len(text) > MAX_TTS_CHARS:
        raise ValueError("text is empty or too long")
    req = g.TTSRequest(text=text, voice=voice, model=settings.tts_model,
                       language=settings.tts_language, sample_rate=settings.tts_sample_rate)
    message = req.ws_message()  # validates voice, model and rate before any network call
    pcm = bytearray()

    async def run() -> None:
        async with connect(url or settings.gnani_tts_ws_url,
                           additional_headers=g.build_tts_headers(api_key), max_size=None,
                           open_timeout=10) as ws:
            await ws.send(message)
            while True:
                chunk = g.parse_tts_ws_message(await ws.recv())
                if chunk.type == "error":
                    raise ConnectionError(str(chunk.raw.get("message", "speech service error")))
                if chunk.is_audio:
                    pcm.extend(g.strip_wav_header(chunk.audio))
                if chunk.is_terminal:
                    return

    await asyncio.wait_for(run(), timeout_s)
    if not pcm:
        raise ConnectionError("no audio came back")
    return bytes(pcm)


class PrismaRelay:
    """Live audio in, one final transcript out. `open`, then `push` as audio arrives, then
    `finish` (sends trailing silence, waits for the last transcript, closes). `close` is safe to
    call at any time and more than once."""

    def __init__(self, settings: Settings, *, lang_code: str, api_key: str,
                 url: str | None = None, idle_timeout_s: float = 3.0):
        self.settings = settings
        self.cfg = g.STTConfig.from_settings(settings, lang_code=lang_code)
        self.headers = g.build_stt_headers(self.cfg, api_key)
        self.url = url or settings.gnani_stt_url
        self.idle_timeout_s = idle_timeout_s
        self.frames = g.FrameBuffer()
        self.finals: list[str] = []
        self.audio_bytes = 0
        self._ws = None
        self._ctx = None
        self._receiver: asyncio.Task | None = None
        self._last_message = time.monotonic()
        self._last_final: float | None = None
        self._error: str | None = None

    async def open(self) -> None:
        from websockets.asyncio.client import connect

        self._ctx = connect(self.url, additional_headers=self.headers, max_size=None,
                            open_timeout=10)
        self._ws = await self._ctx.__aenter__()
        first = g.parse_stt_message(await asyncio.wait_for(self._ws.recv(), 10))
        if first.type == "error":
            await self.close()
            raise ConnectionError(str(first.raw.get("message", "speech service error")))
        self._receiver = asyncio.create_task(self._receive())

    async def _receive(self) -> None:
        assert self._ws is not None
        with contextlib.suppress(Exception):
            async for data in self._ws:
                msg = g.parse_stt_message(data)
                self._last_message = msg.arrived
                if msg.type == "error":
                    self._error = str(msg.raw.get("message", "speech service error"))
                elif msg.type == "transcript" and not msg.is_partial and msg.text.strip():
                    self.finals.append(msg.text.strip())
                    self._last_final = msg.arrived

    async def push(self, audio: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("relay is not open")
        self.audio_bytes += len(audio)
        for frame in self.frames.push(audio):
            await self._ws.send(frame)

    async def finish(self) -> tuple[str, int | None]:
        """Trailing silence (real-time paced, so the server closes the utterance), then wait
        until the server has been quiet for `idle_timeout_s`. Always closes the socket."""
        try:
            if self._ws is None:
                raise RuntimeError("relay is not open")
            last_audio = time.monotonic()
            tail = self.frames.flush()
            if tail is not None:
                await self._ws.send(tail)
            rate = self.cfg.sample_rate
            silence = list(g.iter_frames(b"\x00" * (int(g.TRAILING_SILENCE_S * rate) * 2)))
            start = time.monotonic()
            for i, deadline in g.pace_frames(len(silence), rate, start):
                delay = deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._ws.send(silence[i])
            self._last_message = max(self._last_message, time.monotonic())
            while time.monotonic() - self._last_message < self.idle_timeout_s:
                await asyncio.sleep(0.1)
            if self._error and not self.finals:
                raise ConnectionError(self._error)
            latency = None
            if self._last_final is not None:
                latency = max(0, round((self._last_final - last_audio) * 1000))
            return " ".join(self.finals).strip(), latency
        finally:
            await self.close()

    async def close(self) -> None:
        if self._receiver is not None:
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receiver
            self._receiver = None
        if self._ctx is not None:
            ctx, self._ctx, self._ws = self._ctx, None, None
            with contextlib.suppress(Exception):
                await ctx.__aexit__(None, None, None)
