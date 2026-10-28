"""Offline smoke test for the V1 echo spike (spikes/echo_bot/).

No network, no microphone, no Gnani key: the speech services are fakes, and the transport is a
pair of pass-through processors. The whole module is skipped when the `voice` extra is not
installed, so `make test` stays green on a plain checkout.

What it checks:
1. The pipeline has the real 1.5.0 pieces in the right order (Silero in a VADProcessor, Smart
   Turn v3 in a UserTurnProcessor, our EchoProcessor between STT and TTS).
2. A final transcript travels the real turn controller and comes back as speech.
3. The echo survives Pipecat's ordering trap (a SystemFrame stop overtaking DataFrame
   transcripts).
4. The spike's two Gnani fixes do what spikes/NOTES.md says (needs pipecat-gnani; fake socket).
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

pytest.importorskip("pipecat")
pytest.importorskip("aiortc")  # the rest of the voice extra; the spike needs it too

from pipecat.frames.frames import (
    EndFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.turns.user_start import (
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner
from spikes.echo_bot.bot import (
    EchoConfig,
    EchoProcessor,
    JsonlLog,
    build_echo_pipeline,
)
from spikes.echo_bot.fakes import FakeSTT, FakeTTS


class PassThrough(FrameProcessor):
    """Stands in for SmallWebRTCTransport.input()/.output(): no audio device, no network.

    A bare FrameProcessor forwards nothing, so this one passes every frame on."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def transcript(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="", timestamp=time_now_iso8601())


def build(tmp_path, **cfg_kwargs):
    stt, tts = FakeSTT(), FakeTTS()
    log = JsonlLog(tmp_path / "echo.jsonl")
    built = build_echo_pipeline(
        transport_input=PassThrough(name="fake_input"),
        transport_output=PassThrough(name="fake_output"),
        stt=stt,
        tts=tts,
        cfg=EchoConfig(log_dir=tmp_path, **cfg_kwargs),
        log=log,
        user_turn_stop_timeout=2.0,
    )
    return built, stt, tts


def test_pipeline_has_the_real_pieces_in_order(tmp_path):
    built, _stt, _tts = build(tmp_path)
    assert isinstance(built.pipeline, Pipeline)
    assert isinstance(built.worker, PipelineWorker)
    kinds = [type(p) for p in built.processors]
    assert kinds == [
        PassThrough, VADProcessor, FakeSTT, UserTurnProcessor, EchoProcessor, FakeTTS, PassThrough
    ]
    strategies = built.user_turn._user_turn_controller.user_turn_strategies
    stop = strategies.stop[0]
    assert type(stop).__name__ == "TurnAnalyzerUserTurnStopStrategy"
    assert type(stop._turn_analyzer).__name__ == "LocalSmartTurnAnalyzerV3"
    # Pipecat 1.5.0's default start strategies: Silero OR any transcript may start a turn.
    assert [type(s) for s in strategies.start] == [
        VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy
    ]
    built.log.close()


def test_vad_start_only_drops_the_transcript_start_strategy(tmp_path):
    built, _, _ = build(tmp_path, vad_start_only=True)
    strategies = built.user_turn._user_turn_controller.user_turn_strategies
    assert [type(s) for s in strategies.start] == [VADUserTurnStartStrategy]
    built.log.close()


async def test_a_final_transcript_is_echoed_through_the_real_turn_controller(tmp_path):
    built, _, tts = build(tmp_path)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(built.worker)

    async def feed():
        await asyncio.sleep(0.2)
        # No VAD frames: TranscriptionUserTurnStartStrategy opens the turn, and Smart Turn's
        # stop strategy ends it after FakeSTT's 0.05 s "p99" (its no-VAD fallback path).
        await built.worker.queue_frame(transcript("namaste board"))
        for _ in range(100):
            if tts.texts:
                break
            await asyncio.sleep(0.05)
        await built.worker.queue_frame(EndFrame())

    await asyncio.wait_for(asyncio.gather(runner.run(), feed()), timeout=20)
    built.log.close()

    assert tts.texts == ["namaste board"]
    rows = [json.loads(line) for line in (tmp_path / "echo.jsonl").read_text().splitlines()]
    names = [r.get("name") for r in rows if r["kind"] == "event"]
    assert "on_user_turn_started" in names and "on_user_turn_stopped" in names
    echo = next(r for r in rows if r["kind"] == "echo")
    assert echo["text"] == "namaste board"
    # The log has frame hops with timestamps; TTS audio came back out of FakeTTS.
    hops = [r for r in rows if r["kind"] == "frame"]
    assert all({"frame", "src", "dst", "dir", "ms", "clock_ns"} <= r.keys() for r in hops)
    assert any(r["frame"] == "TTSAudioRawFrame" and r["src"].startswith("FakeTTS") for r in hops)


