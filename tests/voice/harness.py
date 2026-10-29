"""A live Pipecat pipeline for one service under test: frames in, frames out, in real time.

`pipecat.tests.utils.run_test` sends all frames at once and then ends the pipeline, which is
too coarse for cadence, reconnect and interruption timing. This harness runs a real
PipelineWorker in the background, lets a test queue frames whenever it wants, and collects
every frame that comes out of the service downstream, stamped with arrival time.
Imported only by tests that already skipped without the voice extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from pipecat.frames.frames import EndFrame, Frame, InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

SAMPLE_RATE = 16000
PIPECAT_FRAME_BYTES = 640  # 20 ms of 16 kHz s16le, as SmallWebRTC delivers it


class Collector(FrameProcessor):
    """Records every downstream frame (with time.monotonic()) and passes it on."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.frames: list[tuple[float, Frame]] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            self.frames.append((time.monotonic(), frame))
        await self.push_frame(frame, direction)

    def of_type(self, *types) -> list[Frame]:
        return [f for _, f in self.frames if isinstance(f, types)]

    def timed(self, *types) -> list[tuple[float, Frame]]:
        return [(t, f) for t, f in self.frames if isinstance(f, types)]


class Harness:
    """`async with Harness(service) as h:` runs [service, collector] until the block ends."""

    def __init__(self, service: FrameProcessor):
        self.service = service
        self.collector = Collector()
        self.errors: list[str] = []
        self.worker = PipelineWorker(
            Pipeline([service, self.collector]),
            params=PipelineParams(
                audio_in_sample_rate=SAMPLE_RATE, audio_out_sample_rate=SAMPLE_RATE
            ),
            enable_rtvi=False,
            cancel_on_idle_timeout=False,
        )
        self._run: asyncio.Task | None = None

        @service.event_handler("on_error")
        async def _on_error(_proc, error):
            self.errors.append(error.error)

    async def __aenter__(self) -> Harness:
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(self.worker)
        self._run = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(self._run, timeout=10)
        except TimeoutError:
            await self.worker.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._run, timeout=5)

    async def queue(self, frame: Frame) -> None:
        await self.worker.queue_frame(frame)

    async def feed_audio(self, seconds: float, *, real_time: bool = True) -> None:
        """Feed 20 ms microphone frames of quiet noise, like SmallWebRTC's input."""
        n = round(seconds / 0.02)
        t0 = time.monotonic()
        for i in range(n):
            pcm = (b"\x10\x00\xf0\xff") * (PIPECAT_FRAME_BYTES // 4)
            await self.queue(
                InputAudioRawFrame(audio=pcm, sample_rate=SAMPLE_RATE, num_channels=1)
            )
            if real_time:
                delay = t0 + (i + 1) * 0.02 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

    async def wait_until(self, predicate, timeout: float = 5.0, step: float = 0.01) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            await asyncio.sleep(step)
        return predicate()
