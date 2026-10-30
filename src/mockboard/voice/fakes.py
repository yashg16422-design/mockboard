"""Fake speech for free runs (`--fake-speech`) and offline tests: no network, no credits.

* `FakeCommitSTT` behaves like our Prisma service as the voice loop sees it: an utterance
  opens on Silero's start, closes on its own `segment_delay_s` after Silero's stop (Prisma's
  silence window) unless `commit()` closes it first, and each transcript carries the same
  `result["mb"]` tags (`seq`, `commit_id`) as `PrismaSTTService`. Texts come from `answers`
  (cycled) or "fake answer N".
* `FakeTTSService` turns each `TTSSpeakFrame` into a beep (or silence) one request at a time,
  after `ttfb_s`, and reports to a `PlaybackLedger` like `TimbreTTSService`.
* `FakePlayback` stands in for the output transport in offline tests: it plays audio in real
  time and sends `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` upstream, and drops
  queued audio on an interruption.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from collections.abc import AsyncGenerator, Sequence
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.utils.time import time_now_iso8601

from mockboard.speech.prisma_stt import CommitResult

SAMPLE_RATE = 16000

DEFAULT_ANSWERS = (
    "Main Kanpur se hoon. Wahan traffic aur pollution sabse badi samasya hai, kyunki sadkein "
    "sankri hain aur public transport kam hai.",
    "Iska hal hai ki metro aur bus ko joda jaye, aur logon ko cycle ke liye surakshit rasta mile.",
    "Tourism se rozgaar badhta hai, lekin local logon ki bhagidari zaroori hai, warna fayda bahar "
    "chala jata hai.",
    "Mujhe exact figure yaad nahi hai, lekin trend saaf hai ki incidents kam hue hain.",
)


class FakeCommitSTT(STTService):
    def __init__(
        self,
        *,
        answers: Sequence[str] = DEFAULT_ANSWERS,
        segment_delay_s: float = 0.5,
        commit_delay_s: float = 0.05,
        **kwargs,
    ):
        super().__init__(sample_rate=SAMPLE_RATE, ttfs_p99_latency=0.05,
                         settings=STTSettings(model=None, language=None), **kwargs)
        self._answers = itertools.cycle(answers or DEFAULT_ANSWERS)
        self._segment_delay_s = segment_delay_s
        self._commit_delay_s = commit_delay_s
        self._open = False  # Silero heard speech that has not been transcribed yet
        self._close_task: asyncio.Task | None = None
        self._seq = 0
        self._commits = 0
        self.served_model = "fake"
        self.gave_up: str | None = None
        self.commits: list[CommitResult] = []
        self._register_event_handler("on_gave_up")

    def can_generate_metrics(self) -> bool:
        return False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._open = True
            if self._close_task is not None:
                self._close_task.cancel()
                self._close_task = None
        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._open:
            self._close_task = asyncio.create_task(self._close_later())

    async def _close_later(self) -> None:
        await asyncio.sleep(self._segment_delay_s)
        self._close_task = None
        if self._open:
            await self._emit(None)

    async def _emit(self, commit_id: int | None) -> int:
        self._open = False
        self._seq += 1
        await self.push_frame(
            TranscriptionFrame(
                text=next(self._answers), user_id="", timestamp=time_now_iso8601(),
                result={"type": "transcript", "fake": True,
                        "mb": {"seq": self._seq, "commit_id": commit_id, "conn": "fake-c1"}},
            )
        )
        return self._seq

    async def commit(self, *, wait_s: float | None = None, noop_s: float | None = None
                     ) -> CommitResult:
        self._commits += 1
        queued = time.monotonic()
        await asyncio.sleep(self._commit_delay_s)
        texts: list[str] = []
        last_seq = None
        reply = "none"
        was_open = self._open
        if self._open:
            if self._close_task is not None:
                self._close_task.cancel()
                self._close_task = None
            last_seq = await self._emit(self._commits)
            reply, texts = "transcript", ["(fake)"]
        result = CommitResult(self._commits, reply, texts, last_seq, 0, was_open, queued,
                              queued, time.monotonic())
        self.commits.append(result)
        return result


def tone_pcm(seconds: float, freq_hz: float = 440.0, volume: float = 0.15) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    amp = volume * 32767
    return b"".join(
        int(amp * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE)).to_bytes(2, "little",
                                                                              signed=True)
        for i in range(n)
    )


class FakeTTSService(FrameProcessor):
    """One `TTSSpeakFrame` at a time (as `TimbreTTSService` serialises): `ttfb_s`, then audio
    of about `ms_per_word` per word in 20 ms chunks pushed quickly (a streaming service sends
    faster than real time), then `TTSStoppedFrame`. An interruption cancels the request."""

    def __init__(self, *, ledger: Any = None, ttfb_s: float = 0.05, ms_per_word: int = 120,
                 chunk_gap_s: float = 0.002, audible: bool = False, fail_marker: str = "",
                 **kwargs):
        super().__init__(**kwargs)
        self.ledger = ledger
        self.ttfb_s, self.ms_per_word, self.chunk_gap_s = ttfb_s, ms_per_word, chunk_gap_s
        self.audible = audible
        self.fail_marker = fail_marker
        self.texts: list[str] = []
        self._register_event_handler("on_first_byte_timeout")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            await self._speak(frame.text)
            return
        await self.push_frame(frame, direction)

    async def _speak(self, text: str) -> None:
        self.texts.append(text)
        if self.ledger is not None:
            self.ledger.unit_started(text)
        done = False
        try:
            await asyncio.sleep(self.ttfb_s)
            if self.fail_marker and self.fail_marker in text:
                await self._call_event_handler("on_first_byte_timeout", text)
                return
            await self.push_frame(TTSStartedFrame())
            seconds = max(0.2, self.ms_per_word * len(text.split()) / 1000)
            pcm = tone_pcm(seconds) if self.audible else b"\x00\x00" * int(seconds * SAMPLE_RATE)
            step = SAMPLE_RATE // 50 * 2
            for i in range(0, len(pcm), step):
                chunk = pcm[i : i + step]
                if self.ledger is not None:
                    self.ledger.audio(len(chunk), SAMPLE_RATE)
                await self.push_frame(TTSAudioRawFrame(audio=chunk, sample_rate=SAMPLE_RATE,
                                                       num_channels=1))
                await asyncio.sleep(self.chunk_gap_s)
            await self.push_frame(TTSStoppedFrame())
            done = True
        finally:
            if self.ledger is not None:
                self.ledger.unit_done(failed=not done)


class FakePlayback(FrameProcessor):
    """The output transport for offline tests: plays `TTSAudioRawFrame`s in real time and
    reports `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` upstream, as Pipecat's
    output transport does. An `InterruptionFrame` drops queued audio at once."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._player: asyncio.Task | None = None
        self._speaking = False
        self.played_ms = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._player = self.create_task(self._play(), "fake-playback")
            return
        if isinstance(frame, EndFrame | CancelFrame) and self._player is not None:
            await self.cancel_task(self._player)
            self._player = None
        if isinstance(frame, InterruptionFrame):
            while not self._queue.empty():
                self._queue.get_nowait()
            await self._stopped()
        if isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self._queue.put_nowait(frame.audio)
            return  # consumed, like a real output transport
        await self.push_frame(frame, direction)

    async def _stopped(self) -> None:
        if self._speaking:
            self._speaking = False
            await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    async def _play(self) -> None:
        while True:
            try:
                audio = await asyncio.wait_for(self._queue.get(), timeout=0.08)
            except TimeoutError:
                await self._stopped()
                continue
            if not self._speaking:
                self._speaking = True
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            ms = len(audio) / 2 / SAMPLE_RATE * 1000
            await asyncio.sleep(ms / 1000)
            self.played_ms += ms
