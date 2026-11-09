"""The panel on the voice loop (P7): the ritual before the opening question, the page's seats,
captions with a segment, the `answers` and `closing` events, and the 16 and 17-minute limits.

Same offline pipeline as `test_brain_processor.py`: fake STT, fake TTS, the fake brain. Timers are
shortened to fractions of a second; the real values are checked in `test_cutover.py`.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import (
    InterruptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from tests.voice.test_brain_processor import Rig, upsc_session

from mockboard.brain.panel_lines import CLOSE_LINES, RITUAL_LINES, WRAP_UP_CLOSE_LINES, line_for
from mockboard.modes.upsc_panel.setup import PanelSetup
from mockboard.voice.brain_processor import BOARD, LISTENING

# The seats in panel order since Nov 9 (form, degree, case study, district, hobbies).
FIVE = ["Chairperson Sharma", "Doctor Iyer", "Shri Rathore", "Shrimati Menon", "Professor Sen"]
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
             "degree"]
    assert board and board[0]["speaker"] == "Chairperson Sharma"
    assert board[0]["kind"] == "preface"
    assert any(e["speaker"] == "Doctor Iyer" for e in board)
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


async def test_a_candidate_who_talks_on_past_the_grace_at_the_hard_stop_is_treated_as_stopped(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, wrap_up_s=0.3, session_max_s=1.5, hard_stop_grace_s=0.4,
                   report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())  # talking, and never stops
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    assert rig.events("time_up") and [e["reason"] for e in rig.events("closing")] == ["time"]
    assert [e["seconds"] for e in rig.events("grace_over")] == [0.4]
    order = names(rig.brain.events)
    assert order.index("time_up") < order.index("grace_over") < order.index("closing")


async def test_a_candidate_mid_sentence_at_the_hard_stop_finishes_and_is_closed_after_the_answer(
    settings, cards, fake_llm, form, tmp_path
):
    """P9: the hard stop does not cut a sentence. The turn ends when the candidate stops, their
    words are in the answer, and the Chairperson's time line closes the interview after it."""
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, wrap_up_s=0.3, session_max_s=2.0, hard_stop_grace_s=5.0,
                   report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())
        await rig.until(lambda: rig.events("time_up"), timeout=5)  # the hard stop, mid-sentence
        await asyncio.sleep(0.5)  # still talking after it
        assert rig.brain.state == "user" and not rig.events("grace_over")
        assert not [e for e in rig.events("state") if e["state"] == "closing"]  # not cut
        stopped_at = time.monotonic()
        await rig.worker.queue_frame(VADUserStoppedSpeakingFrame())
        await rig.worker.queue_frame(UserStoppedSpeakingFrame())
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
    assert not rig.events("grace_over")
    first_close = next(e for e in rig.events("state") if e["state"] == "closing")
    assert first_close["t"] >= stopped_at
    assert len(rig.brain.outcomes) == 1 and s.state.turns[0].transcript  # their words are kept
    assert [e["reason"] for e in rig.events("closing")] == ["time"]
    said = " ".join(e["text"] for e in rig.events("caption")
                    if e["side"] == "board" and e["t"] > first_close["t"])
    assert said == TIME_CLOSE


async def test_a_board_line_in_progress_at_the_hard_stop_is_finished_then_the_time_line_follows(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=200, wrap_up_s=0.3, session_max_s=0.8,
                   report_dir=tmp_path / "reports") as rig:  # a slow opener: still talking at 0.8 s
        await rig.until(lambda: rig.brain.report is not None, timeout=30)
    board = [e["text"] for e in rig.events("caption") if e["side"] == "board"]
    ritual_and_opener = " ".join(board[: len(board) - len(TIME_CLOSE.split(". ")) - 1])
    assert "Please be seated" in ritual_and_opener  # the ritual was not cut short
    assert " ".join(board).endswith(TIME_CLOSE)
    assert [e["reason"] for e in rig.events("closing")] == ["time"]


