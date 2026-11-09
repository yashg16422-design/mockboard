"""The panel director: routes, counters, seeds, closes and the graph (CTO_NOTES_12 §9.3, §9.4;
CTO_NOTES_14 §7). `Sim` is the session side; the strategist in it is the real one."""

from __future__ import annotations

import ast
import itertools
import random
from pathlib import Path

import pytest

import mockboard.brain.director as director_module
from director_sim import ANSWER, FORM, Sim
from mockboard.brain import llm as llm_module
from mockboard.brain.agenda import build_fallback_agenda, capped_budgets
from mockboard.brain.director import (
    PANEL_FREE_TURNS,
    PanelDirector,
    compile_director_graph,
    decide_panel,
    fold_turns,
    is_clear_yes,
    next_seed,
    opening_memory,
)
from mockboard.brain.panel_lines import (
    CAP_CLOSE_LINES,
    CHANGE_TOPIC_ACK_LINES,
    CLOSE_LINES,
    END_CLOSE_LINES,
    END_CONFIRM_LINES,
    END_DECLINED_LINES,
    REGISTER_LINES,
    REPEAT_PREFIX_LINES,
    SKIP_ACK_LINES,
    WRAP_UP_CLOSE_LINES,
    handoff_line,
    line_for,
)
from mockboard.brain.schemas import (
    Agenda,
    AgendaSeed,
    AgendaSegment,
    Analysis,
    Balance,
    BoardMove,
    CompletedTurn,
    Move,
)
from mockboard.brain.strategist import CLOSE_TARGET, PRESSURE
from mockboard.modes.upsc import UPSC_MOVES

REPEAT = "please repeat"
CLARIFY = "matlab?"
SKIP = "next question please"
CHANGE = "tourism ke alawa kisi aur topic pe baat karte hain"
END = "interview yahin khatam karte hain"


def hi(table: dict[str, str]) -> str:
    return table["hi-en"]


def texts(decision) -> list[str]:
    return [part.text for part in decision.preface]


def speakers(decision) -> list[str]:
    return [part.member for part in decision.preface]


async def test_slang_is_one_sentence_and_the_question_still_comes():
    sim = Sim()
    decision = await sim.say("Bro yeh toh bahut easy hai, log khud dekh lenge.")
    assert texts(decision)[0] == line_for(REGISTER_LINES, "hi-en")
    assert decision.node != "close"
    assert decision.seed or decision.move.move != Move.move_on or decision.preface


async def answers(sim: Sim, count: int) -> None:
    for _ in range(count):
        await sim.say()


async def to_last_unit(sim: Sim) -> Sim:
    """Leave `sim` one answer short of the agenda's end, in the last segment."""
    await answers(sim, sim.agenda.total_budget - 1)
    assert sim.topic == sim.agenda.topics[-1] and not sim.finished
    return sim


def custom_agenda(*budgets: int, must_ask_last: bool = False) -> Agenda:
    members = ["chairman", "administrator", "economist", "humanities", "ips_officer"]
    segments = []
    for index, budget in enumerate(budgets):
        seeds = [AgendaSeed(text=f"Seed {index}.{n} kaise?") for n in range(3)]
        if must_ask_last and index == len(budgets) - 1:
            seeds[-1] = AgendaSeed(text="Situation kaise?", must_ask=True)
        segments.append(
            AgendaSegment(
                id=f"seg{index}", member=members[index], title=f"Part {index}", budget=budget,
                seeds=seeds,
            )
        )
    return Agenda(segments=segments)


# --- Section 9.3, row by row ------------------------------------------------------------------


async def test_normal_answer_counts_and_keeps_the_strategist_move():
    sim = Sim()
    d = await sim.say()
    assert (d.counted, d.free) == (True, False)
    assert d.node == "next_question" and d.move.move == Move.probe and d.move.rule_id == "R8"
    assert d.move.for_turn == "t1"


async def test_thin_and_pressured_answers_count():
    sim = Sim()
    d = await sim.say("Thoda sa.", reasons=[], word_count=2)
    assert d.counted and d.move.move == Move.pressure


async def test_a_long_pause_counts_though_it_is_rule_r3():
    sim = Sim()
    d = await sim.say(latency=9.0)
    assert d.move.rule_id == "R3" and d.move.move == Move.reassure
    assert (d.counted, d.free) == (True, False)


async def test_a_first_honest_unknown_counts_though_it_is_rule_r3():
    sim = Sim()
    d = await sim.say("Pata nahi sir, mujhe yeh nahi maloom.", honest_unknown=True)
    assert d.move.rule_id == "R3" and d.move.move == Move.reassure
    assert (d.counted, d.free) == (True, False)


async def test_silence_is_free_and_reassures():
    sim = Sim()
    d = await sim.say("")
    assert d.move.move == Move.reassure and d.move.rule_id == "R3"
    assert (d.counted, d.free) == (False, True)


async def test_a_listener_fallback_is_not_silence():
    sim = Sim()
    d = await sim.say(ANSWER, word_count=0, is_fallback=True, reasons=[])
    assert (d.counted, d.free) == (True, False)


async def test_garbled_is_free_and_keeps_the_fixed_clarify():
    sim = Sim()
    d = await sim.say("xx yy zz", garbled=True)
    assert d.move.move == Move.clarify and d.move.rule_id == "R1"
    assert (d.counted, d.free) == (False, True)


async def test_a_second_unclear_hearing_asks_the_next_question():
    sim = Sim()
    first = await sim.say("xx yy zz", garbled=True)
    assert first.free and first.move.rule_id == "R1"
    second = await sim.say("aa bb cc", garbled=True)
    assert second.move.rule_id == "D-SEED" and second.move.move == Move.next_question
    assert second.seed and (second.counted, second.free) == (True, False)
    third = await sim.say("qq rr ss", garbled=True)
    assert third.move.rule_id == "R1" and third.free


