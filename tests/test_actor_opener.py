"""Opener (ruling 5): open-ended, no place fact without a card. Actor guards: 30-word cap
in code and ask-never-assert for fact challenges."""

from __future__ import annotations

import json
import re

import pytest

from conftest import make_claim
from mockboard.brain.actor import (
    ACKNOWLEDGE_LINES,
    build_actor_user,
    compose_line,
    count_words,
    fact_challenge_template,
    generate_opener,
    opener_template,
    opener_violations,
    stream_capped,
    stream_line,
)
from mockboard.brain.fake_responders import opener_responder
from mockboard.brain.llm import FakeLLMClient, LLMTransientError
from mockboard.brain.schemas import BoardMove, ClaimKind, Move


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


@pytest.mark.parametrize("max_words", [1, 7, 30])
async def test_stream_line_stops_at_exactly_max_words(deps, max_words):
    """`stream_line` is the path to Timbre in voice mode (P0.2): the cap it passes on must be
    the cap it was given, not one that later guards happen to trim."""
    reply = " ".join(f"w{i}" for i in range(60))
    llm = FakeLLMClient(script={"actor": [reply]})
    out = "".join(
        [
            c
            async for c in stream_line(
                llm, deps.kit, "user", max_words=max_words, max_tokens=120, tag_="t0"
            )
        ]
    )
    assert count_words(out) == max_words
    assert out.split() == [f"w{i}" for i in range(max_words)]


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


def assert_challenge_shape(text: str, claim_fig: str, card_fig: str, year: str | None):
    """The required shape (ruling 11), checked as properties rather than exact wording."""
    assert text[0].isupper(), text
    assert text.endswith("?"), text
    assert "test card" not in text.lower(), text
    assert claim_fig in text and card_fig in text, text
    assert text.index(claim_fig) < text.index(card_fig), "candidate's figure comes first"
    if year:
        assert year in text, text
    # Every sentence starts with a capital letter (sentence case).
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        assert sentence[0].isupper(), sentence


@pytest.mark.parametrize(
    "reply",
    [
        "Aap galat hain, card 9 percent kehta hai.",
        "You are wrong about that figure, aren't you?",
        "Card kehta hai 9 percent.",
        "Aapne 30 percent kaha, lekin data kuch aur kehta hai. Aapka source kya hai?",
        "BUILT-IN TEST CARD: Testland tourism share of GDP is 9 percent in 2023. Kya kahenge?",
    ],
    ids=["asserts", "asserts_en", "no_question", "no_card_figure", "reads_card_text"],
)
async def test_bad_fact_challenge_lines_are_replaced_by_the_template(deps, form, reply):
    card = deps.cards.get("builtin_tourism_gdp_share")
    line = await _compose(deps, form, reply, make_claim(), card)
    assert line.guard == "ask_never_assert"
    assert line.text == fact_challenge_template(make_claim(), card)
    assert_challenge_shape(line.text, "30 percent", "9 percent", "2023")


@pytest.mark.parametrize(
    "reply",
    [
        "Aapne 30 percent kaha. Hamare test data mein 2023 ka figure 9 percent hai. "
        "Aapka figure kahan se hai?",
        "Aapne 30% kaha tha, par 2023 ke data mein yeh 9% hai. Yeh aapne kahan padha?",
    ],
)
async def test_good_fact_challenge_passes(deps, form, reply):
    card = deps.cards.get("builtin_tourism_gdp_share")
    line = await _compose(deps, form, reply, make_claim(), card)
    assert not line.guard and line.text == reply


def test_template_shape_percent(deps):
    card = deps.cards.get("builtin_tourism_gdp_share")
    text = fact_challenge_template(make_claim(), card)
    assert text == (
        "Aapne 30 percent kaha. Hamare test data ke 2023 ke figure ke hisaab se yeh 9 percent "
        "hai. Aapka figure kahan se hai?"
    )


def test_template_renders_both_figures_in_the_cards_scale(deps):
    """The CTO's experiment: a claim of 1,500,000 visitors against a card in lakh used to be
    spoken as "1,500,000 visitors"; now both sides say lakh."""
    card = deps.cards.get("builtin_tourism_arrivals")  # 15 lakh visitors, 2022
    claim = make_claim(value=2_500_000, unit="visitors", year=2022,
                       claim_en="Testland got 25 lakh foreign tourist arrivals in 2022")
    text = fact_challenge_template(claim, card)
    assert_challenge_shape(text, "25 lakh visitors", "15 lakh visitors", "2022")
    assert "2500000" not in text and "2,500,000" not in text


def test_template_uses_source_short_when_the_card_has_one(deps):
    card = deps.cards.get("builtin_tourism_gdp_share").model_copy(
        update={"source_short": "Exampleland test data"}
    )
    text = fact_challenge_template(make_claim(), card)
    assert "Exampleland test data ke 2023 ke figure" in text


def test_template_date_claim(deps):
    card = deps.cards.get("builtin_tourism_policy_year")  # 2018
    claim = make_claim(kind=ClaimKind.date, value=2012, unit="year", year=2012)
    text = fact_challenge_template(claim, card)
    assert text == (
        "Aapne saal 2012 kaha. Hamare test data ke hisaab se yeh saal 2018 hai. "
        "Aapka figure kahan se hai?"
    )


@pytest.mark.parametrize(
    "mix, start, ask",
    [
        ("en", "You said 30 percent.", "Where is your figure from?"),
        ("hi", "आपने 30 प्रतिशत कहा।", "आपका आँकड़ा कहाँ से है?"),
    ],
)
def test_template_follows_the_language_mix(deps, mix, start, ask):
    card = deps.cards.get("builtin_tourism_gdp_share")
    text = fact_challenge_template(make_claim(), card, mix)
    assert text.startswith(start) and text.endswith(ask)


def test_actor_prompt_gets_structured_fields_not_the_card_text(deps, form):
    card = deps.cards.get("builtin_tourism_gdp_share")
    user = build_actor_user(
        deps.personas["economist"], challenge_move(), topic="tourism", form=form,
        last_question="q", last_answer="a", claim=make_claim(), card=card,
    )
    assert card.statement not in user
    facts = json.loads(user.split("<fact_challenge>\n")[1].split("\n</fact_challenge>")[0])
    assert facts == {
        "claim_figure": "30 percent",
        "card_figure": "9 percent",
        "card_year": 2023,
        "is_date": False,
        "source_short": "hamare test data",
        "language_mix": "hi-en",
    }


async def test_fake_actor_speaks_the_template(deps, form):
    from mockboard.brain.fake_responders import actor_responder

    card = deps.cards.get("builtin_tourism_gdp_share")
    llm = FakeLLMClient(responders={"actor": actor_responder})
    line = await compose_line(
        llm, deps.kit, deps.personas["economist"], challenge_move(), topic="tourism",
        form=form, last_question="q", last_answer="a", max_words=30, max_tokens=120,
        turn_id="t1", claim=make_claim(), card=card,
    )
    assert not line.guard
    assert line.text == fact_challenge_template(make_claim(), card)


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
