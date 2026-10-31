"""Opt-in recording of the candidate's own microphone audio (ruling 35).

Off by default. Only `python -m mockboard.voice.server --record-audio` turns it on, the page
then says so, and the file is written only on this computer, under the trace folder, as
`<session id>.mic.wav` (16 kHz, 16-bit mono: exactly what Prisma received). It exists so the
owner can replay his own Hinglish through Prisma with different language settings
(`python -m mockboard.voice.stt_ab`) and choose by reading the transcripts. The board's voice is
not recorded. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import wave
from pathlib import Path

from pipecat.frames.frames import CancelFrame, EndFrame, Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class MicRecorder(FrameProcessor):
    """Passes every frame on; appends input audio to a WAV file as it arrives."""

    def __init__(self, path: Path, *, sample_rate: int = 16000, **kwargs):
        super().__init__(**kwargs)
        self.path = Path(path)
        self.sample_rate = sample_rate
        self._wav: wave.Wave_write | None = None
        self.frames_written = 0
        self.skipped = 0  # frames at another rate or channel count (not expected)

    def _open(self) -> wave.Wave_write:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(self.path), "wb")  # noqa: SIM115 (kept open for the session)
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.sample_rate)
        return wav

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()  # writes the final header sizes
            self._wav = None

    @property
    def seconds(self) -> float:
        return self.frames_written / self.sample_rate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if frame.sample_rate == self.sample_rate and frame.num_channels == 1:
                if self._wav is None:
                    self._wav = self._open()
                self._wav.writeframes(frame.audio)
                self.frames_written += len(frame.audio) // 2
            else:
                self.skipped += 1
        if isinstance(frame, EndFrame | CancelFrame):
            self.close()
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await super().cleanup()
        self.close()