async def test_repeat_is_free_verbatim_and_needs_no_actor():
    sim = Sim()
    opener = sim.director.memory.last_question
    await sim.say("")  # silence keeps the opener as the question
    d = await sim.say(REPEAT)
    assert d.move.move == Move.repeat_question and d.move.pressure == 0
    assert d.repeat_text == opener and d.line_complete
    assert texts(d) == [hi(REPEAT_PREFIX_LINES)]
    assert (d.counted, d.free) == (False, True)


async def test_clarify_question_rephrases_for_the_actor():
    sim = Sim()
    d = await sim.say(CLARIFY)
    assert d.move.move == Move.rephrase_question and not d.line_complete
    assert (d.counted, d.free) == (False, True)


async def test_skip_counts_acks_and_asks_the_next_seed():
    sim = Sim()
    await answers(sim, 2)
    assert sim.topic == "home_district"
    d = await sim.say(SKIP)
    assert (d.counted, d.free) == (True, False)
    assert d.move.move == Move.next_question and d.seed_index == 1
    assert texts(d) == [hi(SKIP_ACK_LINES)] and speakers(d) == ["administrator"]


async def test_skip_with_the_budget_spent_hands_off():
    sim = Sim(cap=10)
    await answers(sim, 2)  # one answer in home_district, budget 2
    d = await sim.say(SKIP)
    assert d.node == "handoff" and d.move.target == "degree"
    assert speakers(d) == ["administrator", "chairman"]


async def test_skip_with_no_seed_left_hands_off_and_forfeits_budget():
    sim = Sim(agenda=custom_agenda(1, 9))
    await sim.say()
    for _ in range(3):
        d = await sim.say(SKIP)
    assert d.move.move == Move.move_on and d.finished and d.close_reason == "agenda"


async def test_change_topic_counts_and_ends_the_segment():
    sim = Sim()
    await answers(sim, 2)
    d = await sim.say(CHANGE)
    assert (d.counted, d.free) == (True, False)
    assert d.node == "handoff" and d.move.target == "degree" and d.move.member == "economist"
    assert d.avoid_terms == ("tourism",)
    assert d.seed_segment == "degree" and d.seed_index == 0


async def test_change_topic_preface_has_two_speakers_in_order():
    sim = Sim()
    await answers(sim, 2)
    d = await sim.say(CHANGE)
    assert [(p.member, p.text) for p in d.preface] == [
        ("administrator", hi(CHANGE_TOPIC_ACK_LINES)),
        ("chairman", handoff_line("Doctor Iyer", "hi-en", 1)),  # the second hand-off
    ]


async def test_change_topic_in_the_last_segment_closes():
    sim = Sim(cap=5)
    await answers(sim, 4)
    d = await sim.say(CHANGE)
    assert d.finished and d.close_reason == "agenda" and d.counted
    assert speakers(d) == ["ips_officer", "chairman"]
    assert texts(d) == [hi(CHANGE_TOPIC_ACK_LINES), hi(CLOSE_LINES)]


async def test_a_weak_change_on_a_short_turn_stands_even_when_the_listener_says_none():
    """P5's veto only covers turns over 8 words (Ruling 53); the Sim's listener says none."""
    sim = Sim()
    await answers(sim, 2)
    d = await sim.say("can we move on")
    assert d.request_kind == "change_topic" and d.node == "handoff"


# --- `end` (Rulings 52, 66) -------------------------------------------------------------------


async def test_end_asks_for_confirmation_and_uses_a_free_turn():
    sim = Sim()
    d = await sim.say(END)
    assert d.pending_end and not d.finished and d.line_complete
    assert texts(d) == [hi(END_CONFIRM_LINES)] and speakers(d) == ["chairman"]
    assert (d.counted, d.free) == (False, True)


@pytest.mark.parametrize(
    "reply", ["haan", "Yes.", "haan ji", "ji", "हाँ", "haan interview khatam karte hain"]
)
async def test_a_clear_yes_closes_without_counting(reply: str):
    sim = Sim()
    await sim.say(END)
    d = await sim.say(reply)
    assert d.finished and d.close_reason == "end_request"
    assert texts(d) == [hi(END_CLOSE_LINES)] and d.move.member == "chairman"
    assert (d.counted, d.free) == (False, False)


@pytest.mark.parametrize("reply", ["nahi", "no", "pata nahi", ANSWER, "", "haan shayad", REPEAT])
async def test_anything_else_resumes_free_with_the_same_question(reply: str):
    sim = Sim()
    question = sim.director.memory.last_question
    await sim.say(END)
    d = await sim.say(reply)
    assert not d.finished and not d.pending_end and d.move.move == Move.repeat_question
    assert texts(d) == [hi(END_DECLINED_LINES)] and d.repeat_text == question
    assert (d.counted, d.free) == (False, True)
    assert d.request_kind is None
    await sim.say()
    assert sim.director.memory.pending_end is False


async def test_a_request_in_a_declining_reply_leaves_no_avoid_terms():
    sim = Sim()
    await sim.say(END)
    await sim.say(f"nahi, {CHANGE}")
    await sim.say()
    assert sim.director.memory.avoid_terms == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("haan", True), ("Haan ji.", True), ("yes please", True), ("ji sir", True),
        ("bilkul", True), ("theek hai", True), ("हां जी", True), ("ज़रूर", True),
        ("ஆம்", True), ("சரி", True), ("இல்லை", False), ("வேண்டாம்", False),
        ("interview khatam karte hain", True), ("haan interview khatam karte hain", True),
        ("", False), ("nahi", False), ("na", False), ("no", False), ("hai", False),
        ("haan nahi", False), ("nahi interview khatam karte hain", False),
        ("interview khatam mat karo", False), ("haan lekin", False), ("शायद", False),
        ("don't end it", False), ("sir", False),
        # a yes, then end or thanks words (real-user review): closes
        ("Yes sir, please end it", True), ("Yes, end it", True), ("yes I want to end", True),
        ("Ji haan, end kar dijiye", True), ("Yes, thank you", True), ("हाँ, धन्यवाद", True),
        # Tamil and Devanagari "sir" (found writing the demo, Nov 7)
        ("ஆமாம் சார்.", True), ("ஆமாம் சார், இங்கே முடிக்கலாம்.", True), ("சரி சார்", True),
        ("जी सर", True), ("சார்", False), ("இல்லை சார்", False),
        ("ஆமாம் சார், ஆனால் ஒரு கேள்வி", False),
        # a yes, then anything else: resumes
        ("yes, continue", False), ("haan lekin ek sawal", False),
        ("yes, I want to continue", False),
        ("thank you", False), ("end it", False), ("yes, don't end it", False),
    ],
)  # fmt: skip
def test_is_clear_yes(text: str, expected: bool):
    assert is_clear_yes(text) is expected


