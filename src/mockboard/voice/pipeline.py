"""One voice session's Pipecat pipeline (ADR-007), shared by the local app and the tests:

    transport.input() -> VADProcessor(Silero) -> Prisma STT -> UserTurnProcessor(Smart Turn v3)
      -> BrainProcessor -> Timbre TTS -> transport.output()

Turn-taking comes from Settings and the session (`turn_params`, `strategies.py`): only Silero
starts a user turn, with V4's minimum-speech thresholds while the board is busy; Smart Turn v3
ends it without waiting for a transcript, because the brain then commits Prisma (ruling 28).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from mockboard.config import Settings
from mockboard.session import InterviewSession
from mockboard.voice.brain_processor import BrainProcessor
from mockboard.voice.lines import PlaybackLedger
from mockboard.voice.strategies import BoardAwareStartStrategy, PausedTurnStopStrategy, TurnTuner

SAMPLE_RATE = 16000  # Prisma and Timbre both take 16 kHz


@dataclass
class VoicePipeline:
    worker: PipelineWorker
    brain: BrainProcessor
    stt: Any
    tts: Any
    ledger: PlaybackLedger
    user_turn: UserTurnProcessor | None = None
    start_strategy: BoardAwareStartStrategy | None = None
    stop_strategy: PausedTurnStopStrategy | None = None
    tuner: TurnTuner | None = None
    processors: list[FrameProcessor] = field(default_factory=list)


def build_turn_taking(settings: Settings, session: InterviewSession, brain_ref: list):
    """Silero, the user-turn processor and the tuner. `brain_ref[0]` is filled in later
    (the start strategy asks the brain what the board is doing)."""
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(
        confidence=settings.voice_vad_confidence,
        start_secs=settings.voice_vad_start_secs,
        stop_secs=settings.voice_vad_stop_secs,
        min_volume=settings.voice_vad_min_volume,
    )))
    params = session.turn_params()
    analyzer = LocalSmartTurnAnalyzerV3(params=SmartTurnParams(
        stop_secs=params.smart_turn_stop_secs))
    stop = PausedTurnStopStrategy(turn_analyzer=analyzer, extra_pause_s=params.extra_pause_s)
    start = BoardAwareStartStrategy(
        board_phase=lambda: brain_ref[0].board_phase(),
        thresholds_ms=lambda: brain_ref[0].thresholds_ms(),
    )
    user_turn = UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(start=[start], stop=[stop]),
        user_turn_stop_timeout=params.user_turn_stop_timeout_s,
    )
    tuner = TurnTuner(stop=stop, analyzer=analyzer, controller=user_turn._user_turn_controller)
    return vad, user_turn, start, stop, tuner


def build_voice_pipeline(
    *,
    session: InterviewSession,
    settings: Settings,
    transport_input: FrameProcessor,
    transport_output: FrameProcessor,
    stt: FrameProcessor,
    tts: FrameProcessor,
    ledger: PlaybackLedger,
    report_dir: Path | None = None,
    observers: list | None = None,
    on_report=None,
    on_listen=None,
    autostart: bool = True,
    mic_recorder: FrameProcessor | None = None,
) -> VoicePipeline:
    brain_ref: list[BrainProcessor] = []
    vad, user_turn, start, stop, tuner = build_turn_taking(settings, session, brain_ref)
    brain = BrainProcessor(
        session, stt=stt, ledger=ledger, tuner=tuner, report_dir=report_dir,
        session_max_s=settings.voice_session_max_s, on_report=on_report, on_listen=on_listen,
        autostart=autostart,
        tts_idle_cap_s=settings.tts_first_byte_timeout_s * 2 + 3,
    )
    brain_ref.append(brain)
    processors = [transport_input, vad, stt, user_turn, brain, tts, transport_output]
    if mic_recorder is not None:  # ruling 35, opt-in: the candidate's audio as Prisma gets it
        processors.insert(1, mic_recorder)
    worker = PipelineWorker(
        Pipeline(processors),
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=settings.tts_sample_rate,  # ruling 38: TTS_SAMPLE_RATE
            enable_metrics=True,
        ),
        observers=observers or [],
        enable_rtvi=False,  # our page does not speak RTVI
        cancel_on_idle_timeout=False,  # a candidate may think in silence
    )
    return VoicePipeline(worker, brain, stt, tts, ledger, user_turn, start, stop, tuner,
                         processors)
