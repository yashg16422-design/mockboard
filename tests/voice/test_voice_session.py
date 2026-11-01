"""Session hooks for the voice loop (V3 item 2, CTO_NOTES_04 §7, ADR-008): `speak_stream`,
`finish_line`, `reopen_turn`, `note_late_final`, `turn_params`, and the actor text sink.
Pure brain code: no Pipecat needed, so these run in `make test`."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from mockboard.brain.actor import ACTOR_TEXT_SINK, FILLER_TEMPLATES, stream_capped
from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import LLMTransientError
from mockboard.brain.schemas import BoardMove, Move
from mockboard.modes.debate.motions import Side, load_curated_motions
from mockboard.modes.debate.session import DebateSetup
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession, TurnParams
from mockboard.voice.lines import PlaybackLedger, SentenceSplitter, split_sentences

ANSWER = "Kanpur mein traffic ek badi samasya hai kyunki sadkein sankri hain."


class SlowActor:
    """Wraps the fake LLM; actor streams come from `plan`: a list of (chunk, delay_s) pairs,
    where a chunk that is an exception is raised at that point."""

    def __init__(self, inner, plan):
        self.inner, self.plan = inner, list(plan)
        self.cancelled = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def structured(self, *a, **kw):
        return await self.inner.structured(*a, **kw)

    def stream_text(self, *, name, **kw):
        if name != "actor":
            return self.inner.stream_text(name=name, **kw)
        return self._stream()

    async def _stream(self):
        try:
            for chunk, delay in self.plan:
                await asyncio.sleep(delay)
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def make(settings, cards, llm, tmp_path, form=None, **kw):
    deps = build_deps(settings, llm, cards=cards)
    return InterviewSession(deps=deps, form=form, recorder=TraceRecorder(tmp_path, "v"),
                            analyzer=TurnAnalyzer(deps), session_id="v", **kw)


async def ended_turn(session):
    await session.open()
    session.submit_segment(ANSWER, segment_index=0)
    return await session.end_of_turn(30)


async def collect(stream):
    return [s async for s in stream]


def trace_lines(session):
    return [json.loads(x) for x in session.recorder.path.read_text().splitlines()]


# --------------------------------------------------------------------------- pure pieces


def test_splitter_keeps_decimals_and_waits_for_whitespace():
    sp = SentenceSplitter()
    assert sp.push("Aapne 12.") == []
    assert sp.push("5 lakh kaha. Kya yeh") == ["Aapne 12.5 lakh kaha."]
    assert sp.push(" sahi hai? ") == ["Kya yeh sahi hai?"]
    assert sp.flush() == ""
    assert split_sentences("यह सही है। ठीक? Ek minute... aap bataiye") == [
        "यह सही है।", "ठीक?", "Ek minute...", "aap bataiye",
    ]


async def test_actor_text_sink_sees_capped_chunks_only_in_its_own_task():
    async def chunks():
        for c in ["Ek ", "do ", "teen ", "char"]:
            yield c

    got: list[str] = []

    async def with_sink():
        ACTOR_TEXT_SINK.set(got.append)
        return [c async for c in stream_capped(chunks(), 3)]

    out = await asyncio.create_task(with_sink())
    assert "".join(got) == "".join(out) == "Ek do teen "
    assert ACTOR_TEXT_SINK.get() is None  # text mode: no sink
    assert [c async for c in stream_capped(chunks(), 9)]


def test_ledger_counts_whole_units_and_estimates_the_cut_one():
    t = [0.0]
    led = PlaybackLedger(clock=lambda: t[0])
    led.unit_started("Pehla vaakya poora hai.")  # 4 words
    led.audio(32000, 16000)  # 1000 ms
    led.unit_done()
    led.unit_started("Doosra vaakya aadha kat jayega yahan.")  # 6 words
    led.audio(32000, 16000)
    led.unit_done()
    led.bot_started()
    t[0] = 1.5  # 1.5 s played: unit 1 whole, half of unit 2
    heard = led.heard()
    assert heard.text == "Pehla vaakya poora hai. Doosra vaakya aadha"
    assert heard.estimated and heard.played_ms == 1500 and heard.audio_ms == 2000
    led.bot_stopped()
    t[0] = 9.0
    assert led.heard().played_ms == 1500  # nothing plays while stopped


def test_a_cut_unit_still_streaming_is_measured_against_its_estimated_length():
    # Ruling 38: one request per whole line means the cut unit usually has more audio to
    # come; its share is played time over an estimate of the whole (400 ms a word).
    t = [0.0]
    led = PlaybackLedger(clock=lambda: t[0])
    led.unit_started("Ek do teen chaar paanch chhe saat aath nau das.")  # 10 words, ~4 s
    led.audio(32000, 16000)  # only 1 s received so far
    led.bot_started()
    t[0] = 0.8  # 800 ms played: about 2 of 10 words, not 8 of 10
    heard = led.heard()
    assert heard.text == "Ek do" and heard.estimated


# --------------------------------------------------------------------------- speak_stream


async def test_streamed_line_yields_sentences_before_the_actor_finishes(
    settings, cards, fake_llm, form, tmp_path
):
    plan = [("Achha. ", 0.01), ("Traffic ke alawa ", 0.01), ("kya dekha? ", 0.01),
            ("Aur kuch ", 0.3), ("bataiye?", 0.01)]
    llm = SlowActor(fake_llm, plan)
    s = make(settings, cards, llm, tmp_path, form=form)
    await ended_turn(s)
    t0 = time.monotonic()
    seen = []
    async for sentence in s.speak_stream():
        seen.append((round(time.monotonic() - t0, 2), sentence))
    assert [x.text for _, x in seen] == ["Achha.", "Traffic ke alawa kya dekha?",
                                         "Aur kuch bataiye?"]
    assert seen[1][0] < 0.2  # streamed: not held back for the slow last sentence
    assert all(x.kind == "model" and x.speaker for _, x in seen)
    out = s.finish_line()
    assert out.line.text == "Achha. Traffic ke alawa kya dekha? Aur kuch bataiye?"
    assert s.last_question == out.line.text
    row = trace_lines(s)[-1]
    assert row["voice"]["line_mode"] == "streamed" and not row["voice"]["interrupted"]
    assert "actor_first_sentence" in row["stages_ms"]
    assert row["board_line"] == out.line.text


async def test_preface_goes_first_and_fact_challenges_are_buffered(
    settings, cards, fake_llm, form, tmp_path
):
    s = make(settings, cards, fake_llm, tmp_path, form=form)
    await ended_turn(s)
    s.turn.move = s.turn.move.model_copy(update={"preface": "acknowledge"})
    fake_llm.push("actor", "Ab yeh bataiye, iska hal kya hai?")
    got = await collect(s.speak_stream())
    assert got[0].kind == "preface" and got[0].text.startswith("Theek hai")
    assert [g.kind for g in got[1:]] == ["model"]
    s.finish_line()

    s.submit_segment(ANSWER, segment_index=0)
    await s.end_of_turn(30)
    s.turn.move = BoardMove(move=Move.fact_challenge, member=s.turn.move.member, pressure=2,
                            reason="test", rule_id="R4", for_turn=s.turn.turn_id,
                            target="missing-claim")
    got = await collect(s.speak_stream())
    assert got and all(g.kind == "line" for g in got)  # buffered: whole line, then split
    assert s.turn.line_mode == "buffered"
    row = s.finish_line()
    assert row.trace["voice"]["challenge_heard"] is True


async def test_a_stream_that_fails_midway_stops_and_asks_the_template_question(
    settings, cards, fake_llm, form, tmp_path
):
    plan = [("Sadkein sankri hain. ", 0.0), (LLMTransientError("stream broke"), 0.01)]
    s = make(settings, cards, SlowActor(fake_llm, plan), tmp_path, form=form)
    move = await ended_turn(s)
    got = await collect(s.speak_stream())
    assert got[0].text == "Sadkein sankri hain." and got[0].kind == "model"
    assert got[-1].kind == "question"
    assert got[-1].text == FILLER_TEMPLATES.get(move.move, FILLER_TEMPLATES[Move.probe])
    out = s.finish_line()
    assert out.trace["voice"]["question_appended"] and out.line.is_fallback


async def test_no_model_text_means_the_fallback_line_is_said(
    settings, cards, fake_llm, form, tmp_path
):
    s = make(settings, cards, fake_llm, tmp_path, form=form)
    await ended_turn(s)
    fake_llm.push("actor", LLMTransientError("down"))
    got = await collect(s.speak_stream())
    assert got and all(g.kind == "line" for g in got)
    out = s.finish_line()
    assert out.line.is_fallback and out.line.error


async def test_barge_in_records_only_the_heard_words(settings, cards, fake_llm, form, tmp_path):
    s = make(settings, cards, fake_llm, tmp_path, form=form)
    await ended_turn(s)
    before = s.last_question
    fake_llm.push("actor", "Pehli baat. Doosri baat kya hai?")
    await collect(s.speak_stream())
    out = s.finish_line("Pehli baat. Doosri", interrupted=True, spoken_estimated=True)
    assert s.last_question == "Pehli baat. Doosri" != before
    v = out.trace["voice"]
    assert v["interrupted"] and v["spoken_estimated"]
    assert v["board_line_generated"] == "Pehli baat. Doosri baat kya hai?"
    assert s.state.board_lines[-1].text == "Pehli baat. Doosri"

    # Nothing heard: the candidate is still answering the previous question.
    s.submit_segment(ANSWER, segment_index=0)
    await s.end_of_turn(30)
    fake_llm.push("actor", "Teesri baat?")
    await collect(s.speak_stream())
    lines_before = len(s.state.board_lines)
    s.finish_line("", interrupted=True)
    assert s.last_question == "Pehli baat. Doosri"
    assert len(s.state.board_lines) == lines_before
    assert len(s.state.moves) == 2  # the unheard move still counts (round 7)


async def test_cancelling_the_stream_cancels_the_actor(settings, cards, fake_llm, form, tmp_path):
    plan = [("Pehli baat. ", 0.0), ("Doosri ", 2.0), ("baat?", 0.0)]
    llm = SlowActor(fake_llm, plan)
    s = make(settings, cards, llm, tmp_path, form=form)
    await ended_turn(s)
    first = asyncio.Event()

    async def consume():
        async for _ in s.speak_stream():
            first.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.cancelled
    out = s.finish_line("Pehli", interrupted=True)
    assert out.trace["voice"]["actor_cancelled"] and out.line.text == "Pehli"


async def test_reopen_keeps_segments_and_chooses_the_move_again(
    settings, cards, fake_llm, form, tmp_path
):
    s = make(settings, cards, fake_llm, tmp_path, form=form)
    await ended_turn(s)
    s.mark("vad_end")
    turn_id = s.turn.turn_id
    s.reopen_turn()
    assert s.turn.move is None and len(s.turn.segments) == 1
    assert not [t for t in s.state.turns if t.turn_id == turn_id]
    s.submit_segment("Aur pollution bhi badh raha hai.", segment_index=1)
    await s.end_of_turn(30)
    assert [t.turn_id for t in s.state.turns].count(turn_id) == 1
    assert len(s.state.turns[-1].segments) == 2
    out = s.finish_line()
    assert out.trace["voice"]["turn_reopened"] == 1
    assert out.trace["reopened"][0]["stages_ms"]["end_of_turn"] >= 0


async def test_a_late_final_joins_the_closed_turn_without_a_new_turn(
    settings, cards, fake_llm, form, tmp_path
):
    s = make(settings, cards, fake_llm, tmp_path, form=form)
    await ended_turn(s)
    s.note_late_final("aur ek baat", commit_id=3)
    assert s.state.turns[-1].segments[-1].text == "aur ek baat"
    await collect(s.speak_stream())
    out = s.finish_line()
    assert out.trace["voice"]["late_finals"][0]["commit_id"] == 3
    assert s.turn is None  # no new turn was opened


# --------------------------------------------------------------------------- turn params


async def test_turn_params_follow_the_mode_hook(settings, cards, fake_llm, form, tmp_path):
    upsc = make(settings, cards, fake_llm, tmp_path, form=form)
    p = upsc.turn_params()
    assert isinstance(p, TurnParams) and not p.long_turn and p.extra_pause_s == 0
    assert p.smart_turn_stop_secs == settings.voice_smart_turn_stop_secs

    setup = DebateSetup(motion=load_curated_motions()["smartphones_in_schools"],
                        user_side=Side.for_)
    debate = make(settings, cards, fake_llm, tmp_path, mode="debate", mode_options=setup)
    await debate.open()
    p = debate.turn_params()  # the opening statement comes next
    assert p.long_turn and p.extra_pause_s == settings.voice_long_turn_extra_pause_s
    assert p.user_turn_stop_timeout_s == settings.voice_long_turn_stop_timeout_s


async def test_debate_guard_stops_a_streamed_line_at_an_uncarded_figure(
    settings, cards, fake_llm, tmp_path
):
    setup = DebateSetup(motion=load_curated_motions()["smartphones_in_schools"],
                        user_side=Side.for_)
    s = make(settings, cards, fake_llm, tmp_path, mode="debate", mode_options=setup)
    await s.open()
    s.submit_segment("Phones dhyan bhatkate hain kyunki notifications aate hain.",
                     segment_index=0)
    move = await s.end_of_turn(30)
    s.turn.move = move.model_copy(update={"move": Move.rebut, "target": "A1"})
    fake_llm.push("actor", "Aapki baat samajh aati hai. Studies show 40 percent agree. "
                           "Iska jawab kya hai?")
    got = await collect(s.speak_stream())
    assert got[0].text == "Aapki baat samajh aati hai."
    assert all("40" not in g.text for g in got)
    assert s.turn.guard_stopped == "Studies show 40 percent agree."
    s.finish_line()