QUESTIONING_REPLIES = [
    "ji?", "haan?", "yes?", "ok?", "जी?", "हाँ?", "theek hai?", "bilkul?", "sure?", "ji ？",
    "kya interview khatam karna hai?",
]  # fmt: skip


@pytest.mark.parametrize("reply", QUESTIONING_REPLIES)
async def test_a_question_never_closes_the_interview(reply: str):
    assert is_clear_yes(reply) is False
    sim = Sim()
    question = sim.director.memory.last_question
    await sim.say(END)
    d = await sim.say(reply)
    assert not d.finished and d.repeat_text == question and d.free


def test_the_question_guard_leaves_a_plain_yes_alone():
    assert is_clear_yes("haan ji") and is_clear_yes("जी हाँ") and is_clear_yes("ji  ")


async def exhausted_at_current_affairs(answered: int) -> Sim:
    """Cap 10, every free turn gone, `answered` answers given, standing in the last segment."""
    sim = Sim(cap=10)
    for _ in range(PANEL_FREE_TURNS):
        assert (await sim.say("")).free
    await answers(sim, answered)
    assert sim.topic == "current_affairs"
    return sim


async def test_a_counted_decline_asks_the_situation_when_it_is_due():
    sim = await exhausted_at_current_affairs(7)
    assert (await sim.say(END)).pending_end
    d = await sim.say("nahi")
    assert d.counted and d.seed_segment == "current_affairs" and d.seed_index == 2
    assert texts(d)[0] == hi(END_DECLINED_LINES)
    assert not d.finished and d.move.move == Move.next_question


async def test_a_decline_that_closes_does_not_first_say_we_go_on():
    sim = await exhausted_at_current_affairs(8)
    assert (await sim.say(END)).pending_end
    d = await sim.say("nahi")
    assert d.finished and d.close_reason == "answers"
    assert hi(END_DECLINED_LINES) not in texts(d)


async def test_ruling_66_end_on_the_last_unit_with_no_free_turn_closes_at_once():
    sim = await to_last_unit(Sim(cap=10, free_turns=0))
    d = await sim.say(END)
    assert d.finished and d.close_reason == "end_request" and not d.pending_end
    assert (d.counted, d.free) == (True, False)
    assert texts(d) == [hi(END_CLOSE_LINES)]
    assert sim.answers_used == 10 and sim.turn_index <= 10


async def test_ruling_66_turn_bound_with_free_turns_spent():
    sim = Sim(cap=5)
    for _ in range(PANEL_FREE_TURNS):
        assert (await sim.say("")).free
    await answers(sim, 4)
    d = await sim.say(END)
    assert d.close_reason == "end_request" and not d.pending_end
    assert sim.turn_index <= 5 + PANEL_FREE_TURNS


async def test_end_with_a_free_turn_left_on_the_last_unit_still_confirms():
    sim = await to_last_unit(Sim(cap=5))
    d = await sim.say(END)
    assert d.pending_end and d.free and not d.finished


async def test_end_with_no_free_turn_elsewhere_still_confirms_and_counts():
    sim = Sim(cap=10, free_turns=0)
    d = await sim.say(END)  # the first answer in form_hr, not the agenda's last unit
    assert d.pending_end and (d.counted, d.free) == (True, False)


async def test_a_counted_decline_after_a_counted_end_keeps_both_bounds():
    sim = Sim(cap=5, free_turns=0)
    d = await sim.say(END)  # form_hr has budget 1, so this spends it and still confirms
    assert d.pending_end and d.counted
    d = await sim.say("nahi")
    assert d.counted and d.node == "handoff" and texts(d)[0] == hi(END_DECLINED_LINES)
    await sim.plain_until_done()
    assert sim.finished and sim.answers_used == 5 and sim.turn_index == 5
    assert sim.log[-1][1].close_reason == "agenda"


async def test_the_situation_is_still_asked_after_a_counted_decline_shortens_the_session():
    sim = Sim(cap=6, free_turns=0)
    await sim.say(END)
    await sim.say("nahi")
    await sim.plain_until_done()
    assert sim.answers_used == 6
    assert [d.seed_index for _, d in sim.log if d.seed_segment == "current_affairs"] == [2]


async def test_end_outranks_wrap_up_and_wrap_up_outranks_a_free_turn():
    sim = Sim(wrap_up_due=True)
    assert (await sim.say(END)).pending_end
    sim = Sim(wrap_up_due=True)
    d = await sim.say(REPEAT)
    assert d.finished and d.close_reason == "time" and d.free


# --- Budget spent overrides the strategist (§9.4 step 5) --------------------------------------


async def test_budget_spent_overrides_interrupt():
    sim = Sim(agenda=custom_agenda(1, 2))
    sim.strategy = type(sim.strategy)(
        chairman="chairman", topic_members=sim.strategy.topic_members,
        board_interrupt_enabled=True, interrupt_after_words=5,
    )  # fmt: skip
    turn, _ = sim.build(ANSWER)
    assert turn.base_move.rule_id == "R2"
    d = decide_panel(turn, sim.agenda, sim.director.memory, sim.settings)
    assert d.node == "handoff"


async def test_budget_spent_overrides_a_latency_reassure():
    sim = Sim(agenda=custom_agenda(1, 2))
    turn, _ = sim.build(ANSWER, latency=9.0)
    assert turn.base_move.rule_id == "R3"
    assert decide_panel(turn, sim.agenda, sim.director.memory, sim.settings).node == "handoff"


