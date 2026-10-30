"""Turn-taking strategies for Pipecat 1.5.0's `UserTurnProcessor` (V3/V4).

* `BoardAwareStartStrategy` (V4 barge-in basics, rulings 26 and 31): only Silero starts a
  user turn, never a transcript, so a late Prisma final can never open a turn and interrupt
  the board (ruling 26). While the board is audibly speaking, the candidate's speech must last
  `barge_in_min_speech_ms` (after Silero's own start delay) before it counts, so a cough does
  not cut the board off; before the board's first audio (it is still thinking) the shorter
  `resume_min_speech_ms` applies, and the turn reopens (ruling 31). With the board silent, a
  turn starts at once.
* `PausedTurnStopStrategy`: Smart Turn v3 decides the end of the turn without waiting for a
  transcript (`wait_for_transcript=False`), because the voice loop then sends Prisma a
  `commit` and waits for the transcript itself (ruling 28). For a long turn (a debate opening
  or closing) the pause must also last `extra_pause_s` after Smart Turn says "complete".
* `TurnTuner` applies the session's per-turn `TurnParams` (CTO_NOTES_04 §7) to the strategy,
  the Smart Turn analyzer and the user-turn controller before each candidate turn.

Pipecat names were read in the installed 1.5.0 source. Two private attributes are set by
`TurnTuner` (`BaseSmartTurn._stop_ms` and `UserTurnController._user_turn_stop_timeout`), both
read afresh on every use in 1.5.0; a test fails if an upgrade renames them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)

# What the board is doing, as the start strategy needs to know it.
IDLE, THINKING, SPEAKING = "idle", "thinking", "speaking"


class BoardAwareStartStrategy(BaseUserTurnStartStrategy):
    """Start a user turn from Silero only, with a minimum speech length while the board is
    busy. `board_phase()` returns IDLE, THINKING or SPEAKING; `thresholds_ms()` returns
    (resume_ms, barge_in_ms)."""

    def __init__(
        self,
        *,
        board_phase: Callable[[], str],
        thresholds_ms: Callable[[], tuple[int, int]],
        clock: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._phase = board_phase
        self._thresholds = thresholds_ms
        self._clock = clock
        self._speech_since: float | None = None
        self.ignored: list[float] = []  # speech bursts too short to count (ms), for the trace

    async def reset(self):
        await super().reset()
        self._speech_since = None

    def _needed_ms(self) -> int:
        resume_ms, barge_ms = self._thresholds()
        phase = self._phase()
        if phase == SPEAKING:
            return barge_ms
        if phase == THINKING:
            return resume_ms
        return 0

    async def _maybe_start(self) -> ProcessFrameResult:
        if self._speech_since is None:
            return ProcessFrameResult.CONTINUE
        if (self._clock() - self._speech_since) * 1000 >= self._needed_ms():
            self._speech_since = None
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        return ProcessFrameResult.CONTINUE

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speech_since = self._clock()
            return await self._maybe_start()
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._speech_since is not None:
                self.ignored.append(round((self._clock() - self._speech_since) * 1000, 1))
            self._speech_since = None
            return ProcessFrameResult.CONTINUE
        if isinstance(frame, InputAudioRawFrame | BotStoppedSpeakingFrame):
            # Audio arrives every 20 ms, so the threshold is checked often enough; when the
            # board falls silent, speech already under way starts the turn at once.
            return await self._maybe_start()
        return ProcessFrameResult.CONTINUE


class PausedTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """Smart Turn ends the turn on its own (no transcript wait); with `extra_pause_s` > 0 the
    silence must also last that long, and speech resuming meanwhile cancels the stop."""

    def __init__(self, *, extra_pause_s: float = 0.0, **kwargs):
        kwargs.setdefault("wait_for_transcript", False)
        super().__init__(**kwargs)
        self.extra_pause_s = extra_pause_s
        self._pause_task: asyncio.Task | None = None

    async def _cancel_pause(self) -> None:
        if self._pause_task is not None:
            await self.task_manager.cancel_task(self._pause_task)
            self._pause_task = None

    async def reset(self):
        await self._cancel_pause()
        await super().reset()

    async def cleanup(self):
        await self._cancel_pause()
        await super().cleanup()

    async def _handle_vad_user_started_speaking(self, frame):
        await self._cancel_pause()
        await super()._handle_vad_user_started_speaking(frame)

    async def trigger_user_turn_stopped(self):
        if self.extra_pause_s <= 0:
            await super().trigger_user_turn_stopped()
            return
        if self._pause_task is None:
            self._pause_task = self.task_manager.create_task(
                self._stop_after_pause(), f"{self}::pause"
            )

    async def _stop_after_pause(self) -> None:
        try:
            await asyncio.sleep(self.extra_pause_s)
        except asyncio.CancelledError:
            return
        self._pause_task = None
        await TurnAnalyzerUserTurnStopStrategy.trigger_user_turn_stopped(self)


class TurnTuner:
    """Applies a session's `TurnParams` to the live turn-taking objects of one pipeline."""

    def __init__(self, *, stop: PausedTurnStopStrategy, analyzer, controller):
        self.stop = stop
        self.analyzer = analyzer
        self.controller = controller
        self.applied = None

    def apply(self, params) -> None:
        self.stop.extra_pause_s = params.extra_pause_s
        # BaseSmartTurn compares its silence counter with `_stop_ms` on every audio chunk.
        self.analyzer._stop_ms = params.smart_turn_stop_secs * 1000
        # UserTurnController re-reads this on each wait of its stop-timeout loop.
        self.controller._user_turn_stop_timeout = params.user_turn_stop_timeout_s
        self.applied = params
