"""The panel through the whole session (P4): whole interviews on the fake LLM, checked as a
conversation (CTO_NOTES_06 §10).

A1  a topic change moves to the next member with an acknowledgement, at every position, in every
    listed spelling, with the listener up or down; the last segment closes
A1c the refused topic never returns, in any script, not even by a fact challenge
A3  every board line traces to its agenda item and member; every change of member is spoken
A4  a bad model line is never heard
A5  per turn only the listener and the actor are called, and the director adds no model call
and: budgets, caps, repeat, reopen, `must_ask`, the report's request list, the checkpointer thread.

The detector's own recall on real speech is unmeasured (Ruling 51): these tests prove the listed
sentences, not paraphrases.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest

from conftest import answer
from mockboard import cli
from mockboard.brain.agenda import (
    DEFAULT_SEGMENT_BUDGETS,
    SEGMENT_MEMBERS,
    capped_budgets,
    situation_for,
)
from mockboard.brain.avoid_terms import expand_terms, find_avoided
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import FakeLLMClient, LLMTransientError
from mockboard.brain.panel_actor import question_part
from mockboard.brain.panel_lines import (
    CHANGE_TOPIC_ACK_LINES,
    CLOSE_LINES,
    HANDOFF_LINES,
    RITUAL_LINES,
    SKIP_ACK_LINES,
    line_for,
)
from mockboard.brain.schemas import PANEL_FREE_TURNS, Move
from mockboard.config import REPO_ROOT
from mockboard.modes.upsc_panel.hooks import REQUEST_ACCURACY_NOTE
from mockboard.modes.upsc_panel.setup import PanelSetup
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

SEGMENTS = list(SEGMENT_MEMBERS)
PLAIN = [
    "Mujhe lagta hai local rozgaar badhna chahiye. Lekin environment ka dhyan bhi rakhna padega.",
    "Main logon se baat karke pehle unki dikkat samjhunga, phir ek chhota pilot chalaunga.",
    "Is par dono taraf ke tark hain, isliye main pehle data dekhna chahunga phir faisla lunga.",
]
CHANGES = {
    "latin": "tourism ke alawa kisi aur topic pe baat karte hain",
    "no_term": "kisi aur topic pe baat karte hain",
    "devanagari": "टूरिज्म के अलावा किसी और टॉपिक पर बात करते हैं",
    "english": "let's talk about something else",
    "variant": "tourism ke ilawa kisi aur topik pe baat karte hain",
}


class DeadListenerLLM(FakeLLMClient):
    """The fake with a listener that always fails: the move falls back, and a request must still
    be honoured, because the detector reads the raw transcript (CTO_NOTES_06 §3)."""

    async def _tool_call(self, system, user, tool, max_tokens, stats):
        if stats.name == "listener":
            raise LLMTransientError("listener down")
        return await super()._tool_call(system, user, tool, max_tokens, stats)


@dataclass
class Played:
    session: InterviewSession
    opener: object
    outs: list
    rows: list[dict]
    llm: FakeLLMClient

    @property
    def hooks(self):
        return self.session.hooks

    def panel(self, i: int) -> dict:
        return self.rows[i]["panel"]


async def start(settings, cards, form, tmp_path, *, cap=None, llm=None, sid=None):
    # A fresh id each time: the director's checkpointer is shared by the process, and a reused
    # thread id would carry one test's append-only logs into the next.
    sid = sid or f"panel-{uuid.uuid4().hex[:10]}"
    llm = llm or make_fake_llm(cards)
    deps = build_deps(settings, llm, cards=cards)
    recorder = TraceRecorder(tmp_path, sid)
    session = InterviewSession(
        deps=deps, form=form, recorder=recorder, analyzer=TurnAnalyzer(deps),
        session_id=sid, mode_options=PanelSetup(max_answers=cap),
    )
    opener = await session.open()
    return session, opener, llm


def trace_rows(session) -> list[dict]:
    path = session.recorder.path
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r.get("panel") and r.get("turn_id")]


async def play(settings, cards, form, tmp_path, answers, *, cap=None, llm=None) -> Played:
    session, opener, llm = await start(settings, cards, form, tmp_path, cap=cap, llm=llm)
    outs = []
    for text in answers:
        if session.state.finished:
            break
        outs.append(await answer(session, text, think_s=0.5))
    await session.close()
    return Played(session, opener, outs, trace_rows(session), llm)


async def reach(session, segment_index: int, position: int, outs: list) -> None:
    """Play plain answers until the next answer is the `position`-th in segment `segment_index`."""
    n = 0
    while not session.state.finished and not (
        session.state.topic_index == segment_index and session.state.turns_in_topic == position
    ):
        outs.append(await answer(session, PLAIN[n % len(PLAIN)], think_s=0.5))
        n += 1


def texts(out) -> list[str]:
    return [p.text for p in out.line.parts]


# --- A1: a topic change moves on, every time, at every position ----------------------------------


POSITIONS = [
    (k, pos)
    for k, segment in enumerate(SEGMENTS)
    for pos in range(DEFAULT_SEGMENT_BUDGETS[segment])
]


@pytest.mark.parametrize(("k", "pos"), POSITIONS)
async def test_change_topic_always_moves_on(settings, cards, form, tmp_path, k, pos):
    session, _, _ = await start(settings, cards, form, tmp_path)
    outs: list = []
    await reach(session, k, pos, outs)
    assert not session.state.finished
    agenda = session.hooks.agenda
    out = await answer(session, CHANGES["latin"], think_s=0.5)
    assert out.move.rule_id in ("D-HANDOFF", "D-CLOSE")
    ack = line_for(CHANGE_TOPIC_ACK_LINES, "hi-en")
    if k < len(SEGMENTS) - 1:
        nxt = agenda.segments[k + 1]
        parts = out.line.parts
        assert parts[0].text == ack and parts[0].member == agenda.segments[k].member
        assert parts[1].member == "chairman" and parts[1].text.startswith("Dhanyavaad.")
        assert parts[-1].member == nxt.member != agenda.segments[k].member
        assert session.state.topic == nxt.id
    else:
        assert out.line.parts[-1].text == line_for(CLOSE_LINES, "hi-en") and session.state.finished


@pytest.mark.parametrize("spelling", sorted(CHANGES))
@pytest.mark.parametrize("k", [0, 2, 4])
async def test_every_listed_spelling_moves_on(settings, cards, form, tmp_path, spelling, k):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await reach(session, k, 0, [])
    before = session.state.topic
    out = await answer(session, CHANGES[spelling], think_s=0.5)
    assert texts(out)[0] == line_for(CHANGE_TOPIC_ACK_LINES, "hi-en")
    assert session.state.topic != before or session.state.finished


@pytest.mark.parametrize("k", [0, 1, 3])
async def test_change_topic_holds_with_the_listener_down(settings, cards, form, tmp_path, k):
    llm = DeadListenerLLM(responders=make_fake_llm(cards)._responders)
    session, _, _ = await start(settings, cards, form, tmp_path, llm=llm)
    await reach(session, k, 0, [])
    before = session.state.topic
    out = await answer(session, CHANGES["latin"], think_s=0.5)
    assert out.fallback_reason or out.trace.get("fallbacks")  # the listener really was down
    assert texts(out)[0] == line_for(CHANGE_TOPIC_ACK_LINES, "hi-en")
    assert session.state.topic != before


# --- A1c: the refused topic never returns --------------------------------------------------------


async def test_a_refused_topic_never_returns_not_even_by_a_fact_challenge(
    settings, cards, form, tmp_path
):
    claim = (
        "Testland mein tourism share of GDP 30 percent hai in 2023, isliye tourism hi growth "
        "ka main engine hai."
    )
    session, _, _ = await start(settings, cards, form, tmp_path)
    outs: list = []
    await reach(session, 1, 0, outs)
    outs.append(await answer(session, claim, think_s=0.5))
    await session.settle_fact_checks(5)  # the claim's verdict lands, so R4 could now fire
    outs.append(await answer(session, CHANGES["latin"], think_s=0.5))
    for i in range(20):
        if session.state.finished:
            break
        outs.append(await answer(session, PLAIN[i % 3], think_s=1.0))
    assert session.state.finished
    assert Move.fact_challenge not in {o.move.move for o in outs}
    avoided = expand_terms(["tourism"])
    for out in outs[-(len(outs) - 3):]:
        assert find_avoided(out.line.text, avoided) is None, out.line.text


@pytest.mark.parametrize(
    "line",
    [
        "Achha. Turism ke baare mein aap kya sochte hain?",
        "Achha. पर्यटन ke baare mein aap kya sochte hain?",
        "Achha. Paryatan ka asar kaisa hoga?",
        "Achha. टूरिज्म ka asar kaisa hoga?",
    ],
)
async def test_a_model_line_naming_the_refused_topic_in_any_script_is_never_heard(
    settings, cards, form, tmp_path, line
):
    llm = make_fake_llm(cards)
    session, _, llm = await start(settings, cards, form, tmp_path, llm=llm)
    await reach(session, 2, 0, [])
    await answer(session, CHANGES["latin"], think_s=0.5)  # the hand-off to the next member
    llm.push("actor", line)  # the next actor call tries to bring it back
    out = await answer(session, PLAIN[0], think_s=0.5)
    low = out.line.text.lower()
    assert "turism" not in low and "tourism" not in low and "paryatan" not in low
    assert "पर्यटन" not in out.line.text and "टूरिज्म" not in out.line.text
    assert out.line.text.rstrip().endswith("?")


async def test_a_seed_that_names_the_refused_topic_is_replaced_by_a_clean_question(
    settings, cards, form, tmp_path
):
    """The degree seed says "Electronics". After the candidate refused it, Doctor Iyer must not."""
    session, _, _ = await start(settings, cards, form, tmp_path)
    await reach(session, 1, 0, [])
    out = await answer(session, "Electronics ke alawa kisi aur topic pe baat karte hain",
                       think_s=0.5)
    assert session.state.topic == "degree"
    assert "electronics" not in out.line.text.lower()
    assert out.line.parts[-1].member == "economist" and out.line.text.endswith("?")


async def test_the_segments_aliases_are_refused_even_when_the_candidate_names_no_term(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await reach(session, 2, 0, [])  # in the degree segment
    out = await answer(session, CHANGES["no_term"], think_s=0.5)
    assert out.trace is not None
    row = trace_rows(session)[-1]["panel"]
    assert "Electronics" in row["avoid_terms"]  # the segment's own word, though none was said


# --- A3: every line traces; every change of member is spoken -------------------------------------


async def test_every_board_line_traces_to_its_agenda_item_and_member(
    settings, cards, form, tmp_path
):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(30)])
    agenda = p.hooks.agenda
    assert p.session.state.finished and len(p.rows) == len(p.outs)
    members = {s.member for s in agenda.segments}
    for out, row in zip(p.outs, p.rows, strict=True):
        panel = row["panel"]
        assert panel["segment_id"] in agenda.topics
        assert panel["agenda_source"] == "template"
        assert panel["member"] in members | {"chairman"}
        assert panel["member_name"] == p.hooks.speaker_name(panel["member"])
        assert [x["text"] for x in panel["parts"]] == texts(out)
        assert panel["parts"][-1]["member"] == out.move.member == panel["member"]
        assert row["board_line"] == out.line.text and row["move"] == out.move.move.value
        if panel["route"] != "close":  # the segment's member speaks all of it, even a reassure
            assert panel["member"] == agenda.segment(panel["segment_id"]).member
        if panel["route"] == "next_question" and panel["seed_id"]:
            seg, index = panel["seed_id"].split("#")
            assert seg == panel["segment_id"] and agenda.segment(seg).seeds[int(index)].text


async def test_the_session_start_line_names_the_agenda_and_the_openers_member(
    settings, cards, form, tmp_path
):
    session, opener, _ = await start(settings, cards, form, tmp_path)
    first = json.loads(session.recorder.path.read_text(encoding="utf-8").splitlines()[0])
    assert first["event"] == "session_start" and first["panel"] == "on"
    assert [s["id"] for s in first["agenda"]["segments"]] == SEGMENTS
    assert first["agenda"]["source"] == "template"
    assert first["segment_id"] == "form_hr" and first["member"] == "chairman"
    assert first["prompt_versions"]["actor"] == 4 and first["prompt_versions"]["opener"] == 2
    assert opener.parts[0].text == line_for(RITUAL_LINES, "hi-en")
    assert not opener.text.lower().startswith("namaste. namaste")


async def test_every_change_of_member_is_a_spoken_handoff(settings, cards, form, tmp_path):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(30)])
    seen = ["form_hr"]
    handoffs = 0
    for out, row in zip(p.outs, p.rows, strict=True):
        segment = row["panel"]["segment_id"]
        if row["panel"]["route"] == "close":
            break
        if segment != seen[-1]:
            handoffs += 1
            seen.append(segment)
            name = p.hooks.speaker_name(row["panel"]["member"])
            chair = [x for x in out.line.parts if x.member == "chairman"]
            assert chair and chair[0].text == line_for(HANDOFF_LINES, "hi-en").format(name=name)
            answered = p.hooks.agenda.segment(row["panel"]["answered_segment"])
            assert row["panel"]["handoff_from"] == answered.member
    assert seen == SEGMENTS and handoffs == 4


# --- the cap, budgets and the close --------------------------------------------------------------


@pytest.mark.parametrize("cap", [5, 6, 7, 10, 12, 15])
async def test_a_capped_interview_reaches_a_proper_close_with_every_member_heard(
    settings, cards, form, tmp_path, cap
):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(40)], cap=cap)
    assert p.session.state.finished
    assert p.hooks.answers_used == cap == sum(capped_budgets(cap).values())
    last = p.outs[-1]
    assert last.line.parts[-1].text == line_for(CLOSE_LINES, "hi-en")
    assert last.line.parts[-1].member == "chairman"
    assert {r["panel"]["segment_id"] for r in p.rows} >= set(SEGMENTS[1:])  # all five spoke
    assert len(p.outs) <= cap + PANEL_FREE_TURNS


async def test_an_uncapped_interview_runs_the_whole_agenda_to_the_close(
    settings, cards, form, tmp_path
):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(40)])
    assert p.hooks.answers_used == sum(DEFAULT_SEGMENT_BUDGETS.values()) == 15
    assert p.rows[-1]["panel"]["close_reason"] == "agenda"
    assert p.session.max_turns == 15 + PANEL_FREE_TURNS


async def test_no_segment_is_asked_more_than_its_budget(settings, cards, form, tmp_path):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(40)], cap=10)
    asked: dict[str, int] = {}
    for row in p.rows:
        if row["panel"]["counted"]:
            seg = row["panel"]["answered_segment"]
            asked[seg] = asked.get(seg, 0) + 1
    for seg, n in asked.items():
        assert n <= capped_budgets(10)[seg]


@pytest.mark.parametrize("cap", [5, 6, 7, 10, 15, None])
async def test_the_dm_situation_is_asked_unless_the_candidate_skips_it(
    settings, cards, form, tmp_path, cap
):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(40)], cap=cap)
    said = " ".join(o.line.text for o in p.outs)
    assert situation_for(form) in said


async def test_no_director_error_in_a_plain_run(settings, cards, form, tmp_path):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(40)], cap=10)
    errors = [r["panel"]["director_error"] for r in p.rows if r["panel"]["director_error"]]
    assert not errors or all(e.startswith("ceiling") for e in errors)


# --- requests ------------------------------------------------------------------------------------


async def test_repeat_says_the_last_question_exactly_and_uses_no_budget(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    first = await answer(session, PLAIN[0], think_s=0.5)
    asked = question_part(first.line.parts[-1].text)  # the question, not the pleasantry
    used = session.hooks.answers_used
    r1 = await answer(session, "please repeat", think_s=0.5)
    r2 = await answer(session, "phir se boliye", think_s=0.5)
    assert r1.line.parts[-1].text == r2.line.parts[-1].text == asked  # exact, with no stacking
    assert [len(r.line.parts) for r in (r1, r2)] == [2, 2]
    assert session.hooks.answers_used == used and session.hooks.free_used == 2


async def test_a_repeat_right_after_a_handoff_says_the_new_members_question(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    outs: list = []
    await reach(session, 1, 0, outs)
    assert outs[-1].move.rule_id == "D-HANDOFF"
    seed = session.hooks.agenda.segment("home_district").seeds[0].text
    out = await answer(session, "please repeat", think_s=0.5)
    assert out.line.parts[-1].text == seed  # the question, without "Shukriya. Namaste."
    assert out.line.parts[-1].member == "administrator" and len(out.line.parts) == 2


async def test_a_repeat_does_not_let_a_second_pressure_through(settings, cards, form, tmp_path):
    """Free turns leave `last_move` alone (CTO_NOTES_15 §9.2)."""
    session, _, _ = await start(settings, cards, form, tmp_path)
    await reach(session, 1, 0, [])  # a segment with room for three questions
    moves = []
    for text in ("Haan, important hai.", "please repeat", "Jobs milti hain."):
        out = await answer(session, text, think_s=0.5)
        moves.append(out.move.move)
    assert moves[0] == Move.pressure and moves[1] == Move.repeat_question
    assert moves[2] != Move.pressure  # no pressure twice in a row, whatever came between


async def test_a_skip_acknowledges_and_asks_the_next_seed(settings, cards, form, tmp_path):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await reach(session, 1, 0, [])
    out = await answer(session, "next question please", think_s=0.5)
    assert texts(out)[0] == line_for(SKIP_ACK_LINES, "hi-en")
    assert out.move.rule_id == "D-SEED" and out.line.parts[-1].member == "administrator"


async def test_matlab_rephrases_and_costs_no_budget(settings, cards, form, tmp_path):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    used = session.hooks.answers_used
    out = await answer(session, "sawal samajh nahi aaya, matlab?", think_s=0.5)
    assert out.move.move == Move.rephrase_question
    assert session.hooks.answers_used == used


async def test_ending_asks_once_and_a_clear_yes_closes(settings, cards, form, tmp_path):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    ask = await answer(session, "interview yahin khatam karte hain", think_s=0.5)
    assert ask.move.rule_id == "D-END-CONFIRM" and not session.state.finished
    done = await answer(session, "haan ji", think_s=0.5)
    assert session.state.finished and done.line.parts[-1].member == "chairman"


async def test_a_wrap_up_request_closes_with_the_time_line(settings, cards, form, tmp_path):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    assert session.request_wrap_up() is True
    out = await answer(session, PLAIN[1], think_s=0.5)
    assert session.state.finished
    last = json.loads(session.recorder.path.read_text().splitlines()[-1])
    assert last["panel"]["close_reason"] == "time"
    assert "samay" in out.line.text.lower()


# --- reopen, latency, state ----------------------------------------------------------------------


async def test_a_reopened_turn_leaves_memory_and_position_unchanged(
    settings, cards, form, tmp_path
):
    """Memory moves only when a finished turn is folded, at the next invoke. A turn that is
    decided, reopened and decided again folds nothing of its own (Ruling 31)."""
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    hooks = session.hooks

    def snapshot():
        return (
            session.state.turn_index, session.state.topic_index, session.state.turns_in_topic,
            hooks.answers_used, hooks.free_used, hooks.last_move,
            hooks.director.memory.model_copy(deep=True),
        )

    session.submit_segment("kuch aur sochkar kehta hoon", segment_index=0)
    await session.end_of_turn(30.0)
    decided = snapshot()  # t1 is folded by this invoke; the open turn is not
    session.reopen_turn()
    assert snapshot() == decided
    session.submit_segment("aur ek baat jodna chahta hoon", segment_index=1)
    await session.end_of_turn(30.0)
    assert snapshot() == decided  # the second decision folded nothing of the reopened turn
    out = await session.speak()
    assert out.line.text and session.state.turn_index == decided[0] + 1


async def test_per_turn_only_the_listener_and_the_actor_call_a_model(
    settings, cards, form, tmp_path
):
    p = await play(settings, cards, form, tmp_path, [PLAIN[i % 3] for i in range(40)], cap=10)
    names = [c.name for c in p.llm.calls]
    assert names.count("opener") == 1 and "plan_agenda" not in names
    assert set(names) <= {"opener", "listener", "actor", "librarian", "judge"}
    for row in p.rows:
        assert {c["name"] for c in row["llm_calls"]} <= {"listener", "actor"}


async def test_the_director_adds_no_model_call(settings, cards, form, tmp_path):
    """The graph is model-free: the whole turn still runs when the model client raises on any
    call the director could make (it makes none)."""
    p = await play(settings, cards, form, tmp_path, ["please repeat", "next question please"])
    assert all(r["panel"]["director_ms"] < 250 for r in p.rows)


async def test_the_checkpointer_thread_is_deleted_by_close_and_close_is_idempotent(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    director = session.hooks.director
    config = {"configurable": {"thread_id": session.session_id}}
    assert await director._graph.checkpointer.aget_tuple(config) is not None
    await session.close()
    await session.close()
    assert await director._graph.checkpointer.aget_tuple(config) is None


async def test_the_report_lists_each_detected_request_with_its_source(
    settings, cards, form, tmp_path
):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    await answer(session, "please repeat", think_s=0.5)
    await answer(session, CHANGES["latin"], think_s=0.5)
    report = await session.finish()
    assert [(r.kind, r.source) for r in report.requests] == [
        ("repeat", "rules"), ("change_topic", "rules"),
    ]
    assert all(r.pattern_id for r in report.requests)
    notes = " ".join(report.notes)
    assert "Request at t1: repeat" in notes and "Request at t2: change_topic" in notes
    assert REQUEST_ACCURACY_NOTE in report.notes
    assert "Accuracy on real speech has not been measured yet." in notes
    assert report.prompt_versions["actor"] == 4


async def test_a_session_with_no_request_says_so(settings, cards, form, tmp_path):
    session, _, _ = await start(settings, cards, form, tmp_path)
    await answer(session, PLAIN[0], think_s=0.5)
    report = await session.finish()
    assert report.requests == []
    assert "No candidate request was detected in this session." in report.notes


# --- the old board is untouched ------------------------------------------------------------------


async def test_a_session_without_a_panel_setup_has_no_hooks_and_keeps_its_old_topics(
    settings, cards, form, tmp_path
):
    deps = build_deps(settings, make_fake_llm(cards), cards=cards)
    session = InterviewSession(
        deps=deps, form=form, recorder=TraceRecorder(tmp_path, "old", enabled=False),
        analyzer=TurnAnalyzer(deps),
    )
    assert session.hooks is None and session.max_turns == 9
    assert session.state.topics == ["background", *settings.topics]
    assert session.opening_ritual() is None and session.request_wrap_up() is False
    await session.close()  # nothing to release


def test_the_panel_flag_is_hidden_fake_only_and_not_for_debate(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--panel"])
    assert "--fake" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.parse_args(["--panel", "--fake", "--mode", "debate"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--max-answers", "10"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--panel", "--fake", "--max-answers", "4"])
    args = cli.parse_args(["--panel", "--fake", "--max-answers", "10"])
    assert args.panel and args.max_answers == 10


def test_the_help_text_does_not_advertise_the_panel(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    assert "--panel" not in capsys.readouterr().out


# --- the CLI, as the owner runs it ---------------------------------------------------------------


def run_cli(capsys, *extra: str) -> str:
    rc = cli.main(
        ["--fake", "--panel", "--script", str(REPO_ROOT / "examples" / "fake_panel.txt"),
         "--session-id", "panel-cli", "--no-trace", *extra]
    )
    assert rc == 0
    return capsys.readouterr().out


def test_the_scripted_panel_session_moves_on_from_tourism_and_it_never_returns(capsys):
    out = run_cli(capsys)
    lines = out.splitlines()
    board = [x for x in lines if x.startswith("[")]
    at = next(i for i, x in enumerate(lines) if x.startswith("You: tourism ke alawa"))
    assert lines[at + 1].startswith("[Shrimati Menon] Zaroor, koi baat nahi.")
    assert lines[at + 2].startswith("[Chairperson Sharma] Dhanyavaad. Doctor Iyer, please.")
    assert lines[at + 3].startswith("[Doctor Iyer] (move_on, D-HANDOFF)")
    said = " ".join(x for x in lines[at + 1 :] if x.startswith("["))
    assert find_avoided(said, expand_terms(["tourism"])) is None
    assert board[-1] == "[Chairperson Sharma] (move_on, D-CLOSE) Thank you, that will be all."
    assert "Request at t3: change_topic" in out


def test_a_cap_of_ten_answers_reaches_a_proper_close_in_the_scripted_session(capsys):
    out = run_cli(capsys, "--max-answers", "10")
    board = [x for x in out.splitlines() if x.startswith("[")]
    assert board[-1] == "[Chairperson Sharma] (move_on, D-CLOSE) Thank you, that will be all."
    answers = [x for x in out.splitlines() if x.startswith("You:")]
    # One of them is the free "please repeat". A topic change forfeits the rest of its segment's
    # budget, so a capped session may end under the cap, never over it.
    assert 8 <= len(answers) - 1 <= 10


def test_the_scripted_session_repeats_no_board_question(capsys):
    out = run_cli(capsys)
    asked = []
    for line in out.splitlines():
        if line.startswith("[") and "(repeat_question" not in line and line.endswith("?"):
            asked.append(line.split(") ", 1)[-1] if ") " in line else line)
    assert len(asked) == len(set(asked)), "a question was asked twice"


def test_the_old_answers_only_script_still_gives_todays_board_lines(capsys):
    """Moved from examples/fake_panel.txt, unchanged, so the md5 of Ruling 46 still guards it."""
    import hashlib

    answers = REPO_ROOT / "examples" / "fake_answers.txt"
    form = REPO_ROOT / "examples" / "forms" / "sample_form.json"
    rc = cli.main(["--fake", "--form-file", str(form), "--script", str(answers), "--no-trace"])
    out = capsys.readouterr().out
    board = "".join(x + "\n" for x in out.splitlines() if x.startswith("["))
    assert rc == 0
    assert hashlib.md5(board.encode("utf-8")).hexdigest() == "826ef550999cc34d9018806dce1b665e"


# --- more: reassure then repeat, a fuzz, and the director sim as the expected behaviour ---------


async def test_a_repeat_after_a_reassure_says_the_reassures_simpler_question(
    settings, cards, form, tmp_path
):
    """`asked` is honest (CTO_NOTES_15 §9.5): a reassure that puts a simpler question is the
    question to repeat; silence puts none, so the last real question stands."""
    session, _, _ = await start(settings, cards, form, tmp_path)
    await reach(session, 1, 0, [])
    reassure = await answer(session, "Mujhe exact pata nahi hai, but I think kuch karna chahiye.",
                            think_s=0.5)
    assert reassure.move.move == Move.reassure
    asked = question_part(reassure.line.parts[-1].text)
    again = await answer(session, "please repeat", think_s=0.5)
    assert again.line.parts[-1].text == asked

    silence = await answer(session, "", think_s=0.5)
    assert silence.move.move == Move.reassure and session.hooks.free_used == 2
    after = await answer(session, "please repeat", think_s=0.5)
    assert after.line.parts[-1].text == asked  # silence did not replace it


REQUEST_MIX = [
    *PLAIN, *PLAIN,
    "please repeat", "next question please", CHANGES["latin"], CHANGES["no_term"],
    "sawal samajh nahi aaya, matlab?", "", "???", "Sach kahun to mujhe nahi pata.",
    "interview yahin khatam karte hain", "nahi nahi, chaliye aage badhte hain", "haan ji",
]


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("cap", [None, 10])
async def test_fuzz_the_panel_holds_its_invariants_on_random_candidates(
    settings, cards, form, tmp_path, seed, cap
):
    import random

    rng = random.Random(seed * 7 + (cap or 0))
    answers = [rng.choice(REQUEST_MIX) for _ in range(40)]
    p = await play(settings, cards, form, tmp_path, answers, cap=cap)
    hooks, st = p.hooks, p.session.state
    assert st.finished or len(p.outs) == len(answers)
    limit = cap if cap is not None else sum(DEFAULT_SEGMENT_BUDGETS.values())
    assert hooks.answers_used <= limit
    assert hooks.free_used <= PANEL_FREE_TURNS
    assert len(p.outs) <= p.session.max_turns
    for row in p.rows:
        err = row["panel"]["director_error"]
        assert err is None or err.startswith("ceiling"), err
    budgets = capped_budgets(cap)
    per_segment: dict[str, int] = {}
    for row in p.rows:
        if row["panel"]["counted"]:
            seg = row["panel"]["answered_segment"]
            per_segment[seg] = per_segment.get(seg, 0) + 1
    assert all(n <= budgets[seg] + 1 for seg, n in per_segment.items())  # +1: a declined end
    if st.finished:
        assert p.rows[-1]["panel"]["route"] == "close"
        assert p.outs[-1].line.parts[-1].member == "chairman"
    questions = [
        o.line.parts[-1].text
        for o in p.outs
        if o.move.move in (Move.probe, Move.counter, Move.steelman, Move.pressure)
        and not o.line.is_fallback
    ]
    assert len(questions) == len(set(questions)), "the board asked the same question twice"


async def test_the_real_session_makes_the_same_decisions_as_the_director_sim(
    settings, cards, form, tmp_path
):
    """The sim (`tests/director_sim.py`) is the expected behaviour of `PanelHooks` (CTO_NOTES_15
    §9.9). The same requests, in the same order, give the same route, cost and lines."""
    from director_sim import Sim

    script = [
        PLAIN[0], PLAIN[1], "please repeat", CHANGES["latin"], PLAIN[2], "next question please",
        PLAIN[0], PLAIN[1], "interview yahin khatam karte hain", "nahi nahi, chaliye aage badhte",
        PLAIN[2], PLAIN[0], PLAIN[1], PLAIN[2], PLAIN[0], PLAIN[1], PLAIN[2],
    ]
    sim = Sim(session_id="sim-parity")
    for text in script:
        if sim.finished:
            break
        await sim.say(text)
    p = await play(settings, cards, form, tmp_path, script)

    def shape(d):
        return (
            d.node, d.counted, d.free, d.close_reason, d.request_kind, d.answered_segment,
            d.panel.segment_id, [x.text for x in d.preface],
        )

    real = [shape(p.hooks.director.decisions[i]) for i in range(len(p.outs) - 1)]
    expected = [shape(d) for _, d in sim.log[: len(real)]]
    assert real == expected
    assert p.session.state.finished == sim.finished