async def test_budget_spent_overrides_a_fact_challenge():
    sim = Sim(agenda=custom_agenda(1, 2))
    turn, _ = sim.build(ANSWER)
    base = BoardMove(move=Move.fact_challenge, member="economist", target="c1", reason="R4: x",
                     rule_id="R4", pressure=2)  # fmt: skip
    turn = turn.model_copy(update={"base_move": base})
    assert decide_panel(turn, sim.agenda, sim.director.memory, sim.settings).node == "handoff"


async def test_a_fact_challenge_with_budget_left_is_spoken_by_the_segment_member():
    sim = Sim()
    turn, _ = sim.build(ANSWER)
    base = BoardMove(move=Move.fact_challenge, member="economist", target="c1", reason="R4: x",
                     rule_id="R4", pressure=2)  # fmt: skip
    turn = turn.model_copy(update={"base_move": base})
    d = decide_panel(turn, sim.agenda, sim.director.memory, sim.settings)
    assert d.move.move == Move.fact_challenge and d.move.member == "chairman"


async def test_a_strong_streak_shortens_the_interview_and_keeps_the_situation():
    sim = Sim(cap=10)
    while not sim.finished:
        await sim.say(balance=Balance.balanced)
    assert sim.answers_used < 10
    asked = [d.seed_index for _, d in sim.log if d.seed_segment == "current_affairs"]
    assert 2 in asked


# --- must_ask (§9.4, CTO_NOTES_14 §7.3) -------------------------------------------------------


@pytest.mark.parametrize("cap", [5, 6, 7, 10, 15, None])
async def test_a_plain_run_asks_the_situation_inside_the_budget(cap: int | None):
    sim = Sim(cap=cap)
    await sim.plain_until_done()
    total = sim.agenda.total_budget
    assert sim.answers_used == total and sim.turn_index == total  # `==` for plain answers
    assert sim.log[-1][1].close_reason == "agenda"
    asks = [d for _, d in sim.log if d.seed_segment == "current_affairs"]
    must = [
        d for d in asks
        if sim.agenda.segment("current_affairs").seeds[d.seed_index].must_ask
    ]  # fmt: skip
    assert len(must) == 1
    budget = sim.agenda.segment("current_affairs").budget
    # Nov 7: a situation with a twist is asked one place early and its twist takes the last place
    # (inside the budget); with room for only one question there is no twist.
    last_asked = sim.log[-(1 + 1)][1]
    if budget == 1:
        return
    if last_asked.move.rule_id == "D-TWIST":
        assert sim.log[-(1 + 2)][1] is must[0]
    else:
        assert last_asked is must[0]


def test_next_seed_at_budget_one_returns_the_must_ask_at_index_two():
    agenda = build_fallback_agenda(FORM, capped_budgets(5))
    segment = agenda.segment("current_affairs")
    index, seed = next_seed(segment, opening_memory(agenda), 1)
    assert index == 2 and seed.must_ask


def test_next_seed_goes_by_questions_left_not_index():
    agenda = build_fallback_agenda(FORM)
    segment = agenda.segment("current_affairs")
    memory = opening_memory(agenda)
    assert next_seed(segment, memory, 4)[0] == 0
    assert next_seed(segment, memory, 3)[0] == 0
    # Nov 7: a situation with a twist waits for the last two places, not the last one.
    assert next_seed(segment, memory, 2)[0] == 2
    assert next_seed(segment, memory, 1)[0] == 2
    assert next_seed(segment, memory, 0) is None


def test_next_seed_falls_to_the_must_ask_when_nothing_else_is_left():
    agenda = build_fallback_agenda(FORM)
    segment = agenda.segment("current_affairs")
    memory = opening_memory(agenda).model_copy(update={"asked_seeds": {segment.id: [0, 1]}})
    assert next_seed(segment, memory, 3)[0] == 2
    done = memory.model_copy(update={"asked_seeds": {segment.id: [0, 1, 2]}})
    assert next_seed(segment, done, 3) is None


async def test_at_budget_one_the_situation_is_the_first_question():
    sim = Sim(cap=5)
    await answers(sim, 4)
    _, d = sim.log[-1]
    assert d.seed_segment == "current_affairs" and d.seed_index == 2


async def test_a_strategist_move_on_with_budget_left_becomes_the_situation():
    sim = Sim()
    await answers(sim, 11)
    assert sim.topic == "current_affairs" and sim.turns_in_topic == 0
    await sim.say("Pata nahi sir.", honest_unknown=True)  # R3 reassure, first time
    d = await sim.say("Pata nahi sir.", honest_unknown=True)
    assert d.move.move == Move.next_question and d.seed_index == 2


async def test_a_skipped_situation_counts_as_asked():
    sim = Sim(cap=7)
    while sim.log == [] or sim.log[-1][1].seed_index != 2:
        await sim.say()
    d = await sim.say(SKIP)
    # Nov 7: the situation now takes the second-last place, so a skip may leave one plain
    # question; the situation is never asked again and the sitting ends on the agenda.
    rest = [d] if d.finished else [d, *await sim.plain_until_done()]
    again = [x for x in rest if x.seed_segment == "current_affairs" and x.seed_index == 2]
    assert again == [] and rest[-1].finished and rest[-1].close_reason == "agenda"


async def test_change_topic_can_leave_the_situation_unasked():
    sim = Sim()
    await answers(sim, 11)
    d = await sim.say(CHANGE)
    assert d.finished and d.close_reason == "agenda"
    asked = sim.director.memory.asked_seeds.get("current_affairs", [])
    assert 2 not in asked


async def test_wrap_up_can_leave_the_situation_unasked():
    sim = Sim()
    await answers(sim, 11)
    sim.wrap_up_due = True
    d = await sim.say()
    assert d.finished and d.close_reason == "time"


# --- Opener, seeds, R8 upgrade ----------------------------------------------------------------


def test_the_opener_consumes_form_hr_seed_zero():
    agenda = build_fallback_agenda(FORM)
    memory = opening_memory(agenda)
    assert memory.asked_seeds == {"form_hr": [0]}
    assert memory.last_question == agenda.segment("form_hr").seeds[0].text
    assert next_seed(agenda.segment("form_hr"), memory, 2)[0] == 1


