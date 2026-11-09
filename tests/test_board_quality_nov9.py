"""The owner's live sitting of Nov 9 (`traces/voice-5a189debc5`), and what it changed.

1. "hello sir" was taken as a thin answer and pressed ("Aapne kaha 'hello sir'..."): a turn that
   is only a greeting now gets a greeting back and the same question, free (`D-GREET`).
2. A member's line opened by grading the answer ("Aapka jawab sahi hai."), echoed the
   Chairperson's thanks ("Shukriya. Ab Doctor Iyer ki baari hai. Shukriya."), and began a new
   member's first question with "Aapke is point par,": code removes all three.
3. A communal answer was treated as merely one-sided. The listener (v7) flags `prejudice`, and the
   member answers with the Constitution's equal dignity and one question on the candidate's duty
   (`D-VALUES`), or says the line before the hand-off when the segment is spent.
"""

from __future__ import annotations

import asyncio

import pytest

from director_sim import Sim
from mockboard.brain.greeting_turn import is_greeting_only
from mockboard.brain.panel_actor import (
    clean_lead_sentence,
    grades_the_answer,
    strip_lead,
    thanks_said,
    without_lead,
)
from mockboard.brain.panel_lines import (
    GREETING_BACK_LINES,
    VALUES_LINES,
    VALUES_QUESTION_LINES,
)
from mockboard.brain.prompts import load_prompt
from mockboard.brain.report_panel import REQUEST_KINDS
from mockboard.brain.requests import detect_request
from mockboard.brain.schemas import Analysis
from mockboard.brain.strategist import merge_analyses
from mockboard.brain.think_time import is_think_request
from mockboard.brain.tools import PanelListenerOutput

MIXES = ("hi-en", "en", "hi", "ta-en")

# --- 1. a greeting is not an answer -------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "hello sir", "हेलो सर", "Good morning, ma'am.", "Namaste sir ji", "Hello? Hello?",
    "Good morning to all the board members", "வணக்கம் சார்",
])  # fmt: skip
def test_a_greeting_alone_is_recognised(text):
    assert is_greeting_only(text)
    assert detect_request(text) is None and not is_think_request(text)


@pytest.mark.parametrize("text", [
    "hello sir main Lucknow se hoon", "sir", "haan ji", "thank you sir",
    "Hi, I think governance matters", "",
])  # fmt: skip
def test_anything_more_than_a_greeting_is_an_answer(text):
    assert not is_greeting_only(text)


async def test_a_greeting_gets_a_greeting_back_and_the_same_question_free():
    sim = Sim()
    question = sim.director.memory.last_question
    used = sim.answers_used
    d = await sim.say("hello sir")
    assert d.move.rule_id == "D-GREET" and d.line_complete and not d.counted
    assert d.request_kind == "greeting"
    assert d.preface[-1].text in GREETING_BACK_LINES.variants["hi-en"]
    assert d.repeat_text == question
    assert sim.answers_used == used
    nxt = await sim.say()  # a real answer to the same question is an answer again
    assert nxt.counted and nxt.move.rule_id != "D-GREET"


async def test_no_greeting_turn_while_an_end_is_being_confirmed():
    sim = Sim()
    await sim.say()
    await sim.say("interview yahin khatam karte hain")
    d = await sim.say("hello sir")
    assert d.move.rule_id != "D-GREET"


def test_greeting_lines_exist_in_every_mix_and_the_report_knows_the_kind():
    for mix in MIXES:
        assert len(GREETING_BACK_LINES.variants[mix]) == 2
    assert "greeting" in REQUEST_KINDS


# --- 2. how a member's line may open ------------------------------------------------------------


@pytest.mark.parametrize("sentence", [
    "Aapka jawab sahi hai.", "Bilkul sahi.", "Very good.", "Your answer is correct.",
    "Aapka uttar accha tha.", "आपका जवाब सही है।",
])  # fmt: skip
def test_a_sentence_that_grades_the_answer_is_dropped(sentence):
    assert grades_the_answer(sentence)
    assert clean_lead_sentence(sentence) == ""


@pytest.mark.parametrize("sentence", [
    "Is jawab mein sahi kya hai, aur galat kya?", "Achha.", "Theek hai.",
    "Aapne kaha ki physics ne aapko kuch naya nahi sikhaaya.",
])  # fmt: skip
def test_questions_and_plain_acknowledgements_stay(sentence):
    assert not grades_the_answer(sentence)


