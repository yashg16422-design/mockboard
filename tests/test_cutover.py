"""P7, the cutover: UPSC is the panel. What changed for good, and what the page now hears.

The behaviour of the panel itself is in `test_panel_invariants.py`, `test_director.py` and the
rest. This file is for what only the cutover made true: no fixed topic list, the 17-minute limits,
the answer cap read from admission, the page's seats, captions with a segment and the right
speaker for a hand-off, and the `answers` and `closing` events.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from conftest import answer
from mockboard import config as config_module
from mockboard.access import AccessConfig
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.panel_lines import WRAP_UP_CLOSE_LINES, line_for
from mockboard.config import Settings
from mockboard.modes.upsc_panel.setup import PanelSetup
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession
from mockboard.speech import keyterms

PLAIN = (
    "Mujhe lagta hai ki gaon mein sadak ka kaam zaroori hai, kyunki isse rozgaar milta hai.",
    "Lekin kaam rokne ka asar bhi dekhna chahiye, isliye pehle kuch gaon mein try karna hoga.",
    "Skilling aur training se naukriyan badhti hain, aur logon ki aamdani bhi sudharti hai.",
    "Mera maanna hai ki local logon se baat karke pehle samasya samajhni chahiye.",
    "Main dono pakshon ki baat sunkar hi koi faisla lunga, aur uska asar bhi dekhunga.",
)


def make(settings, cards, form, tmp_path, *, cap=None, sid="cut") -> InterviewSession:
    deps = build_deps(settings, make_fake_llm(cards), cards=cards)
    options = PanelSetup(max_answers=cap)
    return InterviewSession(
        deps=deps, form=form, recorder=TraceRecorder(tmp_path, sid, enabled=False),
        analyzer=TurnAnalyzer(deps), session_id=sid, mode_options=options,
    )


# --- no fixed topics (A2) -----------------------------------------------------------------------


def test_no_topic_list_in_settings_or_source():
    assert "topics" not in Settings.model_fields
    for module in (config_module, keyterms):
        source = inspect.getsource(module).lower()
        assert "tourism" not in source and "internal_security" not in source, module.__name__


# --- the 17-minute interview ---------------------------------------------------------------------


def test_the_panel_wraps_up_at_16_minutes_and_stops_at_17_and_debate_keeps_its_ten():
    s = Settings(_env_file=None)
    assert s.time_limits("upsc_interview") == (960.0, 1020.0)
    assert s.time_limits("debate") == (None, 600.0)
    assert not hasattr(s, "voice_session_max_s")  # the old shared 600 s is gone


def test_a_wrap_up_after_the_hard_stop_is_refused():
    with pytest.raises(ValidationError, match="voice_wrap_up_s"):
        Settings(_env_file=None, voice_wrap_up_s=1020, voice_hard_stop_s=1020)


def test_prisma_rotates_before_the_session_ends_and_before_its_own_cap():
    """The interview is now longer than Prisma's 15-minute stream cap, so the socket must be moved
    inside it: at 12 minutes when the candidate is quiet, at 14 whatever they do. Both come before
    the wrap-up (16) and the hard stop (17)."""
    s = Settings(_env_file=None)
    assert s.stt_rotate_after_s < s.stt_rotate_hard_s < 900 < s.voice_hard_stop_s
    assert s.stt_rotate_hard_s < s.voice_wrap_up_s
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_rotate_after_s=800, stt_rotate_hard_s=700)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_rotate_hard_s=900)  # the cap itself


def test_the_answer_cap_cannot_be_set_below_what_the_panel_needs(monkeypatch):
    assert AccessConfig.from_env({}).max_answers == 10
    assert AccessConfig.from_env({"ACCESS_MAX_ANSWERS": "5"}).max_answers == 5
    with pytest.raises(ValueError, match="ACCESS_MAX_ANSWERS"):
        AccessConfig.from_env({"ACCESS_MAX_ANSWERS": "4"})


# --- what the page is told ------------------------------------------------------------------------


async def test_the_seats_are_the_five_members_in_agenda_order(settings, cards, form, tmp_path):
    session = make(settings, cards, form, tmp_path)
    assert session.panel_members() == [
        "Chairperson Sharma", "Doctor Iyer", "Shri Rathore", "Shrimati Menon", "Professor Sen",
    ]
    assert session.current_segment() == "form_hr"
    await session.close()


async def test_a_hand_off_is_the_chairpersons_and_the_question_the_next_members(
    settings, cards, form, tmp_path
):
    """The caption names who says each part, and every sentence carries the segment it is in."""
    session = make(settings, cards, form, tmp_path)
    await session.open()
    spoken = []
    for text in PLAIN[:2]:  # the second answer spends the Chairperson's two-question budget
        session.submit_segment(text, segment_index=0)
        await session.end_of_turn(30.0)
        spoken = [s async for s in session.speak_stream()]
        session.finish_line()
    kinds = {(s.speaker, s.kind) for s in spoken}
    assert ("Chairperson Sharma", "preface") in kinds  # "Dhanyavaad. Doctor Iyer, please."
    assert any(s.speaker == "Doctor Iyer" and s.kind in ("model", "line") for s in spoken)
    assert spoken[0].speaker == "Chairperson Sharma" and "Iyer" in spoken[0].text + spoken[1].text
    assert {s.segment for s in spoken} == {"degree"}
    assert [s.index for s in spoken] == list(range(len(spoken)))
    await session.close()


async def test_a_capped_interview_counts_its_answers_and_closes_after_the_last(
    settings, cards, form, tmp_path
):
    session = make(settings, cards, form, tmp_path, cap=5)
    assert session.max_answers() == 5
    await session.open()
    seen: list[tuple[str, dict]] = []
    for text in PLAIN:
        session.submit_segment(text, segment_index=0)
        await session.end_of_turn(30.0)
        seen += session.turn_events()  # read before the line is spoken, as the voice loop does
        async for _ in session.speak_stream():
            pass
        session.finish_line()
    assert [e for e in seen if e[0] == "answers"] == [
        ("answers", {"used": n, "limit": 5, "left": 5 - n}) for n in range(1, 6)
    ]
    assert [e for e in seen if e[0] == "closing"] == [("closing", {"reason": "agenda"})]
    # `answers` for the last answer first, then `closing`, then the Chairperson's close.
    assert [name for name, _ in seen][-2:] == ["answers", "closing"]
    assert session.state.finished
    await session.close()


async def test_an_uncapped_interview_never_sends_answers(settings, cards, form, tmp_path):
    session = make(settings, cards, form, tmp_path, cap=None)
    assert session.max_answers() is None
    await session.open()
    names = []
    for text in PLAIN[:3]:
        session.submit_segment(text, segment_index=0)
        await session.end_of_turn(30.0)
        names += [n for n, _ in session.turn_events()]
        async for _ in session.speak_stream():
            pass
        session.finish_line()
    assert names == []
    await session.close()


async def test_a_repeat_is_not_an_answer_and_sends_no_answers_event(
    settings, cards, form, tmp_path
):
    session = make(settings, cards, form, tmp_path, cap=10)
    await session.open()
    await answer(session, PLAIN[0], think_s=0)
    session.submit_segment("please repeat", segment_index=0)
    await session.end_of_turn(30.0)
    assert session.turn_events() == []
    async for _ in session.speak_stream():
        pass
    session.finish_line()
    session.submit_segment(PLAIN[1], segment_index=0)
    await session.end_of_turn(30.0)
    assert session.turn_events()[0] == ("answers", {"used": 2, "limit": 10, "left": 8})
    await session.close()


async def test_the_time_close_is_said_when_the_wrap_up_was_asked_and_nothing_closed(
    settings, cards, form, tmp_path
):
    """The hard stop with a silent candidate: no turn closed the interview, so the Chairperson's
    time line is the confer line. Before a wrap-up there is none, and neither is there one after
    a close."""
    session = make(settings, cards, form, tmp_path)
    await session.open()
    assert session.closing_line() is None
    assert session.request_wrap_up() is True
    assert session.closing_line() == line_for(WRAP_UP_CLOSE_LINES, "hi-en")
    await session.close()


async def test_a_wrap_up_closes_with_the_next_finished_turn(settings, cards, form, tmp_path):
    session = make(settings, cards, form, tmp_path)
    await session.open()
    await answer(session, PLAIN[0], think_s=0)
    assert session.request_wrap_up() is True
    session.submit_segment(PLAIN[1], segment_index=0)
    await session.end_of_turn(30.0)
    events = session.turn_events()
    assert events == [("closing", {"reason": "time"})]
    async for _ in session.speak_stream():
        pass
    out = session.finish_line()
    assert out.finished and out.line.text == line_for(WRAP_UP_CLOSE_LINES, "hi-en")
    assert session.closing_line() is None  # the close was said; nothing more to confer
    await session.close()
