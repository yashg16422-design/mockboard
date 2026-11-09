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


def test_the_default_plan_prompt_is_v5_written_blind(settings):
    """v3 and v4 stay on disk; v5 is the default since the Nov 9 Nova check (Round 55)."""
    assert settings.prompt_agenda_plan_version == 5


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


# --- agenda_plan v5 (Nov 9): the planner writes blind -------------------------------------------
# On Nova, v3 and v4 handed the template questions back word for word, so nothing new was ever
# accepted (Ruling 102, the owner's live sitting of Nov 9). v5 reads neither the templates nor the
# place. The questions below are the ones Nova wrote with v5 on Nov 9, for a fictional form.

V5_FORM = CandidateForm(home_state="Uttar Pradesh", district="Lucknow", degree_subject="Physics",
                        hobbies=["Chess"], work_experience="One year as a school science teacher",
                        interview_language="hinglish")


def _segments(user: str) -> list[dict]:
    import json

    return json.loads(user.split("<segments>\n")[1].split("\n</segments>")[0])


def test_v5_reads_no_template_question_and_no_place():
    from mockboard.brain.agenda import build_fallback_agenda
    from mockboard.brain.agenda_plan import build_plan_user

    fallback = build_fallback_agenda(V5_FORM)
    blind = build_plan_user(V5_FORM, fallback, "", 5)
    assert "Lucknow" not in blind and "Uttar Pradesh" not in blind
    assert all("template_questions" not in s for s in _segments(blind))
    assert all(s["questions_needed"] >= 1 and s["field"] for s in _segments(blind))
    assert "degree_subject: Physics" in blind and "hobbies: Chess" in blind
    templates = [seed.text for seg in fallback.segments for seed in seg.seeds]
    assert not any(t in blind for t in templates)
    old = build_plan_user(V5_FORM, fallback, "", 4)  # v4 and older read what they always did
    assert "Lucknow" in old and all("template_questions" in s for s in _segments(old))


def test_v5_prompt_keeps_every_hard_rule_and_drops_the_templates():
    v5 = load_prompt("agenda_plan", 5).body
    for rule in ("Never state a fact", "Never name a person", "No digits",
                 "Do not copy a word from <briefing>", "Never write the name of the district"):
        assert rule in v5.replace("\n  ", " "), rule
    assert "template questions" not in v5 and "aapke zile" in v5
    assert PLAN_MAX_WORDS[5] == 36


def test_novas_open_questions_from_the_v5_check_pass():
    for segment, text in [
        ("interests", "Chess khelne se aapko kya seekhne ko mila?"),
        ("interests", "Aapne Physics aur Chess ke beech kya connection dekha?"),
        ("degree", "Physics ke kis topic ne aapko sabse zyada attract kiya aur kyun?"),
        ("degree", "Aapne Physics ke konse concepts ko apne teaching experience mein apply kiya?"),
        ("home_district",
         "Aapke zile mein science education ka kya haal hai aur aap kya sudhar karenge?"),
    ]:
        assert planned_seed_violations(text, segment, V5_FORM, max_words=36) == [], text


def test_a_superlative_is_still_a_place_fact_in_a_statement_and_closed_questions_still_fail():
    assert "states a fact about a place" in planned_seed_violations(
        "Lucknow sabse bada shehar hai. Aap kya karenge?", "degree", V5_FORM, max_words=36)
    assert "states a fact about a place" in planned_seed_violations(
        "Aapke zile mein sabse badi samasya pani hai, kyun?", "home_district", V5_FORM,
        max_words=36)
    assert "states something before it asks" in planned_seed_violations(
        "Physics sabse kathin vishay hai, toh aap is par kya sochte hain?", "degree", V5_FORM,
        max_words=36)
    assert "question is not open-ended" in planned_seed_violations(
        "Kya aap roz Physics ki kitaabein padhte hain?", "degree", V5_FORM, max_words=36)
