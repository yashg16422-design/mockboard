"""The panel on the voice loop (P7): the ritual before the opening question, the page's seats,
captions with a segment, the `answers` and `closing` events, and the 16 and 17-minute limits.

Same offline pipeline as `test_brain_processor.py`: fake STT, fake TTS, the fake brain. Timers are
shortened to fractions of a second; the real values are checked in `test_cutover.py`.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import UserStartedSpeakingFrame, VADUserStartedSpeakingFrame
from tests.voice.test_brain_processor import Rig, upsc_session

from mockboard.brain.panel_lines import CLOSE_LINES, RITUAL_LINES, WRAP_UP_CLOSE_LINES, line_for
from mockboard.modes.upsc_panel.setup import PanelSetup
from mockboard.voice.brain_processor import LISTENING

FIVE = ["Chairperson Sharma", "Shrimati Menon", "Doctor Iyer", "Professor Sen", "Shri Rathore"]
RITUAL = line_for(RITUAL_LINES, "hi-en")
CLOSE = line_for(CLOSE_LINES, "hi-en")
TIME_CLOSE = line_for(WRAP_UP_CLOSE_LINES, "hi-en")


def names(events):
    return [e["event"] for e in events]


async def test_the_ritual_is_said_first_and_the_seats_are_named_up_front(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        assert s.hooks._plan is not None  # the ritual call started the agenda plan
    first = rig.brain.events[0]
    assert first["event"] == "session"
    assert first["members"] == FIVE and first["max_answers"] is None
    captions = [e for e in rig.events("caption") if e["side"] == "board"]
    ritual = [c for c in captions if c["kind"] == "ritual"]
    opener = [c for c in captions if c["kind"] == "opener"]
    assert " ".join(c["text"] for c in ritual) == RITUAL and opener
    assert captions.index(ritual[-1]) < captions.index(opener[0])  # the ritual comes first
    assert {c["segment"] for c in captions} == {"form_hr"}
    # The opening question is not said twice: the ritual is not repeated inside it.
    assert RITUAL not in " ".join(c["text"] for c in opener)
    # The board's own record holds both, once, and the trace has one session_start.
    assert s.state.board_lines[0].text.startswith(RITUAL)
    rows = [json.loads(x) for x in s.recorder.path.read_text().splitlines()] if (
        s.recorder.path.exists()
    ) else []
    assert sum(r.get("event") == "session_start" for r in rows) == 1


async def test_an_answer_over_the_ritual_still_gets_the_opening_question_first(
    settings, cards, fake_llm, form, tmp_path
):
    """A candidate who talks over the ritual does not cancel the opening question's making: the
    session is opened before the turn is decided."""
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=200) as rig:  # a slow ritual
        await rig.until(lambda: rig.events("caption"))
        await rig.speak()
        await rig.until(lambda: len(rig.brain.outcomes) >= 1, timeout=15)
    assert s.state.board_lines[0].turn_id == "t0" and s.last_question
    assert rig.brain.outcomes[0].move.rule_id != "FALLBACK"


async def test_a_capped_interview_sends_answers_and_then_closing_before_the_close(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(
        settings, cards, fake_llm, form, tmp_path, mode_options=PanelSetup(max_answers=5)
    )
    async with Rig(s, tts_ms_per_word=5, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        for n in range(1, 6):
            await rig.speak(0.1)
            await rig.until(lambda n=n: len(rig.brain.outcomes) == n, timeout=15)
            if n < 5:
                await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    answers = rig.events("answers")
    assert [(a["used"], a["limit"], a["left"]) for a in answers] == [
        (n, 5, 5 - n) for n in range(1, 6)
    ]
    order = names(rig.brain.events)
    closing = [e for e in rig.brain.events if e["event"] == "closing"]
    assert [c["reason"] for c in closing] == ["agenda"]
    last_answers = max(i for i, n in enumerate(order) if n == "answers")
    at_closing = order.index("closing")
    close_caption = next(
        i for i, e in enumerate(rig.brain.events)
        if e["event"] == "caption" and e["side"] == "board" and e["text"] == CLOSE
    )
    assert last_answers < at_closing < close_caption < order.index("report")
    assert rig.events("session")[0]["max_answers"] == 5


async def test_a_hand_off_is_captioned_as_the_chairpersons_with_the_new_segment(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        for n in (1, 2):  # the Chairperson's second answer spends their budget
            await rig.speak(0.1)
            await rig.until(lambda n=n: len(rig.brain.outcomes) == n, timeout=15)
            await rig.until(lambda: rig.brain.state == LISTENING)
    board = [e for e in rig.events("caption") if e["side"] == "board" and e.get("segment") ==
             "home_district"]
    assert board and board[0]["speaker"] == "Chairperson Sharma"
    assert board[0]["kind"] == "preface"
    assert any(e["speaker"] == "Shrimati Menon" for e in board)
    users = [e for e in rig.events("caption") if e["side"] == "user"]
    assert [u["segment"] for u in users] == ["form_hr", "form_hr"]


async def test_the_wrap_up_is_requested_and_the_next_answer_closes_with_the_time_line(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, wrap_up_s=0.5, session_max_s=60.0,
                   report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.events("wrap_up"))
        assert s.hooks.wrap_up_due and not rig.events("time_up")
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak(0.1)  # an answer given after the wrap-up: nobody cuts it off
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    assert [e["reason"] for e in rig.events("closing")] == ["time"]
    events = rig.brain.events
    at = next(i for i, e in enumerate(events) if e["event"] == "closing")
    said = [e["text"] for e in events[at:] if e["event"] == "caption" and e["side"] == "board"]
    assert " ".join(said) == TIME_CLOSE and rig.events("report")
    assert not rig.events("time_up")  # the hard stop never came


async def test_the_hard_stop_with_a_silent_candidate_says_the_time_line_and_ends(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, wrap_up_s=0.3, session_max_s=1.5,
                   report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    order = names(rig.brain.events)
    assert order.index("wrap_up") < order.index("time_up") < order.index("closing")
    assert [e["reason"] for e in rig.events("closing")] == ["time"]
    confer = [e for e in rig.events("caption") if e.get("kind") == "confer"]
    assert " ".join(c["text"] for c in confer) == TIME_CLOSE


async def test_a_candidate_still_talking_at_the_hard_stop_is_treated_as_having_stopped(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, wrap_up_s=0.3, session_max_s=1.5,
                   report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())  # talking, and never stops
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    assert rig.events("time_up") and [e["reason"] for e in rig.events("closing")] == ["time"]


async def test_debate_keeps_its_single_stop_with_no_wrap_up(settings, cards, fake_llm, tmp_path):
    from mockboard.brain.deps import build_deps
    from mockboard.brain.graphs import TurnAnalyzer
    from mockboard.modes.debate.motions import Side, load_curated_motions
    from mockboard.modes.debate.session import DebateSetup
    from mockboard.obs.trace import TraceRecorder
    from mockboard.session import InterviewSession

    deps = build_deps(settings, fake_llm, cards=cards)
    motion = load_curated_motions()["smartphones_in_schools"]
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(tmp_path, "deb"), analyzer=TurnAnalyzer(deps),
        mode="debate", mode_options=DebateSetup(motion=motion, user_side=Side.for_),
    )
    assert s.time_limits() == (None, 600.0)
    assert s.panel_members() == [] and s.max_answers() is None and s.current_segment() == ""
    assert s.turn_events() == [] and s.opening_ritual() is None
    assert s.request_wrap_up() is False
    async with Rig(s, session_max_s=1.0, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    session_event = rig.events("session")[0]
    assert "members" not in session_event and "max_answers" not in session_event
    assert not rig.events("closing") and not rig.events("wrap_up")
    assert all("segment" not in e for e in rig.events("caption"))
