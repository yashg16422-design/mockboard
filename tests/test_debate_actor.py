"""Debate actor (DB4): personas, the actor prompt, the number guard, the closing concession,
word caps, restate check and fallback lines."""

from __future__ import annotations

import pytest

from mockboard.brain.cards import load_cards
from mockboard.brain.llm import FakeLLMClient, LLMThrottledError
from mockboard.brain.prompts import load_prompt
from mockboard.brain.schemas import BoardMove, DebateContext, Move, OpponentArg
from mockboard.config import Settings
from mockboard.modes import get_profile
from mockboard.modes.debate.actor import (
    STATE_MOVES,
    DebateBrief,
    apply_guards,
    compose_debate_line,
    move_template,
)
from mockboard.modes.debate.guards import (
    allowed_figures,
    cards_for_topics,
    has_concession,
    number_guard,
    restates_user,
    sentence_allowed,
)
from mockboard.modes.debate.motions import Side, load_curated_motions
from mockboard.modes.debate.numbers import mentions_number
from mockboard.modes.debate.profile import DEBATE_WORD_CAPS
from mockboard.modes.debate.strategist import DEBATE_MOVES, OPPONENT, WARRANT_TARGET

MOTIONS = load_curated_motions()


def brief_for(motion_id: str, cards=(), *, side: Side = Side.for_, rnd: str = "clash",
              mix: str = "hi-en", **debate) -> DebateBrief:
    m = MOTIONS[motion_id]
    case = m.opponent_case(side)
    mcards = cards_for_topics(list(cards), m.card_topics)
    return DebateBrief(
        motion=m.text,
        user_side=side.value,
        round=rnd,
        case=case,
        debate=DebateContext(
            motion=m.text,
            user_side=side.value,
            opponent_args=[OpponentArg(id=a.id, claim=a.claim, keywords=a.keywords)
                           for a in case],
            card_topics=m.card_topics,
            **debate,
        ),
        cards=mcards,
        allowed=allowed_figures(mcards, m.text),
        mix=mix,
    )


def mv(move: Move, target: str | None = "A1", member: str = OPPONENT, rule: str = "D12",
       **kw) -> BoardMove:
    return BoardMove(move=move, target=target, member=member, reason=f"{rule}: test",
                     rule_id=rule, **kw)


@pytest.fixture
def rt(deps):
    return deps.mode("debate")


def test_debate_personas_and_kit(rt):
    assert rt.profile.id == "debate"
    assert set(rt.personas) == {"moderator", "opponent_logician", "opponent_wit"}
    assert rt.personas["moderator"].role == "chairman" and rt.strategy.chairman == "moderator"
    for p in rt.personas.values():
        assert len(p.fallback_lines) >= 2
        assert not any(mentions_number(line) for line in p.fallback_lines)
    assert "## The debate board (persona pack)" in rt.kit.shared_prefix
    assert rt.kit.shared_prefix.startswith(load_prompt("shared_debate", 1).body[:40])
    assert "{{max_words}}" in load_prompt("actor_debate", 1).body


def test_word_caps_follow_the_design():
    assert DEBATE_WORD_CAPS["opening_statement"] == 70
    assert DEBATE_WORD_CAPS["closing_statement"] == 60
    profile = get_profile("debate")
    assert all(profile.word_cap(m, 30) > 0 for m in DEBATE_MOVES)


# ---------------------------------------------------------------------------
# Number guard
# ---------------------------------------------------------------------------


def test_number_guard_drops_uncarded_figures_and_keeps_the_rest():
    allowed = allowed_figures([], MOTIONS["smartphones_in_schools"].text)
    assert allowed == frozenset({10.0})  # "Class 10" in the motion text
    line = ("Class 10 tak phones band hone chahiye. Studies show 40 percent students are "
            "distracted. Teen lakh bachche roz phone laate hain. Dhyan bhatakta hai.")
    out = number_guard(line, allowed)
    assert out.text == "Class 10 tak phones band hone chahiye. Dhyan bhatakta hai."
    assert len(out.dropped) == 2


def test_number_guard_allows_only_the_motions_own_cards():
    s = Settings(_env_file=None)
    cards = load_cards(s.cards_dir, s.topics)  # the data cards motions 5 and 6 cite
    b = brief_for("tourism_hill_towns", cards.cards.values())
    assert {c.topic for c in b.cards} == {"tourism"}
    assert sentence_allowed("Exampleland test data ke hisaab se yeh 12 percent hai.", b.allowed)
    assert sentence_allowed("Yeh 2023 ka figure hai.", b.allowed)
    assert not sentence_allowed("Yeh 13 percent hai.", b.allowed)
    assert not sentence_allowed("Budget 900 crore hai.", b.allowed)  # security card, not ours
    assert not sentence_allowed("Aadhe log sehmat hain.", b.allowed)  # number word alone
    # Motion 1 has no cards: the opponent argues qualitatively.
    assert brief_for("smartphones_in_schools", cards.cards.values()).cards == []


def test_restate_check_is_soft():
    assert restates_user("Aap kehte hain ki phones distract karte hain. Lekin...",
                         "Phones distract karte hain class mein")
    assert not restates_user("Lekin apps madad karti hain.", "Phones distract karte hain")


# ---------------------------------------------------------------------------
# Guards on whole lines
# ---------------------------------------------------------------------------


