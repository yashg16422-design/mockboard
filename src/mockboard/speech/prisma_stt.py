"""Our own Pipecat STT service for Gnani Prisma (ruling 24, voice chunk V2; ruling 27-31, V3).

Why not the stock `pipecat_gnani.GnaniSTTService` (spikes/NOTES.md, surprise 5): it forwards
each pipeline audio frame as it is (20 ms, 640 bytes from SmallWebRTC) although Gnani's docs
require exactly 1,024-byte frames, and it sends none of our VAD headers, `x-max-speech-s` or
word boosting. It also drops its socket reference on any `error` message, although the docs
say the session stays open after a single-utterance failure.

What this service does, built on the tested protocol code in `gnani_protocol.py`:

* opens `wss://api.vachana.ai/stt/v3/stream` (`GNANI_STT_URL`) with the headers that
  `build_stt_headers` makes from an `STTConfig` (current names, ruling 27: `language_code`,
  `format`, `model`, VAD values, `x-max-speech-s`, `x-idle-timeout-sec`, v3.0 `keyterms` and
  `topic`), plus `x-api-request-id` = `<prefix>-c<k>` for the k-th connection (ruling 31: ids
  never repeat across reconnects, so Gnani's `<request_id>-seg-<N>` segment ids stay unique);
* re-chunks incoming audio into exact 1,024-byte s16le frames (`FrameBuffer`) and sends them
  from one sender task at real-time cadence: one frame every 32 ms at 16 kHz, on absolute
  deadlines so jitter does not accumulate;
* `commit()` (rulings 28, 30, 31; CTO_NOTES_05 M3): flushes the framer, puts a commit marker
  into the SAME send queue as the audio, so it is sent only after every audio frame queued
  before it, and resolves when the transcripts it closes have arrived (the in-flight segments
  plus the committed utterance), or after a bounded wait. Transcripts are attributed to the
  commit by cause (Prisma's message order), never by a time window: each pushed
  `TranscriptionFrame` carries `result["mb"] = {"seq", "commit_id", "conn", "ordinal"}`;
* emits a `TranscriptionFrame` for every non-empty `transcript` message, and records every
  `speech_start` / `processing` message with its arrival time (`events`, `pending_segments`,
  `utterance_open`, the `on_processing` event and a `PrismaProcessingFrame` pushed in order
  with the transcripts);
* treats `error` as non-fatal (the docs: the session stays open after a failed utterance) and
  reconnects with backoff when the server closes the socket, except for a rejected key
  (HTTP 401) or a bad header (close code 1008), where retrying cannot help. Giving up fires
  `on_gave_up` so the voice app can tell the candidate (ruling 30). An idle close
  (`x-idle-timeout-sec`) is recorded as a normal `idle_close` event and simply reconnects;
* checks which model served: the docs say an unknown `model` is silently ignored, and v3.0
  adds `request_id` and `config.language_code` to `connected`, so a v3.0 request without
  them is logged loudly and `served_model` says so (ruling 30);
* rotates to a fresh socket before Prisma's 15-minute session cap, between user turns when
  possible (`rotate_after_s`) and unconditionally at `rotate_hard_s`;
* word boosting: v2.5 single words with a required non-zero score via the documented
  `update_settings` frame (`update_bias`); v3.0 `keyterms` via `set_keyterms`; both re-sent on
  every new socket;
* never logs the API key: headers are logged only through `redact_headers`.

Docs: https://docs.gnani.ai/api/STT/stt-websocket.md (fetched Oct 25, re-checked Oct 30, 2026).
Everything here was tested only against a fake server (tests/voice/test_prisma_stt.py).
"""

from __future__ import annotations

