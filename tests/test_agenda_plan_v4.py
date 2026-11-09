"""agenda_plan v4 (owner, Nov 7: richer board questions). It is written and tested here, but the
default stays v3 until one owner-approved Nova plan call shows its questions pass the checks."""

from __future__ import annotations

from mockboard.brain.agenda import MAX_SEED_WORDS
from mockboard.brain.agenda_plan import PLAN_MAX_WORDS, planned_seed_violations
from mockboard.brain.prompts import load_prompt
from mockboard.brain.schemas import CandidateForm

FORM = CandidateForm(home_state="Uttarakhand", district="Almora", degree_subject="Agriculture",
                     hobbies=["Trekking"], interview_language="hinglish")
# 33 words: a role from the form, then a choice with its trade-off.
RICH = ("Agar aap apne zile mein kisaanon ke liye Agriculture ki koi nayi pehal shuru karne ki "
        "zimmedari sambhalein, to aap pehle kis cheez ko chunenge, aur uske liye kya chhodna "
        "padega, aur kyun?")


def test_v4_keeps_every_v3_safety_rule_and_asks_for_richer_questions():
    v3 = load_prompt("agenda_plan", 3).body
    v4 = load_prompt("agenda_plan", 4).body
    for rule in ("Never state a fact", "Never name a person", "No digits",
                 "Do not copy a word from <briefing>", "never write\n  the name of the district"):
        assert rule.replace("\n  ", " ") in v4.replace("\n  ", " "), rule
        assert rule.replace("\n  ", " ") in v3.replace("\n  ", " "), rule
    assert "between 12 and 36 words" in v4 and "concrete role or\n  situation" in v4


def test_the_default_plan_prompt_is_still_v3(settings):
    assert settings.prompt_agenda_plan_version == 3


def test_a_rich_question_fits_v4_and_not_v3():
    assert 28 < len(RICH.split()) <= 36
    assert PLAN_MAX_WORDS[4] == 36 and MAX_SEED_WORDS == 28
    assert planned_seed_violations(RICH, "degree", FORM, max_words=PLAN_MAX_WORDS[4]) == []
    assert any("over 28 words" in p for p in planned_seed_violations(RICH, "degree", FORM))


def test_a_copied_kit_with_another_version_gets_that_version(settings):
    """`replace(kit, versions=...)` shares the prompt cache; it must still load the new version."""
    from dataclasses import replace

    from mockboard.brain.cards import builtin_test_cards
    from mockboard.brain.personas import load_personas
    from mockboard.brain.prompts import build_prompt_kit

    kit = build_prompt_kit(load_personas(), builtin_test_cards(), settings.prompt_versions())
    v3 = replace(kit, versions={**kit.versions, "agenda_plan": 3})
    v4 = replace(kit, versions={**kit.versions, "agenda_plan": 4})
    assert v3.prompt("agenda_plan").version == 3
    assert v4.prompt("agenda_plan").version == 4
    assert v3.prompt("agenda_plan").body != v4.prompt("agenda_plan").body
