"""Fake speech services for the echo spike: no network, no Gnani credits.

* `FakeSTT` answers every end of speech (Silero's VADUserStoppedSpeakingFrame) with one fixed
  final transcript, "fake transcript 1", "fake transcript 2", ...
* `FakeTTS` answers every text with a short beep whose length grows with the number of words.

Used by the offline smoke test, and by `server.py --fake-speech` so the owner can check the
browser, WebRTC, Silero and Smart Turn path before spending credits: speak, pause, hear a beep.
"""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

SAMPLE_RATE = 16000


class FakeSTT(STTService):
    def __init__(self, **kwargs):
        # A small p99 so Smart Turn's stop strategy does not wait the 1.0 s default.
        super().__init__(sample_rate=SAMPLE_RATE, ttfs_p99_latency=0.05,
                         settings=STTSettings(model=None, language=None), **kwargs)
        self.count = 0

    def can_generate_metrics(self) -> bool:
        return False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        yield None  # audio is ignored; transcripts come from VAD stops below

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self.count += 1
            await self.push_frame(
                TranscriptionFrame(
                    text=f"fake transcript {self.count}",
                    user_id="",
                    timestamp=time_now_iso8601(),
                    result={"type": "transcript", "fake": True},
                )
            )


def beep_pcm(seconds: float, freq_hz: float = 660.0, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A quiet sine beep as 16-bit little-endian mono PCM."""
    n = int(seconds * sample_rate)
    amp = 0.2 * 32767
    samples = (int(amp * math.sin(2 * math.pi * freq_hz * i / sample_rate)) for i in range(n))
    return b"".join(v.to_bytes(2, "little", signed=True) for v in samples)


class FakeTTS(TTSService):
    def __init__(self, **kwargs):
        super().__init__(sample_rate=SAMPLE_RATE, push_start_frame=True, push_stop_frames=True,
                         settings=TTSSettings(model=None, voice=None, language=None), **kwargs)
        self.texts: list[str] = []

    def can_generate_metrics(self) -> bool:
        return False

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        self.texts.append(text)
        seconds = min(2.0, 0.15 + 0.1 * len(text.split()))
        pcm = beep_pcm(seconds)
        chunk = SAMPLE_RATE // 50 * 2  # 20 ms of 16-bit audio
        for i in range(0, len(pcm), chunk):
            yield TTSAudioRawFrame(
                audio=pcm[i : i + chunk], sample_rate=SAMPLE_RATE, num_channels=1,
                context_id=context_id,
            )