import asyncio
import contextlib
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
    COMMIT_FRAME,
    FRAME_BYTES,
    FrameBuffer,
    STTConfig,
    STTMessage,
    build_stt_headers,
    frame_duration_s,
    parse_stt_message,
    redact_headers,
    update_keyterms_frame,
    update_settings_frame,
    validate_bias_words,
    validate_keyterms,
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
class CommitResult:
    """What one `commit()` closed (rulings 28, 31).

    `reply` is "transcript" (the commit closed an utterance with words), "empty" (it closed
    one with no words), "none" (nothing was open), "error" (Prisma answered with `error`),
    "timeout" (the bounded wait ended first) or "not_sent" (no connection in time).
    `last_seq` is the `mb.seq` of the last pushed `TranscriptionFrame` that belongs to this
    commit (None when none was pushed): a consumer has every transcript of the closed turn once
    it has seen that seq."""

    commit_id: int
    reply: str
    texts: list[str]
    last_seq: int | None
    pending_at_commit: int
    open_at_commit: bool | None
    queued_at: float
    sent_at: float | None
    resolved_at: float

    @property
    def timed_out(self) -> bool:
        return self.reply in ("timeout", "not_sent")

    @property
    def wait_ms(self) -> float:
        return round((self.resolved_at - self.queued_at) * 1000, 1)


@dataclass(eq=False)
class _Commit:
    """A commit marker in the send queue, then the bookkeeping that resolves it."""

    commit_id: int
    queued_at: float
    future: asyncio.Future
    abandoned: bool = False  # the caller stopped waiting before it was sent: skip it
    conn: _Conn | None = None
    sent_at: float | None = None
    p0: int = 0  # conn.processing when sent
    t0: int = 0  # conn.transcripts when sent
    open: bool | None = None  # utterance open when sent; None: server never sent speech_start
    noop_s: float = 0.3
    noop_expired: bool = False
    error_after: bool = False
    closed_text: str | None = None  # text of the utterance the commit itself closed
    texts: list[str] = field(default_factory=list)
    last_seq: int | None = None
    resolved: bool = False

    def target(self) -> int | None:
        """How many transcripts on its connection this commit waits for; None: not known yet.

        A `processing` after the commit means it closed an utterance (one more transcript).
        With an utterance known to be open, that transcript is expected. With nothing open, an
        `error` reply, or no `processing` within the no-op window, only the in-flight segments
        are waited for. Never more than one past the commit: anything later is new speech."""
        assert self.conn is not None
        if self.conn.processing > self.p0:
            return self.p0 + 1
        if self.open is True and not self.error_after:
            return self.p0 + 1
        if self.open is False or self.error_after or self.noop_expired:
            return self.p0
        return None


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
    speech_start_seen: bool = False
    utterance_open: bool = False
    commits: list[_Commit] = field(default_factory=list)


class PrismaFatalError(RuntimeError):
    """A connection failure that retrying cannot fix (bad key or bad header)."""


