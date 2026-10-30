"""Barge-in basics (V4 core, ADR-008 §3, rulings 26 and 31), offline.

* The start strategy: a cough while the board speaks does not start a turn; real speech
  past the threshold does, and interrupts; before the board's first audio a shorter threshold
  applies; with the board silent a turn starts at once; transcripts never start a turn.
* The pipeline: an interruption stops TTS and playback, cancels the actor, and records only
  the words heard with `interrupted=true`; resumed speech before the first audio reopens the
  turn; a cough's transcript during the board's line is dropped.
* Per-turn parameters reach the live Smart Turn analyzer and user-turn controller.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pipecat")

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601
from tests.voice.harness import Harness
from tests.voice.test_brain_processor import Rig, SlowLLM, upsc_session
from tests.voice.test_voice_session import SlowActor

from mockboard.session import TurnParams
from mockboard.voice.brain_processor import BOARD, LISTENING
from mockboard.voice.strategies import (
    IDLE,
    SPEAKING,
    THINKING,
    BoardAwareStartStrategy,
    PausedTurnStopStrategy,
    TurnTuner,
)


def audio_frame():
    return InputAudioRawFrame(audio=b"\x00\x00" * 320, sample_rate=16000, num_channels=1)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


async def strategy(phase: str, clock: Clock):
    s = BoardAwareStartStrategy(board_phase=lambda: phase, thresholds_ms=lambda: (150, 500),
                                clock=clock)
    started = []
    s.add_event_handler("on_user_turn_started", lambda _s, params: started.append(params))
    return s, started


async def test_a_cough_while_the_board_speaks_does_not_start_a_turn():
    clock = Clock()
    s, started = await strategy(SPEAKING, clock)
    await s.process_frame(VADUserStartedSpeakingFrame())
    for _ in range(10):  # 300 ms of "speech"
        clock.t += 0.03
        await s.process_frame(audio_frame())
    await s.process_frame(VADUserStoppedSpeakingFrame())
    assert started == [] and s.ignored == [300.0]
    # Real speech: past 500 ms it counts, and the turn starts with interruptions on.
    await s.process_frame(VADUserStartedSpeakingFrame())
    for _ in range(20):
        clock.t += 0.03
        await s.process_frame(audio_frame())
    assert len(started) == 1 and started[0].enable_interruptions


async def test_thresholds_follow_what_the_board_is_doing():
    clock = Clock()
    idle, started_idle = await strategy(IDLE, clock)
    await idle.process_frame(VADUserStartedSpeakingFrame())
    assert len(started_idle) == 1  # the board is silent: at once

    thinking, started_thinking = await strategy(THINKING, clock)
    await thinking.process_frame(VADUserStartedSpeakingFrame())
    clock.t += 0.1
    await thinking.process_frame(audio_frame())
    assert not started_thinking
    clock.t += 0.06
    await thinking.process_frame(audio_frame())
    assert len(started_thinking) == 1  # 160 ms >= the 150 ms resume threshold

    # Transcripts never start a turn (ruling 26).
    s, started = await strategy(IDLE, clock)
    await s.process_frame(TranscriptionFrame("late final", "", time_now_iso8601()))
    assert not started


async def test_the_real_user_turn_processor_interrupts_only_past_the_threshold():
    stop = PausedTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())
    start = BoardAwareStartStrategy(board_phase=lambda: SPEAKING,
                                    thresholds_ms=lambda: (150, 400))
    utp = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(start=[start], stop=[stop]))
    async with Harness(utp) as h:
        await h.queue(VADUserStartedSpeakingFrame())
        await h.feed_audio(0.2)
        await h.queue(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(0.1)
        assert not h.collector.of_type(InterruptionFrame)  # a cough
        await h.queue(VADUserStartedSpeakingFrame())
        await h.feed_audio(0.6)
        assert await h.wait_until(lambda: h.collector.of_type(InterruptionFrame))
    assert len(start.ignored) == 1  # the cough


async def test_turn_params_reach_smart_turn_and_the_controller():
    analyzer = LocalSmartTurnAnalyzerV3()
    stop = PausedTurnStopStrategy(turn_analyzer=analyzer)
    utp = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(start=[], stop=[stop]))
    controller = utp._user_turn_controller
    assert hasattr(analyzer, "_stop_ms") and hasattr(controller, "_user_turn_stop_timeout")
    tuner = TurnTuner(stop=stop, analyzer=analyzer, controller=controller)
    tuner.apply(TurnParams(long_turn=True, smart_turn_stop_secs=5.0, extra_pause_s=1.2,
                           user_turn_stop_timeout_s=8.0, barge_in_min_speech_ms=500,
                           resume_min_speech_ms=150))
    assert analyzer._stop_ms == 5000 and stop.extra_pause_s == 1.2
    assert controller._user_turn_stop_timeout == 8.0


# --------------------------------------------------------------------------- pipeline


async def test_barge_in_stops_the_board_and_records_only_the_heard_words(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        rig.tts.ms_per_word = 250  # slow speech from here on, so there is time to cut in
        await rig.speak()
        await rig.until(lambda: rig.brain.state == BOARD
                        and rig.ledger.playback_started_at is not None)
        await asyncio.sleep(0.6)  # part of the line plays
        before = rig.playback.played_ms
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())
        await rig.worker.queue_frame(InterruptionFrame())
        await rig.until(lambda: rig.brain.outcomes)
        await asyncio.sleep(0.4)
        after_cut = rig.playback.played_ms
        await rig.worker.queue_frame(VADUserStoppedSpeakingFrame())
        await rig.worker.queue_frame(UserStoppedSpeakingFrame())
        await rig.until(lambda: len(rig.brain.outcomes) == 2, timeout=10)

    out = rig.brain.outcomes[0]
    v = out.trace["voice"]
    assert v["interrupted"] and v["spoken_estimated"]
    heard, sent = v["board_line_spoken"], v["board_line_sent"]
    assert heard and len(heard.split()) < len(sent.split())
    assert sent.startswith(heard.rsplit(" ", 1)[0])
    assert s.state.board_lines[1].text == heard  # what the candidate heard
    assert after_cut - before < 900  # playback stopped soon after the cut
    # The next turn answered the heard question, and it was a normal line.
    assert not rig.brain.outcomes[1].trace["voice"]["interrupted"]


async def test_barge_in_while_the_actor_is_still_generating_cancels_it(
    settings, cards, fake_llm, form, tmp_path
):
    llm = SlowActor(fake_llm, [("Pehli baat yeh hai. ", 0.0), ("Doosri ", 3.0), ("baat?", 0.0)])
    s = upsc_session(settings, cards, llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        rig.tts.ms_per_word = 250  # slow speech from here on, so there is time to cut in
        await rig.speak()
        await rig.until(lambda: rig.brain.state == BOARD
                        and rig.ledger.playback_started_at is not None)
        await rig.worker.queue_frame(UserStartedSpeakingFrame())
        await rig.worker.queue_frame(InterruptionFrame())
        await rig.until(lambda: rig.brain.outcomes)
    v = rig.brain.outcomes[0].trace["voice"]
    assert v["interrupted"] and v["actor_cancelled"] and llm.cancelled
    assert "Doosri" not in v["board_line_generated"]
    assert rig.tts.texts[-1] == "Pehli baat yeh hai."  # nothing after the cut reached TTS


async def test_resumed_speech_before_the_first_audio_reopens_the_turn(
    settings, cards, fake_llm, form, tmp_path
):
    llm = SlowLLM(fake_llm, actor_s=0.8)
    s = upsc_session(settings, cards, llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: s.turn is not None and s.turn.move is not None)
        assert rig.ledger.playback_started_at is None
        await rig.speak()  # the candidate goes on
        await rig.until(lambda: rig.brain.outcomes, timeout=10)
    out = rig.brain.outcomes[0]
    assert out.trace["voice"]["turn_reopened"] == 1 and not out.trace["voice"]["interrupted"]
    assert len(s.state.turns[0].segments) == 2 and rig.brain.events_of("turn_reopened")


async def test_a_coughs_transcript_during_the_board_line_is_dropped(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        rig.tts.ms_per_word = 250  # slow speech from here on, so there is time to cut in
        await rig.speak()
        await rig.until(lambda: rig.brain.state == BOARD
                        and rig.ledger.playback_started_at is not None)
        # Prisma transcribes a cough; no user turn started (under the threshold).
        await rig.worker.queue_frame(TranscriptionFrame(
            "hmm", "", time_now_iso8601(), result={"mb": {"seq": 99, "commit_id": None}}))
        await rig.until(lambda: rig.brain.outcomes)
    assert rig.brain.events_of("transcript_dropped")
    assert not rig.brain.outcomes[0].trace["voice"]["interrupted"]
    assert len(s.state.turns) == 1