def test_after_a_hand_off_an_echoed_thanks_and_a_point_lead_go():
    chair = "Shukriya. Ab Doctor Iyer ki baari hai."
    member = "Shukriya. Aapke is point par, Physics ki padhai ne aapke sochne ko kaise badla?"
    assert without_lead(member, after_handoff=True, echo=thanks_said(chair)) == (
        "Physics ki padhai ne aapke sochne ko kaise badla?"
    )


def test_a_thanks_in_other_words_than_the_chairs_stays():
    """actor.v5 asks a member to thank the chair in different words: that is kept."""
    chair = "Dhanyavaad. Shrimati Menon, please."
    member = "Ji, shukriya. Aapne kaunsi ek samasya kareeb se dekhi?"
    assert without_lead(member, after_handoff=True, echo=thanks_said(chair)) == member


def test_the_point_lead_stays_when_the_member_continues_their_own_segment():
    line = "Aapke is point par, iska doosra paksh kya hai?"
    assert without_lead(line, after_handoff=False) == line


@pytest.mark.parametrize("piece", [1, 3, 7, 40])
def test_the_stream_says_what_the_whole_text_says(piece):
    line = "Shukriya. Aapka jawab sahi hai. Aapke is point par, aap kya badlenge, aur kyun?"
    echo = thanks_said("Shukriya. Ab Doctor Iyer ki baari hai.")

    async def chunks():
        for i in range(0, len(line), piece):
            yield line[i : i + piece]

    async def run() -> str:
        return "".join([c async for c in strip_lead(chunks(), after_handoff=True, echo=echo)])

    streamed = asyncio.run(run())
    whole = without_lead(line, after_handoff=True, echo=echo)
    assert " ".join(streamed.split()) == whole == "Aap kya badlenge, aur kyun?"


# --- 3. a communal answer gets the board's answer -----------------------------------------------

COMMUNAL = "Mujhe lagta hai woh log achhe nahi hote, unko shehar se hata dena chahiye."


async def test_a_prejudiced_answer_gets_the_equal_dignity_line_and_a_duty_question():
    sim = Sim()
    d = await sim.say(COMMUNAL, prejudice=True)
    assert d.move.rule_id == "D-VALUES" and d.line_complete and d.counted
    assert d.preface[-1].text == VALUES_LINES.variants["hi-en"][0]
    assert d.repeat_text == VALUES_QUESTION_LINES.variants["hi-en"][0]
    assert d.preface[-1].member == d.move.member  # the member who heard it says it
    await sim.say()  # memory folds a turn when the next one is decided
    assert sim.director.memory.last_question == VALUES_QUESTION_LINES.variants["hi-en"][0]
    assert sim.director.memory.probes_on_seed == 1  # one follow-up, then the plan resumes


async def test_a_second_prejudiced_answer_gets_the_second_wording():
    sim = Sim()
    await sim.say(COMMUNAL, prejudice=True)
    await sim.say()
    d = await sim.say(COMMUNAL, prejudice=True)
    if d.move.rule_id == "D-VALUES":
        assert d.preface[-1].text == VALUES_LINES.variants["hi-en"][1]
        assert d.repeat_text == VALUES_QUESTION_LINES.variants["hi-en"][1]
    else:  # the segment was spent: the line comes before the hand-off instead
        assert any(p.text == VALUES_LINES.variants["hi-en"][1] for p in d.preface)


async def test_when_the_segment_is_spent_the_line_comes_before_the_hand_off():
    sim = Sim()
    while sim.turns_in_topic + 1 < sim.agenda.segment(sim.topic).budget:
        await sim.say()  # answer until the next answer would hand over
    d = await sim.say(COMMUNAL, prejudice=True)
    assert d.node == "handoff"
    texts = [p.text for p in d.preface]
    assert VALUES_LINES.variants["hi-en"][0] in texts
    assert texts.index(VALUES_LINES.variants["hi-en"][0]) < len(texts) - 1  # before the chair


async def test_an_ordinary_answer_and_a_request_get_no_values_line():
    sim = Sim()
    assert (await sim.say()).move.rule_id != "D-VALUES"
    d = await sim.say("Ek minute sochne dijiye.", prejudice=True)  # a request wins
    assert d.move.rule_id == "D-THINK"


def test_values_lines_name_no_community_carry_no_digit_and_exist_in_every_mix():
    for table in (VALUES_LINES, VALUES_QUESTION_LINES):
        for mix in MIXES:
            assert len(table.variants[mix]) == 2
            for line in table.variants[mix]:
                low = line.lower()
                assert not any(ch.isdigit() for ch in line)
                for word in ("muslim", "hindu", "sikh", "christian", "dalit", "brahmin"):
                    assert word not in low