async def test_at_form_hr_budget_one_the_first_answer_hands_off():
    d = await Sim(cap=5).say()
    assert d.node == "handoff" and d.move.target == "home_district"


async def test_the_spoken_opener_is_what_a_repeat_replays():
    sim = Sim()
    sim.director = PanelDirector("spoken", sim.agenda, sim.settings, opener_question="Spoken?",
                                 graph=compile_director_graph())  # fmt: skip
    assert (await sim.say(REPEAT)).repeat_text == "Spoken?"


async def test_a_second_r8_probe_on_one_seed_becomes_the_next_seed():
    sim = Sim()
    await answers(sim, 2)
    assert (await sim.say()).move.move == Move.probe
    d = await sim.say()
    assert d.move.move == Move.next_question and d.seed_index == 1 and d.move.rule_id == "D-SEED"
    assert d.move.pressure == PRESSURE[Move.next_question]


async def test_a_prepared_subject_gets_one_follow_up_when_a_later_question_remains():
    sim = Sim(agenda=custom_agenda(3))
    d = await sim.say("Maine gig workers ke social security boards ko follow kiya hai.")
    assert d.move.move == Move.probe and d.move.rule_id == "D-BRIEF"
    again = await sim.say("Gig workers ki income uncertain rehti hai, registration door hai.")
    assert again.move.rule_id != "D-BRIEF"


async def test_a_substantive_answer_gets_one_probe_before_move_on_and_then_the_plan_resumes():
    sim = Sim(agenda=custom_agenda(5))
    turn, analysis = sim.build(
        "I would protect small repair shops first because local jobs depend on them."
    )
    turn = turn.model_copy(update={
        "base_move": turn.base_move.model_copy(update={"move": Move.move_on, "rule_id": "R5"})
    })
    decision = await sim.director.decide_turn(turn)
    assert decision.move.move == Move.probe and decision.move.rule_id == "D-ANSWER"
    sim.apply(decision, analysis, turn.raw_text)

    next_turn, next_analysis = sim.build(
        "The first step would be a short registration camp with the ward office."
    )
    next_turn = next_turn.model_copy(update={
        "base_move": next_turn.base_move.model_copy(update={"move": Move.counter, "rule_id": "R6"})
    })
    next_move = await sim.director.decide_turn(next_turn)
    sim.apply(next_move, next_analysis, next_turn.raw_text)
    assert next_move.move.move == Move.next_question
    assert next_move.move.rule_id == "D-SEED" and next_move.seed_index == 1


async def test_an_ordinary_answer_is_not_a_briefing_follow_up():
    sim = Sim(agenda=custom_agenda(3))
    d = await sim.say("Chess se maine yeh seekha ki jaldi faisla galat hota hai.")
    assert d.move.rule_id != "D-BRIEF"


async def test_a_search_briefing_follows_its_own_words_and_not_the_file():
    from mockboard.brain.affairs import AffairsNote

    note = AffairsNote(
        id="news-0", as_of="2026-11-03", field="news", words=("monsoon", "surplus"),
        line="The papers are arguing about a monsoon surplus.",
        source="Example Paper", source_url="https://example.com/monsoon",
    )
    sim = Sim(agenda=custom_agenda(3))
    sim.director.briefing = (note,)
    heard = await sim.say("I have been reading about the monsoon surplus in the papers.")
    assert heard.move.move == Move.probe and heard.move.rule_id == "D-BRIEF"
    other = Sim(agenda=custom_agenda(3))
    other.director.briefing = (note,)
    gig = await other.say("Maine gig workers ke social security boards ko follow kiya hai.")
    assert gig.move.rule_id != "D-BRIEF"


async def test_a_seed_resets_the_probe_count():
    sim = Sim()
    await answers(sim, 2 + 2)
    await sim.say("")  # free: folds the seed
    assert sim.director.memory.probes_on_seed == 0


async def test_no_seed_is_asked_twice_in_a_session():
    for cap in (5, 10, None):
        sim = Sim(cap=cap)
        await sim.plain_until_done()
        await sim.director.finish()
        for indexes in sim.director.memory.asked_seeds.values():
            assert len(indexes) == len(set(indexes))


# --- Repeat replays the stored question (CTO_NOTES_14 §7.5) -----------------------------------


async def test_two_repeats_after_a_hand_off_are_identical_with_no_stacked_prefix():
    sim = Sim()
    await answers(sim, 1)
    handoff = await sim.say()
    assert handoff.node == "handoff"
    one = await sim.say(REPEAT)
    two = await sim.say(REPEAT)
    assert one.repeat_text == two.repeat_text == handoff.seed
    # One prefix each, never stacked; the table's wording rotates, so two repeats in a row differ
    # (Ruling 73 item 3) and the first is the first wording.
    assert len(one.preface) == len(two.preface) == 1
    assert texts(one) == [hi(REPEAT_PREFIX_LINES)]
    assert texts(two) == [line_for(REPEAT_PREFIX_LINES, "hi-en", 1)] != texts(one)
    assert one.move.member == "administrator"


async def test_a_repeat_after_a_skip_replays_the_seed_not_the_ack():
    sim = Sim()
    await answers(sim, 2)
    skipped = await sim.say(SKIP)
    d = await sim.say(REPEAT)
    assert d.repeat_text == skipped.seed


async def test_a_repeat_after_a_rephrase_replays_the_original():
    sim = Sim()
    await answers(sim, 2)
    await sim.say(CLARIFY)
    d = await sim.say(REPEAT)
    assert d.repeat_text == sim.log[1][1].seed


async def test_a_repeat_after_a_declined_end_repeats_without_either_line_stacking():
    sim = Sim()
    await sim.say(END)
    resumed = await sim.say("nahi")
    d = await sim.say(REPEAT)
    assert d.repeat_text == resumed.repeat_text
    assert hi(END_DECLINED_LINES) not in (d.repeat_text or "")


# --- Free turns run out (§9.3) ----------------------------------------------------------------


