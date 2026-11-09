"""Fixed fallback lines speak the sitting's language, and spoken form values never name a college.

Before these fixes an English, Hindi or Tanglish sitting heard the Hinglish template whenever the
model line failed or a guard cut it, and "B.Tech from IIT Kanpur" was read aloud in a question.
"""

from __future__ import annotations

import re

import pytest

from mockboard.brain.actor import (
    FILLER_TEMPLATES,
    actor_fallback,
    filler_for,
    guard_question,
    opener_template,
)
from mockboard.brain.agenda import (
    build_fallback_agenda,
    capped_budgets,
    names_an_institution,
    seed_violations,
)
from mockboard.brain.panel_lines import CAP_CLOSE_LINES
from mockboard.brain.personas import load_personas
from mockboard.brain.schemas import BoardMove, CandidateForm, Move
from mockboard.brain.spoken import spoken_parts

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
TAMIL = re.compile(r"[஀-௿]")
HINGLISH_MARKERS = ("kijiye", "aap ", "achha", "theek", "shukriya", "bataiye", "kya ")
FILLER_MOVES = [m for m in FILLER_TEMPLATES]


@pytest.mark.parametrize("move", FILLER_MOVES)
def test_each_mix_has_its_own_filler(move: Move):
    en, hi, ta = filler_for(move, "en"), filler_for(move, "hi"), filler_for(move, "ta-en")
    assert en.isascii() and not any(m in en.lower() for m in HINGLISH_MARKERS), en
    assert DEVANAGARI.search(hi), hi
    assert TAMIL.search(ta), ta
    assert filler_for(move, "unknown-mix") == filler_for(move, "hi-en")


def test_hinglish_panel_fillers_are_not_masculine_first_person():
    for move in FILLER_MOVES:
        text = filler_for(move, "hi-en")
        assert "paaya" not in text and "rokta" not in text, text
        assert filler_for(move, "hi").find("पाया") == -1


def test_the_old_hinglish_templates_are_unchanged():
    """Debate and anything else reading FILLER_TEMPLATES keep their exact wording."""
    assert FILLER_TEMPLATES[Move.clarify].startswith("Maaf kijiye, main theek se sun nahi paaya")


@pytest.mark.parametrize("mix", ["en", "hi", "ta-en"])
def test_a_failed_actor_line_speaks_the_sitting_language(mix: str):
    persona = load_personas()["administrator"]
    move = BoardMove(move=Move.probe, member="administrator", reason="t", for_turn="t1")
    line = actor_fallback(move, persona, "t1", error="timeout", guard="", mix=mix)
    assert line.text == filler_for(Move.probe, mix) and line.is_fallback


def test_a_failed_hinglish_line_keeps_the_persona_line():
    persona = load_personas()["administrator"]
    move = BoardMove(move=Move.probe, member="administrator", reason="t", for_turn="t1")
    line = actor_fallback(move, persona, "t1", error="timeout", guard="", mix="hi-en")
    assert line.fallback_kind == "persona_line"


@pytest.mark.parametrize("mix", ["en", "hi", "ta-en"])
def test_a_guard_cut_move_on_finishes_in_the_sitting_language(mix: str):
    move = BoardMove(move=Move.move_on, member="chairman", reason="t", target="degree")
    question = guard_question(move, "degree", mix)
    assert question.endswith("?") and "aate hain" not in question


def test_institutions_are_never_spoken_from_the_form():
    form = CandidateForm(home_state="Uttar Pradesh", degree_subject="B.Tech from IIT Kanpur",
                         optional_subject="Sociology", hobbies=["chess"],
                         interview_language="english")
    agenda = build_fallback_agenda(form, capped_budgets(None))
    spoken = " ".join(seed.text for seg in agenda.segments for seed in seg.seeds)
    assert "IIT" not in spoken and "Kanpur" not in spoken and "BTech" not in spoken
    assert names_an_institution("B.Tech from I.I.T. Kanpur")
    assert names_an_institution("Delhi University")
    for ok in ("Electronics", "Public Administration", "Schooling of children", "History"):
        assert not names_an_institution(ok), ok
    assert "names a college or institute" in seed_violations(
        "What did your years at IIT teach you, and why?"
    )


def test_tamil_templates_use_a_case_safe_frame_for_form_values():
    form = CandidateForm(home_state="Tamil Nadu", district="Madurai",
                         degree_subject="Civil Engineering",
                         hobbies=["cricket"], optional_subject="Geography",
                         interview_language="tanglish")
    text = opener_template(form)
    assert "Madurai பகுதியைச் சேர்ந்தவர்" in text
    agenda = build_fallback_agenda(form, capped_budgets(None))
    spoken = " ".join(seed.text for seg in agenda.segments for seed in seg.seeds)
    assert "Engineeringயில்" not in spoken and "Maduraiக்கு" not in spoken
    assert "Geographyஐ" not in spoken


def test_the_tamil_cap_close_does_not_say_time_ran_out():
    """Ruling 49: the close at the answer cap is truthful about why it ends."""
    line = CAP_CLOSE_LINES["ta-en"]
    assert "நேரம் முடிந்தது" not in line and "வரம்பு" in line


def test_arab_is_a_number_in_tamil_not_a_people():
    assert "அரபு" not in spoken_parts(5, "arab", mix="ta-en").text
