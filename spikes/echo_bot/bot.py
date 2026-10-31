"""V1 echo-bot spike: browser mic -> Silero -> Prisma -> Smart Turn v3 -> echo -> Timbre.

THROWAWAY CODE (CTO_NOTES_03 §3, V1). Its only job is to show how pipecat-ai 1.5.0 really
behaves with the Gnani plugin, so the real voice loop (V2-V4) is built on facts. Nothing in
`src/` imports this file.

The pipeline, in order (Pipecat 1.5.0 names, read from the installed source):

    SmallWebRTCTransport.input()      browser microphone, 20 ms frames of 16 kHz PCM
    VADProcessor(SileroVADAnalyzer)   pushes VADUserStartedSpeakingFrame and
                                      VADUserStoppedSpeakingFrame
    PrismaSTTService (Prisma)         ours (mockboard.speech, V2): 1,024-byte frames, our
                                      headers; pushes TranscriptionFrame for each final
    UserTurnProcessor                 Smart Turn v3 decides the user turn is over; it broadcasts
                                      UserStoppedSpeakingFrame and fires `on_user_turn_stopped`
    EchoProcessor                     ours: on turn stop, says the turn's transcript back
    TimbreTTSService (Timbre)         ours (V2): the plugin plus the barge-in fix; turns
                                      TTSSpeakFrame text into TTSAudioRawFrame audio
    SmallWebRTCTransport.output()     back to the browser

Every frame that moves between two processors is written to a JSONL log with timestamps, so we
learn the real event order (FrameLogObserver below).

This module has no web-server code (that is `server.py`), and it imports the Gnani plugin only
inside `make_gnani_services()`, so the offline smoke test can build the pipeline with fake
speech services and no network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    AudioRawFrame,
    ControlFrame,
    Frame,
    InterimTranscriptionFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

SAMPLE_RATE = 16000  # Prisma and Timbre both take 16 kHz; the probes used it too.
AUDIO_BURST_GAP_S = 0.25  # an audio frame after this much quiet on a hop starts a new "burst"


# ---------------------------------------------------------------------------
# Settings for one run (filled from the command line in server.py)
# ---------------------------------------------------------------------------


@dataclass
class EchoConfig:
    api_key: str = field(default="", repr=False)  # never in a repr or log (CTO_NOTES_05 s-f 11)
    stt_language: str = "en-IN"
    stt_format: str = "verbatim"
    tts_voice: str = "Poorvi"
    tts_model: str = "timbre-v2.5"
    tts_language: str | None = "auto"  # TTS_LANGUAGE's default (ruling 25); None: omit it
    stt_url: str = "wss://api.vachana.ai/stt/v3/stream"
    stt_max_speech_s: float | None = 15.0
    stt_min_silence_ms: int | None = None  # None: Prisma's default (500 ms)
    stt_vad_threshold: float | None = None  # None: Prisma's default (0.7)
    stock_stt: bool = False  # True: the plugin's GnaniSTTService (640-byte frames, no headers)
    stock_tts: bool = False  # True: the plugin's GnaniTTSService (leaky interruption)
    vad_start_only: bool = False  # True: only Silero may start a user turn, not a transcript
    log_dir: Path = Path("spikes/echo_bot/logs")
    log_audio_frames: bool = False  # True: log every audio frame, not only burst starts
    fake_speech: bool = False  # True: FakeSTT/FakeTTS (beeps), no Gnani key or credits


# ---------------------------------------------------------------------------
# JSONL frame log
# ---------------------------------------------------------------------------


class JsonlLog:
    """Append-only JSONL file. Each line has wall time, monotonic ms since start, and a kind."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = path.open("a", encoding="utf-8")
        self._t0 = time.monotonic()

    def write(self, kind: str, **fields: Any) -> None:
        now = time.time()
        line = {
            "wall": time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}",
            "ms": round((time.monotonic() - self._t0) * 1000, 1),
            "kind": kind,
            **fields,
        }
        self._f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def _frame_extras(frame: Frame) -> dict[str, Any]:
    """The few fields worth logging for a frame, beyond its type."""
    extras: dict[str, Any] = {}
    if isinstance(frame, TextFrame):
        extras["text"] = frame.text
    if isinstance(frame, TTSSpeakFrame):
        extras["text"] = frame.text
    if isinstance(frame, TranscriptionFrame | InterimTranscriptionFrame):
        extras["finalized"] = getattr(frame, "finalized", None)
        extras["language"] = str(frame.language) if frame.language else None
        if isinstance(frame.result, dict):
            # Prisma's own message (latency, audio_duration_ms, ...). It carries no secrets.
            extras["stt_result"] = frame.result
    for name in ("start_secs", "stop_secs", "ttfs_p99_latency", "context_id", "error"):
        value = getattr(frame, name, None)
        if value is not None:
            extras[name] = value
    if isinstance(frame, AudioRawFrame):
        extras["bytes"] = len(frame.audio)
        extras["sample_rate"] = frame.sample_rate
    return extras