def test_the_listener_flag_merges_and_a_failed_segment_cannot_raise_it():
    yes = Analysis(prejudice=True, word_count=12)
    no = Analysis(word_count=5)
    failed = Analysis(prejudice=True, is_fallback=True)
    assert merge_analyses([no, yes]).prejudice
    assert not merge_analyses([no, failed]).prejudice
    assert PanelListenerOutput.model_validate({
        "stance": "", "reasons": [], "claims": [], "balance": "unclear", "honest_unknown": False,
        "concedes_error": False, "garbled": False,
    }).prejudice is False


def test_listener_v7_defines_prejudice_narrowly():
    body = load_prompt("listener", 7).body
    assert "- prejudice: true only when" in body
    assert "criticises a policy, a government or one person's act" in body
    assert load_prompt("listener", 6).body.count("prejudice") == 0



# --- Round 56: the live voice run of Nov 9 (generated candidate speech, real Prisma, Nova, Timbre)


@pytest.mark.parametrize("reply", [
    "हाँ सर प्लीज", "हां सर, प्लीज़।", "हाँ जी, ख़त्म कीजिए।", "हां सर, थैंक यू।",
])  # fmt: skip
def test_a_devanagari_yes_closes(reply):
    """Prisma wrote "Haan sir, please" as "हाँ सर प्लीज" and the board resumed instead of ending."""
    from mockboard.brain.director import is_clear_yes

    assert is_clear_yes(reply)


@pytest.mark.parametrize("reply", [
    "हां सर, प्लीज़?", "हां लेकिन एक सवाल और", "नहीं सर, प्लीज़ जारी रखिए",
])  # fmt: skip
def test_a_doubtful_devanagari_reply_still_resumes(reply):
    from mockboard.brain.director import is_clear_yes

    assert not is_clear_yes(reply)


def _listened(text: str, honest: bool) -> Analysis:
    from mockboard.brain.listener import to_analysis
    from mockboard.brain.schemas import Segment

    out = PanelListenerOutput.model_validate({
        "stance": "", "reasons": [], "claims": [], "balance": "unclear",
        "honest_unknown": honest, "concedes_error": False, "garbled": False,
    })
    return to_analysis(out, turn_id="t4", turn_index=4,
                       segment=Segment(segment_id="s0", segment_index=0, text=text))


def test_a_lone_ji_is_not_an_honest_i_dont_know():
    """The live run: "जी" before a full answer was marked "I don't know", the move was R3, and
    the board asked the same question again."""
    assert not _listened("जी", honest=True).honest_unknown
    assert not _listened("haan sir", honest=True).honest_unknown
    assert _listened("pata nahi", honest=False).honest_unknown  # the marker still decides
    assert _listened("नहीं पता", honest=True).honest_unknown
    assert _listened("is vishay par meri jaankari kam hai", honest=True).honest_unknown


def test_the_panel_waits_longer_for_novas_first_word_than_debate(settings):
    assert settings.panel_actor_first_token_budget_s == 3.5
    assert settings.actor_first_token_budget_s == 2.5  # debate and the partner unchanged


# --- Round 57: the owner's order, the case study dug into, a candid board, answers-only report ---


@pytest.mark.parametrize(("answer", "signals"), [
    ("Shayad haan, mujhe lagta hai ki yeh theek hai.", ["unsure", "thin", "no_reason"]),
    ("Haan important hai.", ["thin"]),
    ("Main traffic ke liye flyover banaunga aur public transport sudharunga aur signal "
     "theek karunga.", ["thin", "no_reason"]),
    ("Main pehle logon se milunga kyunki unhe zameeni sthiti pata hai, phir panchayat ke saath "
     "kaam shuru karunga taaki sab ka bharosa bane.", []),
    ("शायद मैं पुलिस बुला लूँगा, मुझे पक्का नहीं पता।", ["unsure", "thin", "no_reason"]),
    ("", []),
])  # fmt: skip
def test_answer_signals(answer, signals):
    from mockboard.brain.panel_actor import answer_signals

    assert answer_signals(answer) == signals