async def test_the_fifth_repeat_counts():
    sim = Sim()
    costs = [(await sim.say(REPEAT)).free for _ in range(5)]
    assert costs == [True, True, True, True, False]
    assert sim.log[-1][1].counted and sim.log[-1][1].move.move == Move.repeat_question
    assert sim.free_used == 4 and sim.answers_used == 1


async def test_free_turns_zero_is_the_strict_reading():
    sim = Sim(free_turns=0)
    assert (await sim.say(REPEAT)).counted
    assert (await sim.say("")).counted
    assert (await sim.say("xx", garbled=True)).counted


async def test_counted_silence_keeps_its_response_and_the_question():
    sim = Sim(free_turns=0)
    await sim.say()
    await sim.say()
    d = await sim.say("")
    assert d.move.move == Move.reassure and d.counted
    assert (await sim.say(REPEAT)).repeat_text == sim.log[1][1].seed


async def test_the_session_cap_on_a_legal_last_unit_keeps_its_real_reason():
    sim = await to_last_unit(Sim(cap=10, free_turns=0))
    turn, _ = sim.build("", latency=None)
    assert turn.base_move.rule_id == "CAP"
    d = await sim.director.decide_turn(turn)
    assert d.close_reason == "answers" and d.director_error is None


@pytest.mark.parametrize(
    ("text", "kw"),
    [(REPEAT, {}), (CLARIFY, {}), ("", {}), ("xx yy", {"garbled": True})],
)
async def test_a_counted_free_kind_on_the_last_unit_gives_the_cap_close(text: str, kw: dict):
    sim = await to_last_unit(Sim(cap=10, free_turns=0))
    d = await sim.say(text, **kw)
    assert d.finished and d.close_reason == "answers" and d.counted
    assert texts(d) == [hi(CAP_CLOSE_LINES)] and d.move.member == "chairman"


async def test_a_counted_repeat_on_a_segments_last_unit_hands_off():
    sim = Sim(cap=10, free_turns=0)
    d = await sim.say(REPEAT)
    assert d.node == "handoff" and d.counted


async def test_a_free_repeat_on_the_last_unit_does_not_spend_it():
    sim = await to_last_unit(Sim(cap=10))
    d = await sim.say(REPEAT)
    assert d.free and not d.finished
    d = await sim.say()
    assert d.finished and d.close_reason == "agenda"


# --- Close reasons ----------------------------------------------------------------------------


async def test_close_agenda_uses_the_plain_line_at_answer_ten():
    sim = await to_last_unit(Sim(cap=10))
    d = await sim.say()
    assert d.close_reason == "agenda" and texts(d) == [hi(CLOSE_LINES)]
    assert d.move.move == Move.move_on and d.move.target == CLOSE_TARGET
    assert d.move.member == "chairman" and d.line_complete


async def test_close_time_uses_the_wrap_up_line():
    sim = Sim(wrap_up_due=True)
    d = await sim.say()
    assert d.close_reason == "time" and texts(d) == [hi(WRAP_UP_CLOSE_LINES)]


async def test_the_session_cap_move_closes_as_ceiling_not_as_a_move_on():
    sim = Sim(max_turns=1, strict=False)
    turn, _ = sim.build(ANSWER)
    assert turn.base_move.rule_id == "CAP" and turn.base_move.move == Move.move_on
    d = await sim.director.decide_turn(turn)
    assert d.close_reason == "ceiling" and d.finished and texts(d) == [hi(CLOSE_LINES)]
    assert d.node == "close" and d.director_error and "ceiling" in d.director_error


async def test_ceiling_outranks_a_free_turn_and_a_new_end():
    for text in (REPEAT, END):
        sim = Sim(max_turns=1, strict=False)
        d = await sim.say(text)
        assert d.close_reason == "ceiling" and not d.pending_end


async def test_a_confirmed_end_outranks_the_ceiling():
    sim = Sim(strict=False)
    await sim.say(END)
    sim.max_turns = 2
    d = await sim.say("haan")
    assert d.close_reason == "end_request"


# --- Budgets come only from the agenda (CTO_NOTES_14 §7.8) ------------------------------------


async def test_budgets_are_read_from_the_agenda():
    sim = Sim(agenda=custom_agenda(1, 1, 2))
    await sim.plain_until_done()
    assert sim.answers_used == 4 and sim.finished
    assert [d.node for _, d in sim.log] == ["handoff", "handoff", "next_question", "close"]


def test_the_director_never_names_the_default_budgets():
    source = Path(director_module.__file__).read_text(encoding="utf-8")
    assert "DEFAULT_SEGMENT_BUDGETS" not in source and "capped_budgets" not in source


async def test_max_answers_none_and_the_top_cap_run_the_same_path():
    plain, top = Sim(cap=None), Sim(cap=15)
    await plain.plain_until_done()
    await top.plain_until_done()
    assert plain.settings.max_answers is None
    assert [d for _, d in plain.log] == [d for _, d in top.log]


async def test_answers_can_finish_under_the_budget_total():
    sim = Sim(cap=10)
    await answers(sim, 1)
    await sim.say(CHANGE)
    await sim.say(CHANGE)
    await sim.plain_until_done()
    assert sim.finished and sim.answers_used < 10


@pytest.mark.parametrize("cap", [5, 7, 10, 15, None])
@pytest.mark.parametrize("seed", range(6))
async def test_a_random_mix_keeps_every_bound(cap: int | None, seed: int):
    rng = random.Random(seed)
    sim = Sim(cap=cap)
    total = sim.agenda.total_budget
    turns = [ANSWER] * 8 + [REPEAT, CLARIFY, "", SKIP, CHANGE, END, "nahi", "haan", "xx"]
    while not sim.finished:
        text = rng.choice(turns)
        d = await sim.say(text, **({"garbled": True} if text == "xx" else {}))
        assert sim.answers_used <= total
        assert sim.free_used <= PANEL_FREE_TURNS
        assert sim.turn_index <= total + PANEL_FREE_TURNS
        assert d.move.move in UPSC_MOVES
    assert sim.log[-1][1].close_reason in ("agenda", "answers", "end_request")


