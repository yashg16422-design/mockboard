"""Our own Pipecat STT service for Gnani Prisma (ruling 24, voice chunk V2).

Why not the stock `pipecat_gnani.GnaniSTTService` (spikes/NOTES.md, surprise 5): it forwards
each pipeline audio frame as it is (20 ms, 640 bytes from SmallWebRTC) although Gnani's docs
require exactly 1,024-byte frames, and it sends none of our VAD headers, `x-max-speech-s` or
word boosting. It also drops its socket reference on any `error` message, although the docs
say the session stays open after a single-utterance failure.

What this service does, built on the tested protocol code in `gnani_protocol.py`:

* opens `wss://api.vachana.ai/stt/v3/stream` (`GNANI_STT_URL`) with the headers that
  `build_stt_headers` makes from an `STTConfig` (lang_code, x-sample-rate, x-format, VAD
  values, x-max-speech-s), plus `x-api-request-id`;
* re-chunks incoming audio into exact 1,024-byte s16le frames (`FrameBuffer`) and sends them
  from one sender task at real-time cadence: one frame every 32 ms at 16 kHz, on absolute
  deadlines so jitter does not accumulate;
* emits a `TranscriptionFrame` for every `transcript` message (Prisma's JSON in `result`), and
  records every `processing` message with its arrival time (`events`, `pending_segments`,
  the `on_processing` event and a `PrismaProcessingFrame` pushed in order with the
  transcripts) for V3's turn gating (ruling 26);
* treats `error` as non-fatal (the docs: the session stays open after a failed utterance) and
  reconnects with backoff when the server closes the socket, except for a rejected key
  (HTTP 401) or a bad header (close code 1008), where retrying cannot help;
* rotates to a fresh socket before Prisma's 15-minute session cap, between user turns when
  possible (`rotate_after_s`) and unconditionally at `rotate_hard_s`;
* sends word boosting with the documented `update_settings` frame (`update_bias`), and
  re-sends it on every new socket;
* never logs the API key: headers are logged only through `redact_headers`.

Docs: https://docs.gnani.ai/api/STT/stt-websocket.md (fetched Oct 25, re-checked Oct 30, 2026).
Everything here was tested only against a fake server (tests/voice/test_prisma_stt.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    ControlFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.services.settings import STTSettings, is_given
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from mockboard.config import Settings
from mockboard.speech.gnani_protocol import (
    FRAME_BYTES,
    FrameBuffer,
    STTConfig,
    STTMessage,
    build_stt_headers,
    frame_duration_s,
    parse_stt_message,
    redact_headers,
    update_settings_frame,
    validate_bias_words,
)

# Docs: "The overall session is always capped at 15 minutes"; at the cap the server sends an
# `error` and closes with 1001. We rotate well before it.
PRISMA_SESSION_CAP_S = 15 * 60
CLOSE_POLICY_VIOLATION = 1008  # docs: bad handshake header (an `error` message comes first)
CLOSE_GOING_AWAY = 1001  # docs: session exceeded 15 minutes

ConnectFn = Callable[..., Awaitable[Any]]


@dataclass
class PrismaProcessingFrame(ControlFrame):
    """Prisma detected the end of a speech segment; its transcript is still being computed.

    Pushed downstream in order with the `TranscriptionFrame`s, so a later processor (V3's
    BrainProcessor) can tell that a segment is in flight when the user turn stops."""

    request_id: str = ""
    arrived: float = 0.0  # time.monotonic() on arrival
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrismaEvent:
    """One server message (or connection event), stamped on arrival. Carries no secrets."""

    kind: str  # connected | processing | transcript | error | settings_updated | closed | ...
    arrived: float  # time.monotonic()
    wall: str  # ISO-8601, for logs
    request_id: str
    raw: dict[str, Any]

    @property
    def segment_index(self) -> int | None:
        value = self.raw.get("segment_index")
        return value if isinstance(value, int) else None


@dataclass
class _Conn:
    """One Prisma WebSocket. A rotation briefly has two: the new one takes audio while the
    old one finishes its last segment."""

    ws: Any
    request_id: str
    opened_at: float
    processing: int = 0
    transcripts: int = 0
    recv_task: asyncio.Task | None = None
    retired: bool = False


class PrismaFatalError(RuntimeError):
    """A connection failure that retrying cannot fix (bad key or bad header)."""


class PrismaSTTService(STTService):
    """Gnani Prisma streaming STT with documented framing and our headers.

    Construct it from Settings (the normal path)::

        stt = PrismaSTTService.from_settings(get_settings(), min_silence_ms=300)

    or directly::

        stt = PrismaSTTService(api_key=key, config=STTConfig(lang_code="en-IN"))

    Extra events, besides STTService's on_connected / on_disconnected / on_connection_error:
    `on_processing(service, event)` and `on_prisma_error(service, message)`.
    """

    def __init__(
        self,
        *,
        api_key: str,
        config: STTConfig | None = None,
        url: str = "wss://api.vachana.ai/stt/v3/stream",
        ttfs_p99_latency: float | None = None,
        bias_list: list[str] | None = None,
        bias_score: float | None = None,
        push_processing_frames: bool = True,
        rotate_after_s: float = 12 * 60,
        rotate_hard_s: float = 14 * 60,
        rotation_grace_s: float = 3.0,
        rotation_check_s: float = 1.0,
        connect_timeout_s: float = 10.0,
        reconnect_backoff_s: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0),
        jitter_s: float = 0.02,
        max_backlog_s: float = 2.0,
        catchup_backlog_frames: int = 3,
        catchup_factor: float = 2.0,
        connect: ConnectFn | None = None,
        **kwargs,
    ):
        cfg = config or STTConfig()
        if not api_key:
            raise ValueError("PrismaSTTService needs a Gnani API key")
        if cfg.partials:
            # UNVERIFIED: `x-partials` is undocumented; only the probe may turn it on.
            raise ValueError("partials are probe-only until FEATURE_PRISMA_PARTIALS has an ADR")
        # Fail at construction, not at connect time, on values outside the documented ranges.
        build_stt_headers(cfg, "validation-only")
        if bias_list is not None:
            validate_bias_words(bias_list)
            update_settings_frame(bias_list, bias_score)  # validates the score too
        if not 0 < rotate_after_s <= rotate_hard_s < PRISMA_SESSION_CAP_S:
            raise ValueError("need 0 < rotate_after_s <= rotate_hard_s < 900 (Prisma's cap)")

        super().__init__(
            sample_rate=cfg.sample_rate,
            ttfs_p99_latency=ttfs_p99_latency,
            settings=STTSettings(model=None, language=cfg.lang_code),
            **kwargs,
        )
        self._api_key = api_key
        self._cfg = cfg
        self._url = url
        self._connect_fn: ConnectFn = connect or websocket_connect
        self._bias_list = list(bias_list) if bias_list is not None else None
        self._bias_score = bias_score
        self._push_processing_frames = push_processing_frames
        self._rotate_after_s = rotate_after_s
        self._rotate_hard_s = rotate_hard_s
        self._rotation_grace_s = rotation_grace_s
        self._rotation_check_s = rotation_check_s
        self._connect_timeout_s = connect_timeout_s
        self._backoff = reconnect_backoff_s
        self._dt = frame_duration_s(cfg.sample_rate)
        self._jitter_s = jitter_s
        self._max_backlog_frames = max(1, int(max_backlog_s / self._dt))
        self._catchup_frames = catchup_backlog_frames
        self._catchup_factor = catchup_factor

        self._framer = FrameBuffer(FRAME_BYTES)
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._conn: _Conn | None = None
        self._conns: list[_Conn] = []  # every socket still open, current one included
        self._connected = asyncio.Event()
        self._closing = False
        self._fatal = False
        self._sender_task: asyncio.Task | None = None
        self._rotation_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._aux_tasks: set[asyncio.Task] = set()

        self.events: deque[PrismaEvent] = deque(maxlen=1024)
        self.stats: dict[str, int] = {
            "frames_sent": 0,
            "frames_dropped": 0,
            "max_backlog_frames": 0,
            "connects": 0,
            "reconnects": 0,
            "rotations": 0,
            "errors": 0,
        }

        self._register_event_handler("on_processing")
        self._register_event_handler("on_prisma_error")

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        api_key: str | None = None,
        bias_list: list[str] | None = None,
        bias_score: float | None = None,
        service_kwargs: dict[str, Any] | None = None,
        **config_overrides: Any,
    ) -> PrismaSTTService:
        """Build from the app's Settings (GNANI_API_KEY, GNANI_STT_URL, STT_*).

        `config_overrides` go to `STTConfig.from_settings` (vad_threshold, min_silence_ms,
        min_speech_ms, min_segment_ms, max_speech_s, lang_code, ...). The VAD values have no
        Settings fields yet, so they stay at Prisma's defaults unless passed here.
        """
        if api_key is None:
            secret = settings.gnani_api_key
            api_key = secret.get_secret_value().strip() if secret is not None else ""
        return cls(
            api_key=api_key,
            config=STTConfig.from_settings(settings, **config_overrides),
            url=settings.gnani_stt_url,
            bias_list=bias_list,
            bias_score=bias_score,
            **(service_kwargs or {}),
        )

    @property
    def config(self) -> STTConfig:
        return self._cfg

    @property
    def request_id(self) -> str | None:
        return self._conn.request_id if self._conn else None

    @property
    def pending_segments(self) -> int:
        """Segments Prisma has closed (`processing`) whose `transcript` has not arrived yet.

        # UNVERIFIED: that every `processing` is followed by exactly one `transcript`. A
        # segment shorter than x-min-segment-ms may be dropped after `processing`; V3 must
        # bound any wait on this count."""
        return sum(max(0, c.processing - c.transcripts) for c in self._conns)

    def last_event(self, kind: str) -> PrismaEvent | None:
        for event in reversed(self.events):
            if event.kind == kind:
                return event
        return None

    def can_generate_metrics(self) -> bool:
        return True

    def language_to_service_language(self, language: Language) -> str | None:
        return str(language.value) if isinstance(language, Language) else str(language)

    # ------------------------------------------------------------------ lifecycle

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if frame.audio_in_sample_rate and frame.audio_in_sample_rate != self._cfg.sample_rate:
            # We send the transport's PCM as it is; a mismatch would mean wrong-speed audio.
            await self.push_error(
                f"Prisma is configured for {self._cfg.sample_rate} Hz but the pipeline's input "
                f"is {frame.audio_in_sample_rate} Hz; set PipelineParams.audio_in_sample_rate",
                fatal=True,
            )
            return
        self._closing = False
        self._sender_task = self.create_task(self._sender_loop(), name="prisma-sender")
        self._rotation_task = self.create_task(self._rotation_loop(), name="prisma-rotation")
        try:
            await self._open_current()
        except Exception as e:  # reported inside; keep the pipeline alive
            if not self._fatal:
                self._schedule_reconnect(f"initial connect failed: {e}")

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._teardown()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._teardown()

    async def cleanup(self):
        await super().cleanup()
        await self._teardown()

    async def _teardown(self):
        if self._closing:
            return
        self._closing = True
        self._connected.clear()
        for task in (self._sender_task, self._rotation_task, self._reconnect_task):
            if task is not None:
                await self.cancel_task(task)
        self._sender_task = self._rotation_task = self._reconnect_task = None
        for task in list(self._aux_tasks):
            await self.cancel_task(task)
        for conn in list(self._conns):
            await self._close_conn(conn)
        self._conn = None
        self._framer.clear()

    # ------------------------------------------------------------------ audio in

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Re-chunk into 1,024-byte frames and queue them for the paced sender."""
        for chunk in self._framer.push(audio):
            if self._queue.qsize() >= self._max_backlog_frames:
                # Disconnected for too long: keep the newest audio, which can still matter
                # for the current turn, and count what we dropped.
                self._queue.get_nowait()
                self.stats["frames_dropped"] += 1
            self._queue.put_nowait(chunk)
            self.stats["max_backlog_frames"] = max(
                self.stats["max_backlog_frames"], self._queue.qsize()
            )
        yield None

    async def _sender_loop(self):
        """Send queued frames at real-time cadence on absolute deadlines.

        Each frame is due one frame-duration (32 ms at 16 kHz) after the previous one. The
        first frame after a stall is held for `jitter_s`, so 20 ms input frames (which make a
        1,024-byte frame every 20 or 40 ms) leave at an even 32 ms. If audio piled up while
        disconnected, frames go out `catchup_factor` times faster until the backlog is gone,
        so the transcript lag does not stay. Docs: "Do not buffer and burst"."""
        deadline: float | None = None
        pending: bytes | None = None
        while True:
            if pending is None:
                pending = await self._queue.get()
            await self._connected.wait()
            now = time.monotonic()
            if deadline is None or now > deadline + self._dt:
                deadline = now + self._jitter_s  # first frame, or input stalled: re-anchor
            delay = deadline - now
            if delay > 0:
                await asyncio.sleep(delay)
            conn = self._conn
            if conn is None:
                continue  # disconnected while waiting; keep `pending`
            try:
                await conn.ws.send(pending)
            except ConnectionClosed:
                # The receive task sees the close and reconnects; resend this frame after.
                self._connected.clear()
                continue
            pending = None
            self.stats["frames_sent"] += 1
            catching_up = self._queue.qsize() >= self._catchup_frames
            deadline += self._dt / self._catchup_factor if catching_up else self._dt

    # ------------------------------------------------------------------ connections

    async def _open_current(self) -> None:
        conn = await self._connect_once()
        self._conn = conn
        self._connected.set()

    async def _connect_once(self) -> _Conn:
        request_id = f"mb_{uuid.uuid4().hex[:12]}"
        headers = build_stt_headers(self._cfg, self._api_key)
        headers["x-api-request-id"] = request_id
        logger.debug(f"[prisma] connect {self._url} headers={redact_headers(headers)}")
        try:
            ws = await asyncio.wait_for(
                self._connect_fn(
                    self._url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ),
                timeout=self._connect_timeout_s,
            )
        except InvalidStatus as e:
            status = e.response.status_code
            self._record("connect_rejected", request_id, {"status": status})
            if status in (401, 403):
                await self._fail_fatally(f"Prisma rejected the API key (HTTP {status})")
            raise
        except Exception as e:
            self._record("connect_failed", request_id, {"error": type(e).__name__})
            await self._call_event_handler("on_connection_error", f"{type(e).__name__}: {e}")
            raise

        try:
            first = await asyncio.wait_for(ws.recv(), timeout=self._connect_timeout_s)
        except Exception:
            await _quiet_close(ws)
            raise
        msg = parse_stt_message(first)
        self._record(msg.type, request_id, msg.raw, arrived=msg.arrived)
        if msg.type == "error":
            # Docs: a bad header (then close 1008) or the concurrent-session cap is reported
            # this way. A bad header will fail again, so only the cap is worth retrying.
            text = str(msg.raw.get("message", "unknown error"))
            await self._report_prisma_error(text)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.wait_closed(), timeout=1.0)
            code = getattr(ws, "close_code", None)
            await _quiet_close(ws)
            if code == CLOSE_POLICY_VIOLATION:
                await self._fail_fatally(f"Prisma refused the headers (1008): {text}")
                raise PrismaFatalError(text)
            raise ConnectionError(f"Prisma refused the session: {text}")
        if msg.type != "connected":
            logger.warning(f"[prisma] unexpected first message type {msg.type!r}")

        conn = _Conn(ws=ws, request_id=request_id, opened_at=time.monotonic())
        self._conns.append(conn)
        self.stats["connects"] += 1
        conn.recv_task = self.create_task(self._receive_loop(conn), name="prisma-recv")
        if self._bias_list is not None or self._bias_score is not None:
            await ws.send(update_settings_frame(self._bias_list, self._bias_score))
        logger.info(f"[prisma] connected request_id={request_id}")
        await self._call_event_handler("on_connected")
        return conn

    async def _receive_loop(self, conn: _Conn) -> None:
        reason = "server closed"
        try:
            async for data in conn.ws:
                await self._handle_message(conn, parse_stt_message(data))
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as e:
            reason = f"connection closed: {e}"
        except Exception as e:
            reason = f"receive error: {type(e).__name__}: {e}"
            logger.warning(f"[prisma] {reason}")
        await self._on_closed(conn, getattr(conn.ws, "close_code", None), reason)

    async def _handle_message(self, conn: _Conn, msg: STTMessage) -> None:
        event = self._record(msg.type, conn.request_id, msg.raw, arrived=msg.arrived)
        if msg.type == "processing":
            conn.processing += 1
            await self._call_event_handler("on_processing", event)
            if self._push_processing_frames:
                await self.push_frame(
                    PrismaProcessingFrame(
                        request_id=conn.request_id, arrived=msg.arrived, raw=msg.raw
                    )
                )
        elif msg.type == "transcript":
            conn.transcripts += 1
            text = msg.text.strip()
            if text:
                await self._push_transcript(text, msg)
        elif msg.type == "error":
            # Docs: "Failure on a single utterance" leaves the session open, so the socket
            # stays; if the server closes it, `_on_closed` decides about reconnecting.
            await self._report_prisma_error(str(msg.raw.get("message", "unknown error")))
        # connected / settings_updated / speech_start / anything new: recorded only.

    async def _push_transcript(self, text: str, msg: STTMessage) -> None:
        language: Language | None
        try:
            language = Language(self._cfg.lang_code)
        except ValueError:
            language = None
        frame_cls = InterimTranscriptionFrame if msg.is_partial else TranscriptionFrame
        await self.push_frame(
            frame_cls(
                text=text,
                user_id=self._user_id,
                timestamp=time_now_iso8601(),
                language=language,
                result=msg.raw,
            )
        )

    async def _report_prisma_error(self, message: str) -> None:
        self.stats["errors"] += 1
        logger.warning(f"[prisma] error message from server: {message}")
        await self._call_event_handler("on_prisma_error", message)
        await self.push_error(f"Gnani Prisma: {message}")

    async def _on_closed(self, conn: _Conn, code: int | None, reason: str) -> None:
        self._record("closed", conn.request_id, {"code": code, "reason": reason})
        if conn in self._conns:
            self._conns.remove(conn)
        if conn is not self._conn or self._closing:
            return  # a retired socket after rotation, or our own shutdown
        self._conn = None
        self._connected.clear()
        await self._call_event_handler("on_disconnected")
        if code == CLOSE_POLICY_VIOLATION:
            await self._fail_fatally(f"Prisma closed with 1008 (bad handshake header): {reason}")
            return
        self._schedule_reconnect(f"code={code} {reason}")

    def _schedule_reconnect(self, why: str) -> None:
        if self._closing or self._fatal:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        logger.info(f"[prisma] reconnecting ({why})")
        self._reconnect_task = self.create_task(self._reconnect_loop(), name="prisma-reconnect")

    async def _reconnect_loop(self) -> None:
        for attempt, delay in enumerate(self._backoff, start=1):
            if self._closing or self._fatal:
                return
            await asyncio.sleep(delay)
            try:
                await self._open_current()
            except PrismaFatalError:
                return
            except Exception as e:
                if self._fatal:
                    return
                logger.warning(f"[prisma] reconnect attempt {attempt} failed: {type(e).__name__}")
                continue
            self.stats["reconnects"] += 1
            return
        if not self._fatal:
            await self._fail_fatally(f"Prisma reconnect failed {len(self._backoff)} times")

    async def _fail_fatally(self, message: str) -> None:
        """Stop retrying. The error is not pipeline-fatal: the session (V3) decides."""
        self._fatal = True
        self._connected.clear()
        logger.error(f"[prisma] {message}")
        await self._call_event_handler("on_connection_error", message)
        await self.push_error(f"Gnani Prisma: {message}")

    # ------------------------------------------------------------------ rotation

    async def _rotation_loop(self) -> None:
        while True:
            await asyncio.sleep(self._rotation_check_s)
            conn = self._conn
            if conn is None:
                continue
            age = time.monotonic() - conn.opened_at
            due = age >= self._rotate_hard_s or (
                age >= self._rotate_after_s and not self._user_speaking
            )
            if due:
                await self.rotate()

    async def rotate(self) -> bool:
        """Open a fresh socket, move audio to it, then retire the old one.

        The old socket gets a `commit` so its open utterance is closed at once, and stays open
        for `rotation_grace_s` so its last transcript still arrives.
        # UNVERIFIED: that two sockets may be open at once for one key (the docs mention a
        # concurrent-session cap without a number) and that `commit` (added to the docs after
        # Oct 25, 2026) is accepted.
        """
        old = self._conn
        try:
            new = await self._connect_once()
        except Exception as e:
            logger.warning(f"[prisma] rotation connect failed ({type(e).__name__}); retrying")
            return False
        self._conn = new
        self._connected.set()
        self.stats["rotations"] += 1
        if old is not None:
            old.retired = True
            with contextlib.suppress(Exception):
                await old.ws.send(json.dumps({"type": "commit"}))
            task = self.create_task(self._retire_later(old), name="prisma-retire")
            self._aux_tasks.add(task)
            task.add_done_callback(self._aux_tasks.discard)
        return True

    async def _retire_later(self, conn: _Conn) -> None:
        await asyncio.sleep(self._rotation_grace_s)
        await self._close_conn(conn)

    async def _close_conn(self, conn: _Conn) -> None:
        if conn.recv_task is not None and conn.recv_task is not asyncio.current_task():
            await self.cancel_task(conn.recv_task)
        conn.recv_task = None
        await _quiet_close(conn.ws)
        if conn in self._conns:
            self._conns.remove(conn)
            self._record("closed", conn.request_id, {"code": None, "reason": "closed by us"})

    # ------------------------------------------------------------------ settings

    async def update_bias(self, words: list[str] | None, score: float | None = None) -> None:
        """Word boosting via the documented `update_settings` frame. Applies from the next
        segment; the server answers `settings_updated` (recorded in `events`)."""
        message = update_settings_frame(words, score)  # validates
        if words is not None:
            self._bias_list = list(words)
        if score is not None:
            self._bias_score = score
        if self._conn is not None:
            await self._conn.ws.send(message)

    async def _update_settings(self, delta: STTSettings) -> dict[str, Any]:
        changed = await super()._update_settings(delta)
        if "language" in changed and is_given(self._settings.language):
            self._cfg.lang_code = str(self._settings.language)
            if self._conn is not None:
                await self.rotate()  # lang_code is a connect-time header
        return changed

    # ------------------------------------------------------------------ helpers

    def _record(
        self, kind: str, request_id: str, raw: dict[str, Any], *, arrived: float | None = None
    ) -> PrismaEvent:
        event = PrismaEvent(
            kind=kind,
            arrived=time.monotonic() if arrived is None else arrived,
            wall=time_now_iso8601(),
            request_id=request_id,
            raw=raw,
        )
        self.events.append(event)
        return event


async def _quiet_close(ws: Any) -> None:
    with contextlib.suppress(Exception):
        await ws.close()