async def test_the_close_cannot_be_interrupted_and_a_question_before_it_still_can(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        assert rig.brain.board_phase() == "idle"
        rig.tts.ms_per_word = 120
        s.request_wrap_up()
        await rig.speak(0.1)  # the answer after the wrap-up: the close is the next line
        await rig.until(lambda: rig.events("closing"), timeout=15)
        assert rig.brain.board_phase() == "locked"  # from the `closing` event on
        await rig.until(lambda: rig.brain.report is not None, timeout=30)
        assert rig.brain.board_phase() == "locked"  # and while the report is made
    assert rig.brain.board_phase() == "locked"


async def test_a_question_that_is_not_a_close_is_not_locked(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=200) as rig:
        await rig.until(lambda: rig.ledger.playback_started_at is not None, timeout=10)
        assert rig.brain.board_phase() in ("speaking", "thinking")  # the ritual: a candidate may
        assert not rig.events("closing")  # cut in on it, as before P9


async def test_a_debates_verdict_keeps_the_thresholds_it_had(
    settings, cards, fake_llm, tmp_path
):
    from mockboard.brain.deps import build_deps
    from mockboard.brain.graphs import TurnAnalyzer
    from mockboard.modes.debate.motions import Side, load_curated_motions
    from mockboard.modes.debate.session import DebateSetup
    from mockboard.obs.trace import TraceRecorder
    from mockboard.session import InterviewSession
    from mockboard.voice.brain_processor import FINISHED

    deps = build_deps(settings, fake_llm, cards=cards)
    motion = load_curated_motions()["smartphones_in_schools"]
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(tmp_path, "deb2"),
        analyzer=TurnAnalyzer(deps), mode="debate",
        mode_options=DebateSetup(motion=motion, user_side=Side.for_),
    )
    async with Rig(s, session_max_s=1.0, report_dir=tmp_path / "reports") as rig:
        await rig.until(lambda: rig.brain.report is not None, timeout=15)
        assert rig.brain.state == FINISHED
        assert rig.brain.board_phase() in ("speaking", "thinking")  # never "locked"


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


# --- P8: a voice per member ------------------------------------------------------------------

VOICES = {"chairman": "Vikrant", "economist": "Poorvi", "ips_officer": "Urmila",
          "administrator": "Poorvi", "humanities": "Urmila"}
IDS = dict(zip(FIVE, VOICES, strict=True))  # display name -> persona id, in seat order


def picker(member: str) -> str | None:
    return VOICES.get(member)


def voices_follow_speakers(rig) -> None:
    """Every request sent to TTS was in the voice of the member whose caption it is. Requests are
    a subsequence of the captions (a cut line never reaches TTS), so each is matched to the next
    caption with its text: the same words can be said by two members ("Namaste.")."""
    captions = [(e["text"], VOICES[IDS[e["speaker"]]])
                for e in rig.events("caption") if e["side"] == "board"]
    at = 0
    for voice, text in rig.tts.requests:
        while at < len(captions) and captions[at][0] != text:
            at += 1
        assert at < len(captions), f"no caption for the request {text!r}"
        assert captions[at][1] == voice, (text, voice, captions[at][1])
        at += 1


async def test_every_request_is_in_the_voice_of_the_member_who_says_it(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, voice_for=picker) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        for n in (1, 2):  # the second answer spends the Chairperson's budget: a hand-off
            await rig.speak(0.1)
            await rig.until(lambda n=n: len(rig.brain.outcomes) == n, timeout=15)
            await rig.until(lambda: rig.brain.state == LISTENING)
    assert rig.tts.requests
    voices_follow_speakers(rig)
    # The hand-off is the Chairperson's voice and the question that follows is Doctor Iyer's.
    at = rig.tts.requests.index(("Vikrant", "Doctor Iyer, please."))
    assert rig.tts.requests[at + 1][0] == "Poorvi"
    # The trace says which voice each request had.
    assert rig.brain.outcomes[1].trace["voice"]["tts_voices"][0] == "Vikrant"
    assert "Poorvi" in rig.brain.outcomes[1].trace["voice"]["tts_voices"]


async def test_a_voice_is_named_at_the_start_of_every_line_even_when_it_has_not_changed(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, voice_for=picker) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)  # the ritual, then the opener
        await rig.speak(0.1)
        await rig.until(lambda: len(rig.brain.outcomes) == 1, timeout=15)
        await rig.until(lambda: rig.brain.state == LISTENING)
    # Three lines, all the Chairperson's (ritual, opener, her probe): three frames, not one.
    assert rig.tts.voice_changes == ["Vikrant"] * 3
    assert {v for v, _t in rig.tts.requests} == {"Vikrant"}