# --- Bad ids reach the fallback (CTO_NOTES_14 §7.8) -------------------------------------------


async def test_a_bad_segment_id_reaches_the_director_error_fallback():
    sim = Sim(strict=False)
    turn, _ = sim.build(ANSWER)
    turn.ctx.topic = "no_such_segment"
    d = await sim.director.decide_turn(turn)
    assert d.director_error and "KeyError" in d.director_error
    assert d.node == "base" and d.move == turn.base_move.model_copy(update={"for_turn": "t1"})
    assert d.counted


async def test_a_missing_persona_name_reaches_the_fallback():
    sim = Sim(strict=False)
    sim.director.cfg = sim.settings.model_copy(update={"names": {}})
    await sim.say()
    d = await sim.say()  # the hand-off needs a name
    assert d.director_error and "KeyError" in d.director_error


# --- The graph --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        [ANSWER] * 4,
        [ANSWER, REPEAT, CLARIFY, ANSWER, CHANGE, ANSWER],
        [ANSWER, ANSWER, SKIP, END, "nahi", ANSWER, END, "haan"],
        ["", "", ANSWER, CHANGE, CHANGE, CHANGE, CHANGE],
    ],
)
async def test_the_graph_and_the_pure_function_agree(script: list[str]):
    sim = Sim(cap=10)
    for text in script:
        if sim.finished:
            break
        turn, analysis = sim.build(text)
        memory = sim.director.memory
        memory = fold_turns(memory, sim.director._queued)[0]
        pure = decide_panel(turn, sim.agenda, memory, sim.settings)
        graph = await sim.director.decide_turn(turn)
        assert graph == pure
        sim.apply(graph, analysis, text)


async def test_every_route_is_a_graph_node():
    nodes = set(compile_director_graph().get_graph().nodes)
    assert {"commit_last", "understand_turn", "handle_request", "handoff", "next_question",
            "close"} <= nodes  # fmt: skip


async def test_deciding_the_same_turn_twice_changes_nothing():
    sim = Sim()
    await answers(sim, 2)
    turn, _ = sim.build(CHANGE)
    first = await sim.director.decide_turn(turn)
    memory = sim.director.memory
    logs = (list(sim.director.requests), list(sim.director.decisions))
    second = await sim.director.decide_turn(turn)
    assert first == second
    assert sim.director.memory == memory
    assert (sim.director.requests, sim.director.decisions) == logs


async def test_a_reopened_turn_re_decides_on_the_longer_text():
    sim = Sim()
    await answers(sim, 2)
    turn, analysis = sim.build("Mujhe lagta hai")
    assert (await sim.director.decide_turn(turn)).node == "next_question"
    longer, analysis = sim.build(SKIP)
    d = await sim.director.decide_turn(longer)
    assert d.request_kind == "skip"
    sim.apply(d, analysis, SKIP)
    await sim.say()
    assert sim.director.memory.committed_turns.count("t3") == 1
    assert sim.answers_used == 4


async def test_a_turn_queued_twice_folds_once():
    sim = Sim()
    await sim.say(CHANGE)
    done = sim.director._queued[0]
    sim.director.queue_completed(done)
    await sim.say()
    memory = sim.director.memory
    assert memory.committed_turns == ["t1"]
    assert len(memory.notes) == 1 and memory.avoid_terms == ["tourism"]
    assert [r.kind for r in sim.director.requests] == ["change_topic"]
    assert [d.turn_id for d in sim.director.decisions] == ["t1"]


def test_fold_turns_is_idempotent():
    agenda = build_fallback_agenda(FORM)
    sim = Sim()
    turn, analysis = sim.build(ANSWER)
    d = decide_panel(turn, agenda, opening_memory(agenda), sim.settings)
    done = CompletedTurn(analysis=analysis, decision=d, asked="Q?")
    once, requests, decisions = fold_turns(opening_memory(agenda), [done])
    twice, more_requests, more_decisions = fold_turns(once, [done])
    assert once == twice and not more_requests and not more_decisions
    assert len(decisions) == 1 and not requests


def test_a_fallback_analysis_leaves_an_empty_gist_and_no_raw_text():
    agenda = build_fallback_agenda(FORM)
    sim = Sim()
    turn, _ = sim.build(ANSWER)
    d = decide_panel(turn, agenda, opening_memory(agenda), sim.settings)
    analysis = Analysis(turn_id="t1", is_fallback=True, stance="should not appear")
    done = CompletedTurn(analysis=analysis, decision=d)
    memory, _, _ = fold_turns(opening_memory(agenda), [done])
    assert memory.notes[0].gist == ""


def test_a_gist_is_capped_and_paraphrased():
    agenda = build_fallback_agenda(FORM)
    sim = Sim()
    turn, _ = sim.build(ANSWER)
    d = decide_panel(turn, agenda, opening_memory(agenda), sim.settings)
    stance = " ".join(f"s{i}" for i in range(40))  # a piece is trimmed to 15 words, then the whole
    reasons = [" ".join(f"{k}{i}" for i in range(40)) for k in ("r", "q", "p")]  # note to 30
    analysis = Analysis(turn_id="t1", stance=stance, reasons=reasons)
    done = CompletedTurn(analysis=analysis, decision=d)
    memory, _, _ = fold_turns(opening_memory(agenda), [done])
    assert len(memory.notes[0].gist.split()) == 30
    assert ANSWER not in memory.notes[0].gist


