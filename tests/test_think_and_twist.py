"""Owner's requests of Nov 7: time to think and long answers, and scenarios that raise the stakes.

1. "Ek minute sochne dijiye" (or any language's "a moment, please") gets one fixed line, keeps the
   same question, costs no answer, and makes the next turn a long one.
2. Every DM/SP situation has a twist, asked once after a real answer, inside the budget, and the
   situation and its twist are long turns.
"""

from __future__ import annotations

import pytest

from director_sim import Sim
from mockboard.brain.agenda import (
    SITUATIONS,
    TWISTS,
    is_situation,
    seed_violations,
    twist_for,
)
from mockboard.brain.panel_lines import THINK_LINES
from mockboard.brain.requests import detect_request
from mockboard.brain.requests_ta import detect_tamil_request
from mockboard.brain.schemas import CandidateForm
from mockboard.brain.think_time import is_think_request

LONG_ANSWER = (
    "Sir, main pehle sabse zyada prabhavit logon ki pehchaan karunga, kyunki rahat wahi pahunchni "
    "chahiye jahan zaroorat sabse zyada hai, aur phir panchayat se milkar vitran ki suchi banaunga."
)

# --- 1. time to think ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Ek minute sochne dijiye.", "Sir, ek minute.", "Mujhe thoda samay chahiye.",
    "Soch ke batata hoon sir", "Let me think.", "Can I take a moment, sir?",
    "Give me a minute please.", "Just a moment.", "एक मिनट सोचने दीजिए।", "सोचकर बताता हूँ",
    "ஒரு நிமிடம் யோசிக்கிறேன்.", "Konjam neram kudunga sir",
])  # fmt: skip
def test_a_moment_to_think_is_recognised_in_every_language(text):
    assert is_think_request(text)
    # It never collides with the frozen detector or the Tamil layer.
    assert detect_request(text) is None and detect_tamil_request(text) is None


@pytest.mark.parametrize("text", [
    "Ek minute mein kaam ho jaata hai.", "Ek minute ka kaam hai", "Sochne ki zaroorat nahi hai.",
    "I don't need a moment.", "Phir se boliye.",
    "Let me think about the farmers, because the mandi system takes most of their profit.",
    "Mujhe lagta hai ki pehle zameeni sthiti samajhni chahiye.",
])  # fmt: skip
def test_ordinary_answers_are_not_a_request_to_think(text):
    assert not is_think_request(text)


async def test_think_keeps_the_question_costs_nothing_and_makes_a_long_turn():
    sim = Sim()
    await sim.say()  # answer the opener
    before = sim.director.memory.last_question
    used = sim.answers_used
    d = await sim.say("Ek minute sochne dijiye.")
    assert d.move.rule_id == "D-THINK" and d.thinking and d.line_complete
    assert d.request_kind == "think" and not d.counted
    assert [p.text for p in d.preface][-1] in THINK_LINES.variants["hi-en"]
    assert sim.answers_used == used
    nxt = await sim.say(LONG_ANSWER)
    assert sim.director.memory.last_question == before or nxt.move.rule_id != "D-THINK"
    assert not nxt.thinking


async def test_think_is_not_taken_during_an_end_confirmation():
    sim = Sim()
    await sim.say()
    await sim.say("interview yahin khatam karte hain")
    d = await sim.say("Ek minute sochne dijiye.")
    assert d.move.rule_id != "D-THINK"


def test_every_think_line_exists_in_every_mix():
    for mix in ("hi-en", "en", "hi", "ta-en"):
        assert len(THINK_LINES.variants[mix]) == 3


# --- 2. scenarios and twists --------------------------------------------------------------------


def test_six_situations_each_with_a_twist_in_every_language():
    assert len(SITUATIONS) == len(TWISTS) == 6
    for situation, twist in zip(SITUATIONS, TWISTS, strict=True):
        assert set(situation) == set(twist) == {"hi-en", "en", "hi", "ta-en"}
        for mix, text in situation.items():
            assert twist_for(text) == twist[mix]
            assert twist_for(f"Theek hai. {text}") == twist[mix]  # inside a spoken line
            assert is_situation(text)
    assert twist_for("Aapko civil services ki taraf kis baat ne modha?") is None


def test_every_situation_passes_the_question_validator():
    form = CandidateForm(home_state="Uttarakhand", degree_subject="Physics")
    for situation in SITUATIONS:
        for text in situation.values():
            assert seed_violations(text, "current_affairs", form) == []


def test_twists_name_no_party_caste_or_community_and_no_he_or_she():
    banned = ("party", "caste", "jaati", "religion", "dharm", "hindu", "muslim", " he ", " she ",
              " his ", " her ")
    for twist in TWISTS:
        for text in twist.values():
            low = f" {text.lower()} "
            assert not any(b in low for b in banned), text


async def _run_to_situation(sim: Sim) -> str:
    """Answer plainly until the board has just asked the situation; return it."""
    for _ in range(40):
        d = await sim.say()
        if d.seed is not None and is_situation(d.seed):
            return d.seed
        assert not d.finished, "the sitting ended before the situation"
    raise AssertionError("no situation")


async def test_a_real_answer_to_the_situation_gets_its_twist_inside_the_budget():
    sim = Sim()
    situation = await _run_to_situation(sim)
    d = await sim.say(LONG_ANSWER)
    assert d.move.rule_id == "D-TWIST" and d.line_complete
    assert d.repeat_text == twist_for(situation)
    await sim.say(LONG_ANSWER)  # the twist is folded in: asked once
    assert sim.director.memory.last_question != situation
    rest = await sim.plain_until_done()
    assert all(x.move.rule_id != "D-TWIST" for x in rest)
    assert sim.answers_used == sim.agenda.total_budget


async def test_a_short_or_unknown_answer_gets_no_twist():
    sim = Sim()
    await _run_to_situation(sim)
    d = await sim.say("Pata nahi sir.")
    assert d.move.rule_id != "D-TWIST"


@pytest.mark.parametrize("cap", [5, 6])
async def test_a_short_capped_sitting_has_no_room_and_no_twist(cap):
    sim = Sim(cap=cap)
    decisions = await sim.plain_until_done()
    if sim.agenda.segment("current_affairs").budget == 1:
        assert all(d.move.rule_id != "D-TWIST" for d in decisions)
    assert sim.answers_used <= sim.agenda.total_budget


def test_the_report_shows_a_think_turn_as_a_request():
    """First real run (Nov 8): the think turn was listed as an "answer" in "How the board heard
    you"; it is a request for time, in plain words."""
    from mockboard.brain.report_panel import REQUEST_KINDS, REQUEST_WORDS

    assert "think" in REQUEST_KINDS
    assert REQUEST_WORDS["think"] == "take a moment to think"