async def test_without_a_voice_table_no_settings_frame_is_ever_sent(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        for n in (1, 2):
            await rig.speak(0.1)
            await rig.until(lambda n=n: len(rig.brain.outcomes) == n, timeout=15)
            await rig.until(lambda: rig.brain.state == LISTENING)
    assert rig.tts.voice_changes == [] and {v for v, _t in rig.tts.requests} == {None}
    assert all(v is None for v in rig.brain.outcomes[1].trace["voice"]["tts_voices"])


async def test_a_member_left_out_of_the_table_is_not_given_a_frame(
    settings, cards, fake_llm, form, tmp_path
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, voice_for=lambda m: {"chairman": "Vikrant"}.get(m)) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        for n in (1, 2):
            await rig.speak(0.1)
            await rig.until(lambda n=n: len(rig.brain.outcomes) == n, timeout=15)
            await rig.until(lambda: rig.brain.state == LISTENING)
    assert set(rig.tts.voice_changes) == {"Vikrant"}  # Doctor Iyer got none: the last voice stays


@pytest.mark.parametrize("unit", ["line", "first_rest"])
async def test_a_request_held_for_a_whole_line_never_mixes_two_voices(
    settings, cards, fake_llm, form, tmp_path, unit
):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, tts_ms_per_word=5, voice_for=picker, tts_request_unit=unit) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        for n in (1, 2):
            await rig.speak(0.1)
            await rig.until(lambda n=n: len(rig.brain.outcomes) == n, timeout=15)
            await rig.until(lambda: rig.brain.state == LISTENING)
    board = [e for e in rig.events("caption") if e["side"] == "board"]
    question = [e["text"] for e in board if e["speaker"] == FIVE[1]][-1]  # Doctor Iyer's question
    called = next(e["text"] for e in board if e["text"].startswith("Doctor Iyer, please"))
    # The Chairperson's hand-off and Doctor Iyer's question are separate requests, each in its own
    # voice, even though the unit is a whole line: one request never carries both.
    mine = [(v, t) for v, t in rig.tts.requests if called in t]
    hers = [(v, t) for v, t in rig.tts.requests if question in t]
    assert len(mine) == 1 and mine[0][0] == "Vikrant" and question not in mine[0][1]
    assert len(hers) == 1 and hers[0][0] == "Poorvi" and called not in hers[0][1]
    assert len(rig.tts.requests) < len(board)  # and it did hold sentences together


async def test_a_voice_cut_off_by_a_barge_in_does_not_leak_into_the_next_line(
    settings, cards, fake_llm, form, tmp_path
):
    """The settings frame is uninterruptible and the speech after it is not: cut a hand-off line
    after its first words and the voice of its later part is still set. The next line names its
    voice again, so every request that is ever sent is in its own speaker's voice."""
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s, voice_for=picker) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak(0.1)
        await rig.until(lambda: len(rig.brain.outcomes) == 1, timeout=15)
        await rig.until(lambda: rig.brain.state == LISTENING)
        rig.tts.ms_per_word = 300  # slow, so there is time to cut in
        await rig.speak(0.1)  # the second answer: the Chairperson hands over to Doctor Iyer
        await rig.until(lambda: rig.brain.state == BOARD
                        and rig.ledger.playback_started_at is not None, timeout=15)
        await asyncio.sleep(0.3)
        await rig.worker.queue_frame(VADUserStartedSpeakingFrame())
        await rig.worker.queue_frame(UserStartedSpeakingFrame())
        await rig.worker.queue_frame(InterruptionFrame())
        await rig.until(lambda: len(rig.brain.outcomes) == 2, timeout=10)
        await asyncio.sleep(0.3)
        # What the cut line left behind: Doctor Iyer's voice is still set on the TTS service.
        assert rig.tts.voice == "Poorvi"
        # A line by the Chairperson names her voice again, so nothing of Doctor Iyer's leaks in.
        await rig.brain.say("Ek minute.", speaker=FIVE[0], kind="test")
        assert rig.tts.requests[-1] == ("Vikrant", "Ek minute.")
        rig.tts.ms_per_word = 5
        await rig.worker.queue_frame(VADUserStoppedSpeakingFrame())
        await rig.worker.queue_frame(UserStoppedSpeakingFrame())
        await rig.until(lambda: len(rig.brain.outcomes) == 3, timeout=15)
    assert rig.brain.outcomes[1].trace["voice"]["interrupted"]
    voices_follow_speakers(rig)