async def test_a_whole_session_makes_no_model_call(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the director called the model")

    for owner in (llm_module._BaseClient, llm_module.BedrockNovaClient, llm_module.FakeLLMClient):
        for name in ("structured", "stream_text", "_tool_call"):
            if hasattr(owner, name):
                monkeypatch.setattr(owner, name, refuse)
    sim = Sim(cap=10)
    for text in (ANSWER, REPEAT, CHANGE, CLARIFY, SKIP, "", END, "nahi", ANSWER):
        await sim.say(text)
    await sim.plain_until_done()
    assert sim.finished


def test_the_director_module_imports_no_model_code():
    tree = ast.parse(Path(director_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in imported if m.endswith((".llm", ".actor", ".listener", ".deps"))}


async def test_the_final_turn_is_folded_and_the_thread_deleted():
    graph = compile_director_graph()
    sim = Sim(session_id="to-delete", graph=graph)
    await sim.say()
    await sim.say(CHANGE)
    config = {"configurable": {"thread_id": "to-delete"}}
    assert await graph.checkpointer.aget_tuple(config) is not None
    memory = await sim.director.finish()
    assert memory.committed_turns == ["t1", "t2"]
    assert [r.kind for r in sim.director.requests] == ["change_topic"]
    assert await graph.checkpointer.aget_tuple(config) is None


async def test_threads_of_two_sessions_do_not_mix():
    graph = compile_director_graph()
    one, two = Sim(session_id="a", graph=graph), Sim(session_id="b", graph=graph)
    await one.say(CHANGE)
    await one.say()
    await two.say()
    await two.say()
    assert one.director.memory.avoid_terms == ["tourism"] and not two.director.memory.avoid_terms


class BrokenGraph:
    """A graph that fails, then optionally works, with a real checkpointer."""

    def __init__(self, working=None, failures: int = 10**6):
        self.working, self.failures = working, failures
        self.checkpointer = (working or compile_director_graph()).checkpointer

    async def ainvoke(self, *args, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("graph broke")
        return await self.working.ainvoke(*args, **kwargs)


async def test_a_broken_graph_falls_back_to_the_pure_function_and_honours_the_request():
    sim = Sim(graph=BrokenGraph(), strict=False)
    d = await sim.say(CHANGE)
    assert d.director_error == "RuntimeError: graph broke"
    assert d.node == "handoff" and d.request_kind == "change_topic"


async def test_a_turn_finished_during_a_broken_graph_is_not_lost_or_doubled():
    graph = BrokenGraph(compile_director_graph(), failures=2)
    sim = Sim(session_id="recovers", graph=graph, strict=False)
    await sim.say(END)
    d = await sim.say("haan")  # still broken: the fallback must see `pending_end`
    assert d.finished and d.close_reason == "end_request"
    sim.finished = False
    await sim.director.finish()
    assert sim.director.memory.committed_turns == ["t1", "t2"]
    assert len(sim.director.decisions) == 2


async def test_finish_still_folds_with_a_broken_graph():
    sim = Sim(graph=BrokenGraph(), strict=False)
    await sim.say(CHANGE)
    memory = await sim.director.finish()
    assert memory.committed_turns == ["t1"] and memory.avoid_terms == ["tourism"]
    assert [r.kind for r in sim.director.requests] == ["change_topic"]


async def test_a_failed_first_run_still_seeds_the_memory():
    graph = BrokenGraph(compile_director_graph(), failures=1)
    sim = Sim(session_id="late-seed", graph=graph, strict=False)
    question = sim.director.memory.last_question
    await sim.say("")
    d = await sim.say(REPEAT)
    assert d.director_error is None and d.repeat_text == question


class _SaverThatCannotDelete:
    async def adelete_thread(self, thread_id: str) -> None:
        raise RuntimeError("delete failed")


async def test_finish_survives_a_failing_delete(monkeypatch):
    sim = Sim()
    await answers(sim, 2)
    monkeypatch.setattr(sim.director._graph, "checkpointer", _SaverThatCannotDelete())
    assert (await sim.director.finish()).asked_seeds


async def test_finish_survives_a_graph_with_no_checkpointer(monkeypatch):
    sim = Sim()
    await answers(sim, 2)
    monkeypatch.setattr(sim.director._graph, "checkpointer", None)
    assert (await sim.director.finish()).asked_seeds


# --- Ruling 73 item 3: wording rotates, so no fixed line repeats back to back -------------------


async def test_three_repeats_in_a_row_use_three_different_wordings_then_wrap():
    sim = Sim()
    await answers(sim, 1)
    said = [texts(await sim.say(REPEAT))[0] for _ in range(4)]
    assert said[:3] == list(REPEAT_PREFIX_LINES.variants["hi-en"])
    assert said[3] == said[0] and said[3] != said[2]  # wraps, still never twice in a row


async def test_two_skips_do_not_acknowledge_in_the_same_words():
    sim = Sim()
    await answers(sim, 2)
    first = texts(await sim.say(SKIP))[0]
    second = texts(await sim.say(SKIP))[0]
    assert first == hi(SKIP_ACK_LINES) and second == line_for(SKIP_ACK_LINES, "hi-en", 1)


async def test_every_handoff_in_a_session_has_a_different_wording_from_the_one_before():
    sim = Sim()
    chair: list[str] = []
    for _ in range(sim.agenda.total_budget):
        if sim.finished:
            break
        d = await sim.say()
        chair += [p.text for p in d.preface if p.kind == "handoff"]
    assert len(chair) == 4
    assert all(a != b for a, b in itertools.pairwise(chair))


async def test_the_first_handoff_alone_is_marked_first():
    sim = Sim()
    flags = []
    for _ in range(sim.agenda.total_budget):
        if sim.finished:
            break
        d = await sim.say()
        if d.node == "handoff":
            flags.append(d.panel.first_handoff)
    assert flags == [True, False, False, False]


async def test_a_reopened_turn_picks_the_same_wording_because_only_a_fold_counts():
    sim = Sim()
    await answers(sim, 1)
    turn, _analysis = sim.build(REPEAT)
    one = await sim.director.decide_turn(turn)
    two = await sim.director.decide_turn(turn)  # re-decided, never applied (Ruling 31)
    assert one.preface == two.preface


async def test_a_wording_is_counted_once_per_folded_turn_and_the_table_is_named():
    sim = Sim()
    await answers(sim, 2)  # the second answer hands off to Menon
    await sim.say(REPEAT)
    await sim.say(REPEAT)  # folds the first repeat
    assert sim.director.memory.line_uses["repeat_prefix"] == 1
    assert sim.director.memory.line_uses.get("handoff") == 1