def test_closing_without_a_concession_gets_one_inside_the_cap():
    b = brief_for("smartphones_in_schools", answered_ids=["A2"])
    text = " ".join(["Phones are tools and bans never teach responsibility."] * 10)
    line = apply_guards(text, mv(Move.closing_statement, "A1"), b, last_answer="x",
                        max_words=60)
    assert line.concession_added and has_concession(line.text)
    assert line.text.startswith("Maanna padega ki 'Parents need to reach children")
    assert len(line.text.split()) <= 60
    ok = apply_guards("Fair point on safety. But phones are tools.",
                      mv(Move.closing_statement), b, last_answer="x", max_words=60)
    assert not ok.concession_added


def test_over_long_lines_are_capped_and_restate_is_logged():
    b = brief_for("gap_year")
    line = apply_guards("word " * 100, mv(Move.rebut), b, last_answer="gap year", max_words=45)
    assert len(line.text.split()) == 45 and line.restate_ok is False


# ---------------------------------------------------------------------------
# compose_debate_line end to end (fake model)
# ---------------------------------------------------------------------------


async def test_opening_statement_uses_two_case_arguments(rt, deps):
    b = brief_for("smartphones_in_schools", rnd="opening")
    line = await compose_debate_line(
        deps.llm, rt.kit, rt.personas["opponent_logician"], mv(Move.opening_statement, "A1",
                                                                rule="D5"),
        b, last_question="Moderator intro", last_answer="Phones distract students in class.",
        max_words=70, max_tokens=200, turn_id="t0",
    )
    assert not line.is_fallback and line.arguments_used == ("A1", "A2")
    assert "learning tools" in line.text and line.restate_ok
    assert len(line.text.split()) <= 70
    call = deps.llm.calls_named("actor")[-1]
    assert "<debate_board>" in call.user and "## Your role now: DEBATE BOARD" in call.system


async def test_user_figure_restated_by_the_opponent_is_dropped(rt, deps):
    b = brief_for("smartphones_in_schools")
    line = await compose_debate_line(
        deps.llm, rt.kit, rt.personas["opponent_wit"], mv(Move.rebut, "A1"), b,
        last_question="q", last_answer="80 percent bachche phone se distract hote hain.",
        max_words=45, max_tokens=200, turn_id="t1",
    )
    assert "80" not in line.text and line.dropped_sentences
    assert "number_guard" in line.guard


async def test_acknowledge_preface_is_spoken_first(rt, deps):
    b = brief_for("gap_year")
    line = await compose_debate_line(
        deps.llm, rt.kit, rt.personas["opponent_wit"],
        mv(Move.rebut, "A1", preface="acknowledge"), b, last_question="q",
        last_answer="Mujhe nahi pata", max_words=45, max_tokens=200, turn_id="t1",
    )
    assert line.text.startswith("Theek hai, yeh maanna achhi baat hai.")
    assert len(line.text.split()) <= 45


@pytest.mark.parametrize("move", sorted(DEBATE_MOVES - {Move.fact_challenge}))
async def test_actor_failure_falls_back_safely(rt, move):
    llm = FakeLLMClient(script={"actor": [LLMThrottledError("throttled")]})
    b = brief_for("street_food", rnd="closing" if move == Move.closing_statement else "clash")
    target = {Move.move_on: "closing", Move.cross_question: "A2"}.get(move, "A1")
    member = OPPONENT if move not in (Move.redirect, Move.reassure, Move.clarify, Move.move_on,
                                      Move.interrupt) else "moderator"
    persona = rt.personas["moderator" if member == "moderator" else "opponent_wit"]
    line = await compose_debate_line(
        llm, rt.kit, persona, mv(move, target, member=member), b, last_question="q",
        last_answer="a", max_words=60, max_tokens=200, turn_id="t3",
    )
    assert line.is_fallback and "LLMThrottledError" in line.error
    if move in STATE_MOVES:
        assert line.fallback_kind == "move_template"
    else:
        assert line.fallback_kind == "persona_line" and line.text in persona.fallback_lines
    assert not mentions_number(line.text)


async def test_after_a_filler_every_move_uses_its_template(rt):
    llm = FakeLLMClient(script={"actor": [LLMThrottledError("throttled")]})
    line = await compose_debate_line(
        llm, rt.kit, rt.personas["opponent_wit"], mv(Move.rebut, "A1"),
        brief_for("street_food"), last_question="q", last_answer="a", max_words=45,
        max_tokens=200, turn_id="t3", filler_played=lambda: True,
    )
    assert line.fallback_kind == "move_template"
    assert line.text.startswith("Aapki baat samajh aati hai.")


async def test_guards_leaving_nothing_fall_back_to_the_template(rt):
    llm = FakeLLMClient(script={"actor": ["Studies show 70 percent agree. 9 in 10 do."]})
    line = await compose_debate_line(
        llm, rt.kit, rt.personas["opponent_wit"], mv(Move.rebut, "A1"),
        brief_for("street_food"), last_question="q", last_answer="a", max_words=45,
        max_tokens=200, turn_id="t3",
    )
    assert line.is_fallback and line.fallback_kind == "move_template"
    assert len(line.dropped_sentences) == 2 and not mentions_number(line.text)


@pytest.mark.parametrize("motion_id", sorted(MOTIONS))
@pytest.mark.parametrize("mix", ["hi-en", "en"])
def test_every_template_is_figure_free_for_every_motion(motion_id, mix):
    m = MOTIONS[motion_id]
    for side in Side:
        for answered in ([], ["A1", "F1"]):
            b = brief_for(motion_id, side=side, mix=mix, answered_ids=answered)
            for move in DEBATE_MOVES - {Move.fact_challenge}:
                for target in (b.case[0].id, WARRANT_TARGET, "closing", "close", None):
                    text = move_template(mv(move, target), b)
                    allowed = allowed_figures([], m.text)
                    assert text and all(sentence_allowed(s, allowed)
                                        for s in text.split(". ")), (move, text)