def test_signals_reach_the_member_only_on_a_follow_up():
    import json

    from mockboard.brain.panel_actor import board_context
    from mockboard.brain.personas import load_personas
    from mockboard.brain.schemas import BoardMove, CandidateForm, Move

    persona = load_personas()["economist"]
    form = CandidateForm(home_state="Kerala", degree_subject="Physics")
    weak = "Shayad, pata nahi."

    def ctx(move: Move) -> dict:
        m = BoardMove(move=move, member="economist", reason="test", rule_id="R7")
        text = board_context(persona, m, form=form, spoken=[], after_handoff=False,
                             last_answer=weak)
        return json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])

    assert ctx(Move.pressure)["answer_signals"] == ["unsure", "thin"]
    assert "answer_signals" not in ctx(Move.next_question)


def test_actor_v12_is_candid_and_keeps_v11():
    v11 = load_prompt("actor", 11).body
    v12 = load_prompt("actor", 12).body
    assert "Be candid, the way a real UPSC board is." in v12
    assert "Never praise a weak answer" in v12 and "do not invent a criticism" in v12
    assert "politely press" in v11 and "politely press" not in v12
    assert 'move_reason` says "case study"' in v12
    for kept in ("Never repeat the candidate's words back to them", "Never state a fact",
                 "Never ask the candidate for a number"):
        assert kept in v12.replace("\n  ", " ") or kept in v12, kept


def test_the_judge_reads_answers_only():
    from mockboard.brain.report_panel import answer_turns
    from mockboard.brain.schemas import BoardMove, CandidateTurn, Move, PanelRequest, Segment

    def turn(i: int, text: str) -> CandidateTurn:
        return CandidateTurn(turn_id=f"t{i}", turn_index=i, topic="degree", question="Q?",
                             segments=[Segment(segment_id=f"s{i}", segment_index=0, text=text)])

    turns = [turn(0, "namaste sir"), turn(1, "Ek lamba jawab kyunki..."), turn(2, "phir se boliye"),
             turn(3, "ek minute"), turn(4, "Doosra jawab."), turn(5, "khatam karein"),
             turn(6, "haan sir"), turn(7, "")]
    requests = [PanelRequest(turn_id=t, kind=k, source="rules")
                for t, k in [("t0", "greeting"), ("t2", "repeat"), ("t3", "think"), ("t5", "end")]]
    moves = [BoardMove(move=Move.clarify, member="chairman", reason="x", rule_id="D-END-CONFIRM",
                       for_turn="t5")]
    assert [t.turn_id for t in answer_turns(turns, requests, moves)] == ["t1", "t4"]


def test_the_practice_question_starts_at_the_question():
    from mockboard.brain.report_panel import _asked

    assert _asked("Shukriya. Ab Doctor Iyer ki baari hai. Physics se kya badla?") == (
        "Physics se kya badla?"
    )
    assert _asked("Dhanyavaad. Shrimati Menon, please. Aapke zile mein kya karenge?") == (
        "Aapke zile mein kya karenge?"
    )


def test_the_degree_questions_are_upsc_level_in_every_mix():
    from mockboard.brain.agenda import build_fallback_agenda, seed_violations
    from mockboard.brain.schemas import CandidateForm

    for lang in ("hinglish", "english", "hindi"):
        form = CandidateForm(home_state="Kerala", degree_subject="Physics", interview_language=lang)
        seeds = [s.text for s in build_fallback_agenda(form).segment("degree").seeds]
        assert all(seed_violations(s, "degree", form) == [] for s in seeds), seeds
        joined = " ".join(seeds).lower()
        assert "sochne ke tareeke" not in joined and "changed the way you think" not in joined


# --- Round 58: a hedge at the end of a segment is named before the hand-off ----------------------


async def test_a_hedged_last_answer_is_named_before_the_hand_off():
    from mockboard.brain.panel_lines import HEDGE_PRESS_LINES

    sim = Sim()
    await sim.say()  # the Chairperson's second question is the segment's last
    d = await sim.say("Shayad, mujhe pakka nahi pata ki kya karna chahiye sir.")
    assert d.node == "handoff"
    texts = [p.text for p in d.preface]
    assert texts[0] == HEDGE_PRESS_LINES.variants["hi-en"][0]
    assert d.preface[0].member == "chairman" and len(texts) == 2  # then the hand-off itself


async def test_an_honest_i_dont_know_or_a_firm_answer_is_not_pressed():
    from mockboard.brain.panel_lines import HEDGE_PRESS_LINES

    for text, kw in [("Pata nahi sir.", {"honest_unknown": True}), (None, {})]:
        sim = Sim()
        await sim.say()
        d = await sim.say(text, **kw) if text else await sim.say()
        assert all(p.text not in HEDGE_PRESS_LINES.variants["hi-en"] for p in d.preface)