class PrismaSTTService(STTService):
    """Gnani Prisma streaming STT with documented framing and our headers.

    Construct it from Settings (the normal path)::

        stt = PrismaSTTService.from_settings(get_settings(), min_silence_ms=300)

    or directly::

        stt = PrismaSTTService(api_key=key, config=STTConfig(lang_code="en-IN"))

    Extra events, besides STTService's on_connected / on_disconnected / on_connection_error:
    `on_processing(service, event)`, `on_prisma_error(service, message)` and
    `on_gave_up(service, message)` (no more reconnects: the voice app must tell the user).
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
        request_id_prefix: str | None = None,
        commit_wait_s: float = 1.5,
        commit_noop_s: float = 0.3,
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
        if bias_list:
            if cfg.is_v3:
                raise ValueError("prisma-v3.0 boosting is `keyterms` (STTConfig), not bias_list")
            validate_bias_words(bias_list)
            if not bias_score:
                # Docs: bias_list "Requires bias_score"; omitted or 0.0 disables boosting.
                raise ValueError("a bias list needs a non-zero bias_score on prisma-v2.5")
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
        self._request_id_prefix = request_id_prefix or f"mb_{uuid.uuid4().hex[:10]}"
        self._conn_counter = 0
        self._commit_wait_s = commit_wait_s
        self._commit_noop_s = commit_noop_s
        self._commit_counter = 0
        self._seq = 0  # pushed TranscriptionFrames
        self.served_model: str | None = None
        self.gave_up: str | None = None
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
        # One send queue for audio frames AND commit markers, so a commit is sent only after
        # every frame queued before it (CTO_NOTES_05 M3). `_audio_queued` counts frames only.
        self._items: deque[bytes | _Commit] = deque()
        self._items_ready = asyncio.Event()
        self._audio_queued = 0
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
            "commits": 0,
            "commit_timeouts": 0,
            "idle_closes": 0,
        }

        self._register_event_handler("on_processing")
        self._register_event_handler("on_prisma_error")
        self._register_event_handler("on_gave_up")

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
        min_speech_ms, min_segment_ms, max_speech_s, lang_code, keyterms, topic, ...). The
        commit waits come from STT_COMMIT_WAIT_MS / STT_COMMIT_NOOP_MS; a v2.5 bias list
        takes STT_BIAS_SCORE unless `bias_score` is given.
        """
        if api_key is None:
            secret = settings.gnani_api_key
            api_key = secret.get_secret_value().strip() if secret is not None else ""
        return cls(
            api_key=api_key,
            config=STTConfig.from_settings(settings, **config_overrides),
            url=settings.gnani_stt_url,
            bias_list=bias_list,
            bias_score=bias_score if bias_score is not None else (
                settings.stt_bias_score if bias_list else None
            ),
            commit_wait_s=settings.stt_commit_wait_ms / 1000,
            commit_noop_s=settings.stt_commit_noop_ms / 1000,
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

    @property
    def utterance_open(self) -> bool | None:
        """True after `speech_start` until its `processing`; None if this connection's server
        never sent `speech_start` (then nobody can tell). UNVERIFIED: that v2.5 sends it."""
        conn = self._conn
        if conn is None or not conn.speech_start_seen:
            return None
        return conn.utterance_open

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
            self._enqueue_audio(chunk)
        yield None

    def _enqueue_audio(self, chunk: bytes) -> None:
        if self._audio_queued >= self._max_backlog_frames:
            # Disconnected for too long: keep the newest audio, which can still matter for
            # the current turn, and count what we dropped. Commit markers are never dropped.
            for i, item in enumerate(self._items):
                if isinstance(item, bytes):
                    del self._items[i]
                    self._audio_queued -= 1
                    self.stats["frames_dropped"] += 1
                    break
        self._items.append(chunk)
        self._audio_queued += 1
        self._items_ready.set()
        self.stats["max_backlog_frames"] = max(
            self.stats["max_backlog_frames"], self._audio_queued
        )

    async def _peek(self) -> bytes | _Commit:
        while not self._items:
            self._items_ready.clear()
            await self._items_ready.wait()
        return self._items[0]

    def _pop(self) -> None:
        item = self._items.popleft()
        if isinstance(item, bytes):
            self._audio_queued -= 1

    async def _sender_loop(self):
        """Send queued frames at real-time cadence on absolute deadlines, and commit markers
        in queue order.

        Each frame is due one frame-duration (32 ms at 16 kHz) after the previous one. The
        first frame after a stall is held for `jitter_s`, so 20 ms input frames (which make a
        1,024-byte frame every 20 or 40 ms) leave at an even 32 ms. If audio piled up while
        disconnected, frames go out `catchup_factor` times faster until the backlog is gone,
        so the transcript lag does not stay. Docs: "Do not buffer and burst". A commit marker
        takes no frame slot; it goes out right after the last frame queued before it."""
        deadline: float | None = None
        while True:
            item = await self._peek()
            await self._connected.wait()
            conn = self._conn
            if conn is None:
                continue
            if isinstance(item, _Commit):
                if item.abandoned:
                    self._pop()
                    continue
                try:
                    await conn.ws.send(COMMIT_FRAME)
                except ConnectionClosed:
                    self._connected.clear()
                    continue
                self._pop()
                self._commit_sent(item, conn)
                continue
            now = time.monotonic()
            if deadline is None or now > deadline + self._dt:
                deadline = now + self._jitter_s  # first frame, or input stalled: re-anchor
            delay = deadline - now
            if delay > 0:
                await asyncio.sleep(delay)
            conn = self._conn
            if conn is None or not self._items or self._items[0] is not item:
                continue  # disconnected while waiting (keep the frame) or queue changed
            try:
                await conn.ws.send(item)
            except ConnectionClosed:
                # The receive task sees the close and reconnects; resend this frame after.
                self._connected.clear()
                continue
            self._pop()
            self.stats["frames_sent"] += 1
            catching_up = self._audio_queued >= self._catchup_frames
            deadline += self._dt / self._catchup_factor if catching_up else self._dt

    # ------------------------------------------------------------------ commit

    async def commit(
        self, *, wait_s: float | None = None, noop_s: float | None = None
    ) -> CommitResult:
        """Close the open utterance now (`{"type": "commit"}`) and wait, bounded, for the
        transcripts it closes: every segment already in flight plus the committed one.

        The framer's remainder is flushed first (padded with silence) and the marker joins the
        audio send queue, so it never overtakes speech queued before it. The wait is bounded
        by `wait_s` (default `commit_wait_s`), and with nothing open by `noop_s` (default
        `commit_noop_s`): Gnani does not document what `commit` does with nothing open
        (nothing, an empty transcript or an `error`), and all three end the wait.
        # UNVERIFIED: commit's behaviour against the real server (fake server only)."""
        wait_s = self._commit_wait_s if wait_s is None else wait_s
        self._commit_counter += 1
        self.stats["commits"] += 1
        loop = asyncio.get_running_loop()
        c = _Commit(self._commit_counter, time.monotonic(), loop.create_future())
        c.noop_s = self._commit_noop_s if noop_s is None else noop_s
        tail = self._framer.flush()
        if tail is not None:
            self._enqueue_audio(tail)
        self._items.append(c)
        self._items_ready.set()
        try:
            return await asyncio.wait_for(asyncio.shield(c.future), timeout=wait_s)
        except TimeoutError:
            self.stats["commit_timeouts"] += 1
            if c.sent_at is None:
                c.abandoned = True
                reply = "not_sent"
            else:
                reply = "timeout"  # stays registered: later transcripts still carry its id
            self._record("commit_timeout", c.conn.request_id if c.conn else "",
                         {"commit_id": c.commit_id, "reply": reply})
            return self._commit_result(c, reply)

    def _commit_result(self, c: _Commit, reply: str) -> CommitResult:
        return CommitResult(
            commit_id=c.commit_id,
            reply=reply,
            texts=list(c.texts),
            last_seq=c.last_seq,
            pending_at_commit=max(0, c.p0 - c.t0),
            open_at_commit=c.open,
            queued_at=c.queued_at,
            sent_at=c.sent_at,
            resolved_at=time.monotonic(),
        )

    def _commit_sent(self, c: _Commit, conn: _Conn) -> None:
        c.conn, c.sent_at = conn, time.monotonic()
        c.p0, c.t0 = conn.processing, conn.transcripts
        c.open = conn.utterance_open if conn.speech_start_seen else None
        conn.commits.append(c)
        self._record("commit_sent", conn.request_id, {
            "commit_id": c.commit_id, "pending": c.p0 - c.t0, "open": c.open,
        })
        asyncio.get_running_loop().call_later(c.noop_s, self._commit_noop_expired, c)
        self._check_commit(c)

    def _commit_noop_expired(self, c: _Commit) -> None:
        c.noop_expired = True
        self._check_commit(c)

    def _commit_for(self, conn: _Conn, ordinal: int) -> _Commit | None:
        """The commit that caused transcript number `ordinal` on `conn`, if any."""
        for c in conn.commits:
            limit = c.target()
            if limit is None:
                limit = c.p0
            if c.t0 < ordinal <= limit:
                return c
        return None

    def _check_commit(self, c: _Commit) -> None:
        if c.resolved or c.conn is None:
            return
        target = c.target()
        if target is None or c.conn.transcripts < target:
            return
        c.resolved = True
        if c in c.conn.commits:
            c.conn.commits.remove(c)
        if c.conn.processing > c.p0:
            reply = "transcript" if c.closed_text else "empty"
        elif c.error_after:
            reply = "error"
        else:
            reply = "none"
        result = self._commit_result(c, reply)
        self._record("commit_resolved", c.conn.request_id, {
            "commit_id": c.commit_id, "reply": reply, "wait_ms": result.wait_ms,
        })
        if not c.future.done():
            c.future.set_result(result)

    # ------------------------------------------------------------------ connections

    async def _open_current(self) -> None:
        conn = await self._connect_once()
        self._conn = conn
        self._connected.set()

    async def _connect_once(self) -> _Conn:
        self._conn_counter += 1
        request_id = f"{self._request_id_prefix}-c{self._conn_counter}"
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
        else:
            self.served_model = self._served_model(msg.raw, request_id)

        conn = _Conn(ws=ws, request_id=request_id, opened_at=time.monotonic())
        self._conns.append(conn)
        self.stats["connects"] += 1
        conn.recv_task = self.create_task(self._receive_loop(conn), name="prisma-recv")
        if self._bias_list:
            await ws.send(update_settings_frame(self._bias_list, self._bias_score))
        logger.info(f"[prisma] connected request_id={request_id}")
        await self._call_event_handler("on_connected")
        return conn

    def _served_model(self, connected: dict[str, Any], request_id: str) -> str:
        """Which model the server says it is running (ruling 30). The docs: an unknown `model`
        is silently ignored, and v3.0's `connected` adds `request_id` and
        `config.language_code`. # UNVERIFIED: that v2.5 never sends those fields."""
        config = connected.get("config") if isinstance(connected.get("config"), dict) else {}
        v3_fields = "request_id" in connected or "language_code" in config
        if self._cfg.is_v3 and not v3_fields:
            served = "unconfirmed: prisma-v3.0 requested, `connected` lacks v3.0 fields"
            logger.error(f"[prisma] {served}; the server may be running prisma-v2.5")
        elif self._cfg.is_v3:
            served = "prisma-v3.0"
        else:
            served = "prisma-v2.5"
        self._record("model_check", request_id, {"requested": self._cfg.model, "served": served})
        return served

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
        close_reason = getattr(conn.ws, "close_reason", None)
        if close_reason:
            reason = f"{reason} ({close_reason})"
        await self._on_closed(conn, getattr(conn.ws, "close_code", None), reason)

    async def _handle_message(self, conn: _Conn, msg: STTMessage) -> None:
        event = self._record(msg.type, conn.request_id, msg.raw, arrived=msg.arrived)
        if msg.type == "speech_start":
            conn.speech_start_seen = True
            conn.utterance_open = True
        elif msg.type == "processing":
            conn.processing += 1
            conn.utterance_open = False
            await self._call_event_handler("on_processing", event)
            if self._push_processing_frames:
                await self.push_frame(
                    PrismaProcessingFrame(
                        request_id=conn.request_id, arrived=msg.arrived, raw=msg.raw
                    )
                )
            for c in list(conn.commits):
                self._check_commit(c)
        elif msg.type == "transcript":
            conn.transcripts += 1
            ordinal = conn.transcripts
            text = msg.text.strip()
            commit = self._commit_for(conn, ordinal)
            seq = None
            if text:
                self._seq += 1
                seq = self._seq
                await self._push_transcript(text, msg, {
                    "seq": seq,
                    "commit_id": commit.commit_id if commit else None,
                    "conn": conn.request_id,
                    "ordinal": ordinal,
                })
            if commit is not None:
                if text:
                    commit.texts.append(text)
                    commit.last_seq = seq
                if ordinal == commit.p0 + 1:
                    commit.closed_text = text
            for c in list(conn.commits):
                self._check_commit(c)
        elif msg.type == "error":
            # Docs: "Failure on a single utterance" leaves the session open, so the socket
            # stays; if the server closes it, `_on_closed` decides about reconnecting. An
            # error right after a commit is one of its three possible replies.
            for c in conn.commits:
                if c.sent_at is not None and conn.processing == c.p0:
                    c.error_after = True
            for c in list(conn.commits):
                self._check_commit(c)
            await self._report_prisma_error(str(msg.raw.get("message", "unknown error")))
        # connected / settings_updated / anything new: recorded only.

    async def _push_transcript(self, text: str, msg: STTMessage, mb: dict[str, Any]) -> None:
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
                result={**msg.raw, "mb": mb},
            )
        )

    async def _report_prisma_error(self, message: str) -> None:
        self.stats["errors"] += 1
        logger.warning(f"[prisma] error message from server: {message}")
        await self._call_event_handler("on_prisma_error", message)
        await self.push_error(f"Gnani Prisma: {message}")

    async def _on_closed(self, conn: _Conn, code: int | None, reason: str) -> None:
        self._record("closed", conn.request_id, {"code": code, "reason": reason})
        idle = "idle" in reason.lower() or "idle" in self._last_error_text(conn).lower()
        if idle:
            # x-idle-timeout-sec ran out: a normal event, not a failure (ruling 30).
            # UNVERIFIED: how the real server words or codes an idle close.
            self.stats["idle_closes"] += 1
            self._record("idle_close", conn.request_id, {"code": code})
        for c in list(conn.commits):  # a commit on a dead socket can no longer resolve
            c.noop_expired = c.error_after = True
            self._check_commit(c)
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

    def _last_error_text(self, conn: _Conn) -> str:
        for event in reversed(self.events):
            if event.request_id == conn.request_id and event.kind == "error":
                return str(event.raw.get("message", ""))
        return ""

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
        self.gave_up = message
        self._connected.clear()
        logger.error(f"[prisma] {message}")
        await self._call_event_handler("on_connection_error", message)
        await self._call_event_handler("on_gave_up", message)
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
        for `rotation_grace_s` so its last transcript still arrives. This commit goes straight
        to the old socket: rotation happens between user turns, and audio still queued then
        belongs on the new socket (CTO_NOTES_05 M3 concerns turn-end commits, `commit()`).
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
                await old.ws.send(COMMIT_FRAME)
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
        """v2.5 word boosting via the documented `update_settings` frame. Applies from the next
        segment; the server answers `settings_updated` (recorded in `events`). A list needs a
        non-zero score (docs), given here or earlier."""
        if self._cfg.is_v3:
            raise ValueError("prisma-v3.0 boosting is keyterms: use set_keyterms()")
        if words and not (score or self._bias_score):
            raise ValueError("a bias list needs a non-zero bias_score on prisma-v2.5")
        message = update_settings_frame(words, score)  # validates
        if words is not None:
            self._bias_list = list(words)
        if score is not None:
            self._bias_score = score
        if self._conn is not None:
            await self._conn.ws.send(message)

    async def set_keyterms(self, terms: list[str]) -> bool:
        """Prisma v3.0: replace the keyterms (per interview topic). On a live socket they go
        in an `update_settings` frame (docs: v3.0 keyterms change "through the `bias` key");
        the config is updated too, so a reconnect sends them as the `keyterms` header.
        Returns False (and does nothing) on v2.5, which has no keyterms."""
        if not self._cfg.is_v3:
            return False
        self._cfg.keyterms = validate_keyterms(terms)
        if self._conn is not None:
            with contextlib.suppress(ConnectionClosed):
                await self._conn.ws.send(update_keyterms_frame(self._cfg.keyterms))
        return True

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