class FrameLogObserver(BaseObserver):
    """Writes one JSONL line per frame hop (processor -> processor).

    Audio frames arrive 50 times a second on every hop, so by default only the first audio
    frame of each burst on a hop is written (`burst_start`), with a running count. That is
    enough to see "first audio from Timbre" and "first audio to the browser"."""

    def __init__(self, log: JsonlLog, *, log_audio_frames: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._log = log
        self._all_audio = log_audio_frames
        self._last_audio: dict[tuple[str, str, str], float] = {}
        self.audio_counts: dict[str, int] = {}

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        src, dst = str(data.source), str(data.destination)
        direction = "down" if data.direction == FrameDirection.DOWNSTREAM else "up"
        ftype = type(frame).__name__
        if isinstance(frame, AudioRawFrame):
            key = (src, dst, ftype)
            self.audio_counts[ftype] = self.audio_counts.get(ftype, 0) + 1
            now = time.monotonic()
            last = self._last_audio.get(key)
            self._last_audio[key] = now
            burst_start = last is None or now - last > AUDIO_BURST_GAP_S
            if not (burst_start or self._all_audio):
                return
            self._log.write(
                "frame", frame=ftype, src=src, dst=dst, dir=direction, clock_ns=data.timestamp,
                burst_start=burst_start, **_frame_extras(frame),
            )
            return
        self._log.write(
            "frame", frame=ftype, src=src, dst=dst, dir=direction, clock_ns=data.timestamp,
            **_frame_extras(frame),
        )


# ---------------------------------------------------------------------------
# The echo itself
# ---------------------------------------------------------------------------


@dataclass
class _EchoTurnEnd(ControlFrame):
    """Private marker that EchoProcessor queues to itself when the user turn stops."""


class EchoProcessor(FrameProcessor):
    """Collects the final transcripts of one user turn and says them back when the turn stops.

    In Pipecat 1.5.0 the "user turn stopped" signal reaches downstream processors as a
    `UserStoppedSpeakingFrame` broadcast by `UserTurnProcessor` (the same moment its
    `on_user_turn_stopped` event fires). Final transcripts are `TranscriptionFrame`s.

    Ordering trap (seen in the first headless run): `UserStoppedSpeakingFrame` is a
    SystemFrame, which every FrameProcessor handles at once, while `TranscriptionFrame` is a
    DataFrame that waits in the processor's own queue. So the stop can be handled BEFORE the
    turn's last transcript, even though UserTurnProcessor pushed the transcript first. We
    therefore queue a private non-system marker on stop; it lands behind any transcripts
    still waiting, and the echo happens when the marker comes up.
    """

    def __init__(self, log: JsonlLog | None = None, **kwargs):
        super().__init__(**kwargs)
        self._log = log
        self._parts: list[str] = []
        self._turn_open = False
        self._parts_when_stop_seen = 0
        self.spoken: list[str] = []  # what we asked TTS to say (read by the smoke test)

    def _event(self, kind: str, **fields: Any) -> None:
        if self._log is not None:
            self._log.write(kind, **fields)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, _EchoTurnEnd):
            await self._echo()
            return  # the marker is ours; it does not travel on

        if isinstance(frame, UserStartedSpeakingFrame):
            self._turn_open = True
        elif isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                self._parts.append(frame.text.strip())
            if not self._turn_open:
                # A final that arrived after this turn's echo, with no user turn open. With
                # the default start strategies it opens a new turn itself (and interrupts the
                # bot). We want to know how often Prisma's last final lands that late.
                self._event("transcript_outside_turn", text=frame.text)

        # Pass every frame on first, so ordering downstream is unchanged.
        await self.push_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._parts_when_stop_seen = len(self._parts)
            await self.queue_frame(_EchoTurnEnd(), FrameDirection.DOWNSTREAM)

    async def _echo(self) -> None:
        self._turn_open = False
        text = " ".join(self._parts).strip()
        # How many transcripts were still queued here when the (system) stop frame was handled.
        queued_behind_stop = len(self._parts) - self._parts_when_stop_seen
        self._parts = []
        if text:
            self.spoken.append(text)
            self._event("echo", text=text, transcripts_queued_behind_stop=queued_behind_stop)
            await self.push_frame(TTSSpeakFrame(text, append_to_context=False))
        else:
            self._event("echo_skipped", reason="turn stopped with no final transcript")