async def test_echo_waits_for_transcripts_queued_behind_a_system_stop_frame():
    """UserStoppedSpeakingFrame is a SystemFrame and is handled before DataFrames that are
    still queued in the same processor. The echo must still include every transcript."""
    echo, tts = EchoProcessor(), FakeTTS()
    await run_test(
        Pipeline([echo, tts]),
        frames_to_send=[
            UserStartedSpeakingFrame(),
            transcript("namaste board"),
            transcript("kaise ho"),
            UserStoppedSpeakingFrame(),
            SleepFrame(0.3),
        ],
    )
    assert tts.texts == ["namaste board kaise ho"]
    assert echo.spoken == ["namaste board kaise ho"]


# ---------------------------------------------------------------------------
# The spike's Gnani fixes, against a fake socket (needs pipecat-gnani installed)
# ---------------------------------------------------------------------------


class FakeTimbreSocket:
    """Streams 20 audio chunks, 50 ms apart, for each request; records closes."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.sent: list = []
        self.tasks: set[asyncio.Task] = set()

    async def send(self, msg):
        self.sent.append(msg)

        async def stream():
            for i in range(20):
                await asyncio.sleep(0.05)
                if self.closed:
                    return
                audio = base64.b64encode(b"\x01\x00" * 800).decode()
                await self.queue.put(
                    json.dumps({"type": "audio", "data": {"chunk_index": i, "audio": audio}})
                )
            await self.queue.put(json.dumps({"type": "complete", "data": {"audio": ""}}))

        task = asyncio.create_task(stream())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self):
        self.closed = True
        await self.queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


async def _audio_after_interruption(monkeypatch, *, stock_tts: bool) -> tuple[int, int]:
    gnani_tts = pytest.importorskip("pipecat_gnani.tts")
    from spikes.echo_bot.bot import make_gnani_services

    sockets: list[FakeTimbreSocket] = []

    async def fake_connect(*_args, **_kwargs):
        sockets.append(FakeTimbreSocket())
        return sockets[-1]

    monkeypatch.setattr(gnani_tts, "websocket_connect", fake_connect)
    _, tts = make_gnani_services(EchoConfig(api_key="test-key", stock_tts=stock_tts))
    down, _ = await run_test(
        tts,
        frames_to_send=[
            TTSSpeakFrame("Namaste, yeh ek lambi line hai.", append_to_context=False),
            SleepFrame(0.3),
            InterruptionFrame(),
            SleepFrame(1.2),
        ],
    )
    after = 0
    seen_interruption = False
    for frame in down:
        seen_interruption = seen_interruption or isinstance(frame, InterruptionFrame)
        after += int(seen_interruption and isinstance(frame, TTSAudioRawFrame))
    return after, len(sockets)


async def test_stock_gnani_tts_keeps_playing_after_an_interruption(monkeypatch):
    """Documents pipecat-gnani 0.5.12 on pipecat-ai 1.5.0: the socket is not closed on
    interruption, so the rest of the line still arrives. If this starts failing after an
    upgrade, the plugin was fixed and the spike's override can go."""
    after, sockets = await _audio_after_interruption(monkeypatch, stock_tts=True)
    assert after > 0
    assert sockets == 1  # no reconnect happened


async def test_closing_gnani_tts_stops_audio_at_the_interruption(monkeypatch):
    after, sockets = await _audio_after_interruption(monkeypatch, stock_tts=False)
    assert after == 0
    assert sockets == 2  # closed on interruption, reopened for the next line


async def test_framed_stt_sends_exact_1024_byte_frames():
    pytest.importorskip("pipecat_gnani")
    from spikes.echo_bot.bot import make_gnani_services

    class RecordingSocket:
        def __init__(self):
            self.sent: list[bytes] = []

        async def send(self, data):
            self.sent.append(data)

    stt, _ = make_gnani_services(EchoConfig(api_key="test-key"))
    socket = RecordingSocket()
    stt._ws = socket  # skip the real connect; run_stt only needs .send()
    for _ in range(8):  # eight 20 ms frames of 16 kHz PCM, as SmallWebRTC delivers them
        async for _frame in stt.run_stt(b"\x00\x00" * 320):
            pass
    assert [len(chunk) for chunk in socket.sent] == [1024] * 5  # 5,120 bytes in, 5 frames out
