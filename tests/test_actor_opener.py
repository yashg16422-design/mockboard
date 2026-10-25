"""Opener (ruling 5): open-ended, no place fact without a card. Actor guards: 30-word cap
in code and ask-never-assert for fact challenges."""

from __future__ import annotations

import pytest

from conftest import make_claim
from mockboard.brain.actor import (
    ACKNOWLEDGE_LINES,
    compose_line,
    count_words,
    generate_opener,
    opener_template,
    opener_violations,
    stream_capped,
)
from mockboard.brain.fake_responders import opener_responder
from mockboard.brain.llm import FakeLLMClient, LLMTransientError
from mockboard.brain.schemas import BoardMove, Move


async def opener_with(deps, form, reply):
    llm = FakeLLMClient(script={"opener": [reply]})
    return await generate_opener(
        llm, deps.kit, form, deps.cards, max_words=40, max_tokens=120, turn_id="t0"
    )


async def test_fake_opener_is_open_ended_and_accepted(deps, form):
    llm = FakeLLMClient(responders={"opener": opener_responder})
    line = await generate_opener(
        llm, deps.kit, form, deps.cards, max_words=40, max_tokens=120, turn_id="t0"
    )
    assert not line.guard and line.text.endswith("?")
    assert opener_violations(line.text, deps.cards) == []


@pytest.mark.parametrize(
    "reply, problem",
    [
        ("Kanpur is famous for leather. What do you think about it?", "place"),
        ("Kanpur has the highest number of tanneries. How would you regulate them?", "place"),
        ("Kanpur ki literacy 80 percent hai. Isse kaise badhayenge?", "number"),
        ("Do you like Kanpur?", "open-ended"),
        ("Tell us about your district.", "question"),
        ("Kanpur sabse bada industrial city hai. Aap kya sochte hain?", "place"),
    ],
)
async def test_opener_guard_replaces_bad_lines(deps, form, reply, problem):
    problems = opener_violations(reply, deps.cards)
    assert any(problem in p for p in problems), problems
    line = await opener_with(deps, form, reply)
    assert line.guard == "opener_guard" and line.text == opener_template(form)


async def test_opener_citing_unknown_card_is_rejected(deps, form):
    reply = "Card [made_up_card] ke hisaab se tourism 50 percent hai. Aap kya sochte hain?"
    assert any("unknown cards" in p for p in opener_violations(reply, deps.cards))


def test_opener_template_itself_passes_the_guard(deps, form):
    assert opener_violations(opener_template(form), deps.cards) == []


async def test_opener_falls_back_when_model_fails(deps, form):
    llm = FakeLLMClient(script={"opener": [LLMTransientError("throttled")]})
    line = await generate_opener(
        llm, deps.kit, form, deps.cards, max_words=40, max_tokens=120, turn_id="t0"
    )
    assert line.is_fallback and line.text == opener_template(form)


def test_opener_user_offers_only_sourced_place_cards(deps, form):
    from mockboard.brain.actor import build_opener_user

    user = build_opener_user(form, deps.cards)
    assert "<allowed_facts>\n[]\n</allowed_facts>" in user  # built-in cards are synthetic


async def test_word_cap_enforced_in_code():
    async def chunks():
        for _ in range(100):
            yield "word "

    out = "".join([c async for c in stream_capped(chunks(), 30)])
    assert count_words(out) == 30


def challenge_move():
    return BoardMove(
        move=Move.fact_challenge, target="t0.s0.c0", member="economist", reason="R4: test"
    )


async def _compose(deps, form, reply, claim=None, card=None):
    llm = FakeLLMClient(script={"actor": [reply]})
    return await compose_line(
        llm,
        deps.kit,
        deps.personas["economist"],
        challenge_move(),
        topic="tourism",
        form=form,
        last_question="q",
        last_answer="a",
        max_words=30,
        max_tokens=120,
        turn_id="t1",
        claim=claim,
        card=card,
    )


@pytest.mark.parametrize(
    "reply",
    [
        "Aap galat hain, card 9 percent kehta hai.",
        "You are wrong about that figure, aren't you?",
        "Card kehta hai 9 percent.",
    ],
)
async def test_fact_challenge_must_ask_never_assert(deps, form, reply):
    card = deps.cards.get("builtin_tourism_gdp_share")
    line = await _compose(deps, form, reply, make_claim(), card)
    assert line.guard == "ask_never_assert"
    assert line.text.endswith("?") and card.statement in line.text


async def test_good_fact_challenge_passes(deps, form):
    card = deps.cards.get("builtin_tourism_gdp_share")
    line = await _compose(
        deps,
        form,
        "Synthetic test card 9 percent batata hai. Aap ise kaise reconcile karenge?",
        make_claim(),
        card,
    )
    assert not line.guard


async def test_fact_challenge_without_card_is_never_spoken(deps, form):
    line = await _compose(deps, form, "irrelevant", make_claim(), None)
    assert line.guard == "no_card" and line.is_fallback
    assert "?" in line.text and "card" not in line.text.lower()


async def test_acknowledgement_preface_is_spoken_first_and_within_the_cap(deps, form):
    move = BoardMove(move=Move.probe, member="economist", reason="R8: x", preface="acknowledge")
    llm = FakeLLMClient(script={"actor": [" ".join(["shabd"] * 40) + "?"]})
    line = await compose_line(
        llm, deps.kit, deps.personas["economist"], move, topic="tourism", form=form,
        last_question="q", last_answer="a", max_words=30, max_tokens=120, turn_id="t2",
    )
    assert line.text.startswith(ACKNOWLEDGE_LINES["hi-en"])
    assert count_words(line.text) <= 30


async def test_acknowledgement_follows_the_interview_language(deps, form):
    move = BoardMove(move=Move.probe, member="economist", reason="R8: x", preface="acknowledge")
    english = form.model_copy(update={"interview_language": "english"})
    llm = FakeLLMClient(script={"actor": ["Why do you say so?"]})
    line = await compose_line(
        llm, deps.kit, deps.personas["economist"], move, topic="tourism", form=english,
        last_question="q", last_answer="a", max_words=30, max_tokens=120, turn_id="t2",
    )
    assert line.text == f"{ACKNOWLEDGE_LINES['en']} Why do you say so?"