# ---------------------------------------------------------------------------
# Gnani services (real network; not used by the offline smoke test)
# ---------------------------------------------------------------------------


def make_gnani_services(cfg: EchoConfig, log: JsonlLog | None = None):
    """Build Prisma STT and Timbre TTS. By default these are our own V2 services from
    `mockboard.speech` (ruling 24 and 25); `--stock-stt` / `--stock-tts` swap in the plain
    plugin classes for comparison.

    Imported here, not at module level, so the smoke test needs no Gnani packages."""
    from pipecat.transcriptions.language import Language
    from pipecat_gnani import GnaniSTTService, GnaniTTSService

    from mockboard.speech.gnani_protocol import STTConfig
    from mockboard.speech.prisma_stt import PrismaSTTService
    from mockboard.speech.timbre_tts import TimbreTTSService

    if cfg.stock_stt:
        # The plugin as it ships: Pipecat's 20 ms (640-byte) frames, no VAD headers.
        # UNVERIFIED: whether Prisma accepts 640-byte frames (its docs say 1,024).
        try:
            language: Any = Language(cfg.stt_language)
        except ValueError:
            language = cfg.stt_language  # the plugin passes unknown strings through as-is
        stt = GnaniSTTService(
            api_key=cfg.api_key,
            sample_rate=SAMPLE_RATE,
            settings=GnaniSTTService.Settings(language=language, format=cfg.stt_format),
        )
    else:
        # Ours: exact 1,024-byte frames at real-time cadence, our headers, reconnects.
        # UNVERIFIED: that Prisma accepts these headers and frames (fake server only so far).
        stt = PrismaSTTService(
            api_key=cfg.api_key,
            url=cfg.stt_url,
            config=STTConfig(
                lang_code=cfg.stt_language,
                sample_rate=SAMPLE_RATE,
                fmt=cfg.stt_format,
                max_speech_s=cfg.stt_max_speech_s,
                min_silence_ms=cfg.stt_min_silence_ms,
                vad_threshold=cfg.stt_vad_threshold,
            ),
        )
        if log is not None:
            @stt.event_handler("on_processing")
            async def _processing(_stt, event):
                log.write("prisma_processing", request_id=event.request_id,
                          pending_segments=_stt.pending_segments)

            @stt.event_handler("on_prisma_error")
            async def _prisma_error(_stt, message):
                log.write("prisma_error", message=message)

    if cfg.stock_tts:
        # The plugin's own (leaky) interruption handling, its wav container, and no check
        # for a socket Timbre closed while idle (spikes/NOTES.md).
        tts = GnaniTTSService(
            api_key=cfg.api_key,
            # gnani-vachana 0.7.9's default model is timbre-v2.0, which has no Poorvi.
            model=cfg.tts_model,
            sample_rate=SAMPLE_RATE,
            settings=GnaniTTSService.Settings(voice=cfg.tts_voice, language=cfg.tts_language),
        )
    else:
        # Ours: closes the socket on barge-in, asks for raw 16-bit PCM.
        # UNVERIFIED: in a real browser against real Timbre (fake socket only so far).
        tts = TimbreTTSService(
            api_key=cfg.api_key,
            voice=cfg.tts_voice,
            model=cfg.tts_model,
            language=cfg.tts_language,
            sample_rate=SAMPLE_RATE,
        )
    return stt, tts


# ---------------------------------------------------------------------------
# Pipeline assembly (shared by the live server and the offline smoke test)
# ---------------------------------------------------------------------------


@dataclass
class EchoPipeline:
    pipeline: Pipeline
    worker: PipelineWorker
    vad: VADProcessor
    user_turn: UserTurnProcessor
    echo: EchoProcessor
    observer: FrameLogObserver
    log: JsonlLog
    processors: list[FrameProcessor] = field(default_factory=list)


