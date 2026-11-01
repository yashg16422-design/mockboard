"""Offline pipeline tests for the BrainProcessor (V3 item 3; ADR-007/008, rulings 26, 28, 31).

The pipeline is [input -> fake STT -> BrainProcessor -> fake TTS -> fake playback], with the
FakeLLM brain. The user-turn processor's output (`UserStarted/StoppedSpeakingFrame`) and
Silero's VAD frames are injected, so each test controls exactly when the candidate speaks.
No network, no microphone. Skipped without the `voice` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import (
    EndFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.utils.time import time_now_iso8601
from pipecat.workers.runner import WorkerRunner
from tests.voice.harness import Collector

from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession
from mockboard.speech.prisma_stt import CommitResult
from mockboard.voice.brain_processor import LISTENING, BrainProcessor
from mockboard.voice.fakes import FakeCommitSTT, FakePlayback, FakeTTSService
from mockboard.voice.lines import PlaybackLedger


class PassThrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class SlowLLM:
    """The fake LLM with a delay on one call name (listener or actor)."""

    def __init__(self, inner, *, listener_s: float = 0.0, actor_s: float = 0.0):
        self.inner, self.listener_s, self.actor_s = inner, listener_s, actor_s

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def structured(self, *a, **kw):
        if kw.get("name") == "listener" and self.listener_s:
            await asyncio.sleep(self.listener_s)
        return await self.inner.structured(*a, **kw)

    def stream_text(self, *, name, **kw):
        inner = self.inner.stream_text(name=name, **kw)
        if name != "actor" or not self.actor_s:
            return inner
        return self._delayed(inner)

    async def _delayed(self, inner):
        await asyncio.sleep(self.actor_s)
        async for chunk in inner:
            yield chunk


class Rig:
    """Runs the offline voice pipeline for one session."""

    def __init__(self, session, *, stt=None, tts_ms_per_word=60, deadline_s=5.0,
                 report_dir=None, session_max_s=None, tts_request_unit=None, wrap_up_s=None):
        self.session = session
        self.ledger = PlaybackLedger()
        self.stt = stt or FakeCommitSTT(segment_delay_s=10.0)
        self.tts = FakeTTSService(ledger=self.ledger, ms_per_word=tts_ms_per_word)
        self.playback = FakePlayback()
        self.collector = Collector()
        self.brain = BrainProcessor(session, stt=self.stt, ledger=self.ledger,
                                    deadline_s=deadline_s, settle_s=0.05,
                                    report_dir=report_dir, session_max_s=session_max_s,
                                    wrap_up_s=wrap_up_s, tts_request_unit=tts_request_unit)
        self.worker = PipelineWorker(
            Pipeline([PassThrough(), self.stt, self.brain, self.tts, self.playback,
                      self.collector]),
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
            enable_rtvi=False, cancel_on_idle_timeout=False,
        )

    async def __aenter__(self):
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(self.worker)
        self._run = asyncio.create_task(runner.run())
        return self

    async def __aexit__(self, *exc):
        await self.worker.queue_frame(EndFrame())
        try:
            await asyncio.wait_for(self._run, timeout=10)
        except TimeoutError:
            await self.worker.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._run, timeout=5)

    async def until(self, predicate, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out; brain state={self.brain.state}")

    async def speak(self, seconds: float = 0.3):
        """The candidate says something: what Silero and the user-turn processor emit."""
        await self.worker.queue_frame(VADUserStartedSpeakingFrame())
        await self.worker.queue_frame(UserStartedSpeakingFrame())
        await asyncio.sleep(seconds)
        await self.worker.queue_frame(VADUserStoppedSpeakingFrame())
        await self.worker.queue_frame(UserStoppedSpeakingFrame())

    def events(self, kind):
        return [e for e in self.brain.events if e["event"] == kind]


def upsc_session(settings, cards, llm, form, tmp_path, **kw):
    deps = build_deps(settings, llm, cards=cards)
    return InterviewSession(deps=deps, form=form, recorder=TraceRecorder(tmp_path, "vs"),
                            analyzer=TurnAnalyzer(deps), session_id="vs", **kw)


ORDER = ["vad_end", "commit_sent", "final_transcript_0", "turn_end", "end_of_turn",
         "move_ready", "actor_first_sentence", "tts_first_byte", "playback_start"]


async def test_a_turn_runs_in_order_with_every_mark_and_ends_with_a_report(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)  # the opener played
        opener = rig.tts.texts[:]
        assert opener and s.state.board_lines[0].text.startswith(opener[0][:10])
        await rig.speak()
        await rig.until(lambda: len(rig.brain.outcomes) == 1)
        await rig.until(lambda: rig.brain.state == LISTENING)
        s.request_wrap_up()  # the panel's time is nearly up: the next answer is the last
        await rig.speak()
        await rig.until(lambda: rig.brain.report is not None, timeout=15)

    first = rig.brain.outcomes[0].trace
    stages = first["stages_ms"]
    times = [stages[k] for k in ORDER]
    assert times == sorted(times), dict(zip(ORDER, times, strict=True))
    v = first["voice"]
    assert not v["interrupted"] and v["board_line_spoken"] == v["board_line_sent"]
    assert v["commit"]["reply"] == "transcript" and v["commit"]["settled"]
    assert v["played_ms"] > 0 and v["tts_units"]
    # Each sentence went to TTS as its own request, in order (ADR-008 §1-2).
    said = [e["text"] for e in rig.events("caption") if e["side"] == "board"]
    assert rig.tts.texts == said
    users = [e for e in rig.events("caption") if e["side"] == "user"]
    assert len(users) == 2 and users[0]["text"].startswith("Main Kanpur se hoon")
    # The session ended at the wrap-up: the report is on the page and on disk.
    report = rig.events("report")[0]
    assert "REPORT" in report["text"] and (tmp_path / "reports" / "vs.report.txt").exists()
    msgs = rig.collector.of_type(OutputTransportMessageUrgentFrame)
    assert msgs and all(m.message["type"] == "mockboard" for m in msgs)


async def test_the_move_deadline_holds_when_the_listener_is_slow(
    settings, cards, fake_llm, form, tmp_path
):
    llm = SlowLLM(fake_llm, listener_s=1.5)
    s = upsc_session(settings, cards, llm, form, tmp_path)
    async with Rig(s, deadline_s=0.3) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: rig.brain.outcomes)
    trace = rig.brain.outcomes[0].trace
    assert "deadline 0.3s" in trace["fallback_reason"]
    stages = trace["stages_ms"]
    assert stages["move_ready"] - stages["turn_end"] < 600  # 0.3 s deadline plus slack


class LateSTT(FakeCommitSTT):
    """Prisma's own VAD closed the segment before the stop; the commit's own transcript is
    slower than the bounded wait and lands while the board is already speaking."""

    async def commit(self, *, wait_s=None, noop_s=None):
        self._commits += 1
        cid = self._commits
        if self._open:
            await self._emit(None)  # the segment that closed before the commit

        async def late():
            await asyncio.sleep(0.4)
            self._seq += 1
            await self.push_frame(TranscriptionFrame(
                text="aur ek baat", user_id="", timestamp=time_now_iso8601(),
                result={"mb": {"seq": self._seq, "commit_id": cid}}))

        asyncio.get_running_loop().create_task(late())
        now = time.monotonic()
        return CommitResult(cid, "timeout", [], None, 0, True, now, now, now)


async def test_a_late_final_joins_its_turn_and_never_interrupts_the_board(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, stt=LateSTT(segment_delay_s=10.0), tts_ms_per_word=150) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: rig.brain.outcomes)
    out = rig.brain.outcomes[0]
    assert out.trace["voice"]["late_finals"][0]["words"] == 3
    assert s.state.turns[0].segments[-1].text == "aur ek baat"
    assert not out.trace["voice"]["interrupted"]
    assert not rig.collector.of_type(InterruptionFrame)
    assert not rig.events("transcript_dropped") and len(s.state.turns) == 1


async def test_a_commit_with_no_words_returns_to_listening(
    settings, cards, fake_llm, form, tmp_path
):
    class Silent(FakeCommitSTT):
        async def commit(self, *, wait_s=None, noop_s=None):
            self._open = False
            now = time.monotonic()
            return CommitResult(1, "empty", [], None, 0, True, now, now, now)

    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, stt=Silent(segment_delay_s=10.0)) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: rig.events("no_speech"))
        await rig.until(lambda: rig.brain.state == LISTENING)
    assert s.turn is None and not rig.brain.outcomes


async def test_speech_lost_ends_the_session_with_a_report(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.brain.speech_lost("Prisma reconnect failed 5 times")
        await rig.until(lambda: rig.brain.report is not None, timeout=10)
    err = rig.events("error")[0]
    assert "lost" in err["text"] and rig.events("report")


async def test_the_real_pipeline_is_assembled_with_our_turn_taking(
    settings, cards, fake_llm, form, tmp_path
):
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.turns.user_turn_processor import UserTurnProcessor

    from mockboard.voice.pipeline import build_voice_pipeline
    from mockboard.voice.strategies import BoardAwareStartStrategy, PausedTurnStopStrategy

    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    ledger = PlaybackLedger()
    vp = build_voice_pipeline(
        session=s, settings=settings, transport_input=PassThrough(),
        transport_output=PassThrough(), stt=FakeCommitSTT(),
        tts=FakeTTSService(ledger=ledger), ledger=ledger,
    )
    kinds = [type(p) for p in vp.processors]
    assert kinds[1] is VADProcessor and kinds[3] is UserTurnProcessor
    assert kinds[4] is BrainProcessor and isinstance(vp.processors[2], FakeCommitSTT)
    strategies = vp.user_turn._user_turn_controller.user_turn_strategies
    assert [type(x) for x in strategies.start] == [BoardAwareStartStrategy]  # no transcript
    assert [type(x) for x in strategies.stop] == [PausedTurnStopStrategy]
    assert strategies.stop[0].wait_for_transcript is False  # the brain commits (ruling 28)
    assert vp.brain.board_phase() == "speaking" or vp.brain.board_phase() == "thinking"


async def test_the_session_time_cap_ends_the_interview_with_a_report(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, session_max_s=1.0) as rig:  # regression: the timer used to collide
        await rig.until(lambda: rig.brain.report is not None, timeout=12)  # with Pipecat's
    assert rig.brain.events_of("time_up") and rig.brain.events_of("report")  # _clock


# --------------------------------------------------------------------------- defect F


async def test_stop_while_the_board_speaks_gives_a_report_with_the_heard_words(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        rig.tts.ms_per_word = 250
        await rig.speak()
        await rig.until(lambda: rig.brain.state == "speaking"
                        and rig.ledger.playback_started_at is not None)
        await asyncio.sleep(0.3)
        await rig.brain.request_stop()
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    v = rig.brain.outcomes[0].trace["voice"]
    assert v["interrupted"] and v["stopped"]
    assert rig.events("stopping") and rig.events("report")
    assert (tmp_path / "reports" / "vs.report.txt").exists()
    assert rig.events("caption")[-1].get("kind") != "verdict"  # nothing more was spoken


async def test_stop_mid_answer_keeps_the_answer_for_the_judge(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()  # first answer, normal turn
        await rig.until(lambda: rig.brain.outcomes and rig.brain.state == LISTENING)
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0.2)
        await rig.worker.queue_frame(VADUserStoppedSpeakingFrame())  # Prisma closes it
        await rig.until(lambda: s.turn is not None and s.turn.segments, timeout=12)
        await rig.brain.request_stop()
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    assert len(s.state.turns) == 2
    last = rig.brain.outcomes[-1].trace["voice"]
    assert last["board_silent"] and last["board_line_spoken"] == ""


async def test_stop_before_any_answer_ends_without_a_report(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.brain.request_stop()
        await rig.until(lambda: rig.events("ended"))
    assert rig.brain.report is None and rig.events("ended")[0]["reason"] == "no_answers"


async def test_a_closed_tab_still_saves_the_report(settings, cards, fake_llm, form, tmp_path):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: rig.brain.outcomes and rig.brain.state == LISTENING)
        await rig.brain.finish_without_page()
    assert rig.brain.report is not None and (tmp_path / "reports" / "vs.report.json").exists()



# --------------------------------------------------------------------------- ruling 34


class NoneCommitSTT(FakeCommitSTT):
    """The first real run (Round 20, trace voice-32a441f916): every commit replied "none" in
    ~80 ms without closing anything, and Prisma's own VAD sent the final ~1.3 s after the
    candidate stopped. This replays that event order."""

    async def commit(self, *, wait_s=None, noop_s=None):
        self._commits += 1
        queued = time.monotonic()
        await asyncio.sleep(0.08)
        return CommitResult(self._commits, "none", [], None, 0, None, queued, queued,
                            time.monotonic())


async def test_a_none_commit_waits_for_the_final_that_comes_1_3_s_later(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    stt = NoneCommitSTT(segment_delay_s=1.3)
    async with Rig(s, stt=stt) as rig:
        rig.brain._final_wait_s = 1.8
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: rig.brain.outcomes, timeout=10)
    assert not rig.events("no_speech") and not rig.events("transcript_dropped")
    assert len(s.state.turns) == 1 and s.state.turns[0].segments
    commit = rig.brain.outcomes[0].trace["voice"]["commit"]
    assert commit["reply"] == "none" and commit["settled"]
    assert 1200 <= commit["final_wait_ms"] < 1800  # ended when the final came, not at the cap
    users = [e for e in rig.events("caption") if e["side"] == "user"]
    assert len(users) == 1 and not users[0].get("late")


async def test_a_final_slower_than_the_budget_joins_its_turn_late(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    stt = NoneCommitSTT(segment_delay_s=0.9)
    async with Rig(s, stt=stt, tts_ms_per_word=200) as rig:
        rig.brain._final_wait_s = 0.4
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()  # the first answer's final comes after the wait: no words yet
        await rig.until(lambda: rig.events("no_speech"))
        await rig.until(lambda: rig.brain.outcomes, timeout=10)  # it then opens the turn
        await rig.until(lambda: rig.brain.state == LISTENING)
        # Second answer: Prisma had already sent part of it; the rest is late.
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0.2)
        await rig.worker.queue_frame(VADUserStoppedSpeakingFrame())
        await asyncio.sleep(1.0)  # its first segment's final arrives (0.9 s)
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(0.2)
        await rig.worker.queue_frame(VADUserStoppedSpeakingFrame())
        await rig.worker.queue_frame(UserStoppedSpeakingFrame())
        await rig.until(lambda: len(rig.brain.outcomes) == 2, timeout=10)
        await asyncio.sleep(1.0)
    second = rig.brain.outcomes[1].trace["voice"]
    assert not second["commit"]["settled"]
    assert not rig.events("transcript_dropped")  # the late final was not thrown away
    assert any(e.get("late") for e in rig.events("caption") if e["side"] == "user")
    assert len(s.state.turns) == 2 and len(s.state.turns[1].segments) == 2


@pytest.mark.parametrize("unit", ["line", "first_rest"])
async def test_ruling_38_whole_line_requests(unit, settings, cards, fake_llm, form, tmp_path):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_request_unit=unit) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: len(rig.brain.outcomes) == 1)
    captions = [e for e in rig.events("caption") if e["side"] == "board"]
    ritual = [e["text"] for e in captions if e["kind"] == "ritual"]
    opener = [e["text"] for e in captions if e["kind"] == "opener"]
    turn = [e["text"] for e in captions if e["kind"] not in ("ritual", "opener")]
    line = rig.brain.outcomes[0].trace["voice"]
    assert len(ritual) >= 2  # "Namaste." and "Please be seated.", captioned one by one
    assert opener and turn
    if unit == "line":  # one request each for the ritual, the opening question and the turn
        assert rig.tts.texts == [" ".join(ritual), " ".join(opener), " ".join(turn)]
    else:  # the first sentence alone, then the rest of each line
        assert rig.tts.texts[:2] == [ritual[0], " ".join(ritual[1:])]
        assert rig.tts.texts[2] == opener[0]
        assert rig.tts.texts[3] == turn[0]
    assert not line["interrupted"] and line["board_line_spoken"] == line["board_line_sent"]


def test_an_unknown_request_unit_is_refused(settings, cards, fake_llm, form, tmp_path):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    with pytest.raises(ValueError, match="tts_request_unit"):
        BrainProcessor(s, tts_request_unit="word")