def build_echo_pipeline(
    *,
    transport_input: FrameProcessor,
    transport_output: FrameProcessor,
    stt: FrameProcessor,
    tts: FrameProcessor,
    cfg: EchoConfig,
    log: JsonlLog,
    user_turn_stop_timeout: float = 5.0,
) -> EchoPipeline:
    """Wire the processors in order and wrap them in a PipelineWorker.

    In 1.5.0 `PipelineTask`/`PipelineRunner` are deprecated aliases (since 1.3.0) of
    `PipelineWorker`/`WorkerRunner`, so we use the new names."""
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams()))
    strategies = UserTurnStrategies(
        # None means Pipecat's defaults:
        # [VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy].
        start=[VADUserTurnStartStrategy()] if cfg.vad_start_only else None,
        # Smart Turn v3 (the bundled smart-turn-v3.2-cpu.onnx model) is also 1.5.0's default.
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
    )
    user_turn = UserTurnProcessor(
        user_turn_strategies=strategies, user_turn_stop_timeout=user_turn_stop_timeout
    )
    echo = EchoProcessor(log)

    @user_turn.event_handler("on_user_turn_started")
    async def _started(_proc, strategy):
        log.write("event", name="on_user_turn_started", strategy=type(strategy).__name__)

    @user_turn.event_handler("on_user_turn_stopped")
    async def _stopped(_proc, strategy):
        # strategy is None when the 5 s user_turn_stop_timeout ended the turn.
        log.write("event", name="on_user_turn_stopped",
                  strategy=type(strategy).__name__ if strategy else None)

    @user_turn.event_handler("on_user_turn_stop_timeout")
    async def _timeout(_proc):
        log.write("event", name="on_user_turn_stop_timeout")

    processors = [transport_input, vad, stt, user_turn, echo, tts, transport_output]
    pipeline = Pipeline(processors)
    observer = FrameLogObserver(log, log_audio_frames=cfg.log_audio_frames)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            enable_metrics=True,
        ),
        observers=[observer],
        # Our page does not speak Pipecat's RTVI client protocol.
        enable_rtvi=False,
    )
    return EchoPipeline(pipeline, worker, vad, user_turn, echo, observer, log, processors)


async def run_echo_bot(webrtc_connection, cfg: EchoConfig) -> None:
    """Run one browser session until the browser disconnects. Called by server.py."""
    from pipecat.transports.base_transport import TransportParams
    from pipecat.workers.runner import WorkerRunner

    from mockboard.voice.cv2_shim import ensure_cv2

    ensure_cv2()  # the voice extra no longer installs OpenCV (defect E)
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = JsonlLog(cfg.log_dir / f"echo_{stamp}.jsonl")
    log.write("session_start", stt_language=cfg.stt_language, tts_voice=cfg.tts_voice,
              tts_model=cfg.tts_model, tts_language=cfg.tts_language,
              stt_max_speech_s=cfg.stt_max_speech_s, stt_min_silence_ms=cfg.stt_min_silence_ms,
              stt_vad_threshold=cfg.stt_vad_threshold,
              stock_stt=cfg.stock_stt, stock_tts=cfg.stock_tts,
              vad_start_only=cfg.vad_start_only, fake_speech=cfg.fake_speech)
    logger.info(f"Frame log: {log.path}")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    if cfg.fake_speech:
        from .fakes import FakeSTT, FakeTTS

        stt, tts = FakeSTT(), FakeTTS()
    else:
        stt, tts = make_gnani_services(cfg, log)
    for name, svc in (("stt", stt), ("tts", tts)):
        for event in ("on_connected", "on_disconnected", "on_connection_error"):
            def _make(n=name, e=event):
                async def _handler(_svc, *args):
                    log.write("event", name=f"{n}.{e}", args=[str(a) for a in args])
                return _handler
            svc.add_event_handler(event, _make())

    built = build_echo_pipeline(
        transport_input=transport.input(), transport_output=transport.output(),
        stt=stt, tts=tts, cfg=cfg, log=log,
    )

    @transport.event_handler("on_client_connected")
    async def _connected(_transport, _client):
        log.write("event", name="on_client_connected")

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport, _client):
        log.write("event", name="on_client_disconnected")
        await built.worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(built.worker)
    try:
        await runner.run()
    finally:
        log.write("session_end", audio_frame_counts=built.observer.audio_counts)
        log.close()
        logger.info(f"Session ended. Frame log: {log.path}")
