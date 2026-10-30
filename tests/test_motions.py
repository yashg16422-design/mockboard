"""Debate motions (DB1): the 13 curated motions and their case files, the fail-closed safety
screen for custom motions, the custom case file, and the labelled screen eval gate."""

from __future__ import annotations

import pytest
from eval.motions.runner import load_items, run_items, summarize

from mockboard.brain.cards import load_cards
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.llm import FakeLLMClient, LLMThrottledError
from mockboard.config import Settings
from mockboard.modes.debate.case_file import build_custom_case, generic_case
from mockboard.modes.debate.motions import (
    MAX_MOTION_CHARS,
    CaseArgument,
    Side,
    case_card_problems,
    find_motion,
    load_curated_motions,
    normalise_motion,
)
from mockboard.modes.debate.numbers import mentions_number
from mockboard.modes.debate.screen import ScreenDecision, ScreenLayer, rule_screen, screen_motion

# CTO_NOTES_04 §5, verbatim.
CURATED_TEXTS = [
    "This House would ban smartphones in schools up to Class 10.",
    "This House would replace board exams with continuous assessment.",
    "This House would make coding compulsory from Class 6.",
    "This House believes AI tutors will do Indian students more good than harm.",
    "This House believes tourism does more good than harm to India's hill towns.",
    "This House believes community policing beats building more police stations.",
    "This House would build metro lines before flyovers in growing cities.",
    "This House would ban single-use plastic completely, even if prices rise.",
    "This House believes work-from-home is better than office work for young professionals.",
    "This House would take a gap year before college.",
    "This House believes cricket gets too much attention compared with other sports in India.",
    "This House believes filter coffee beats masala chai.",
    "This House believes street food is India's greatest culinary treasure.",
]


@pytest.fixture(scope="module")
def motions():
    return load_curated_motions()


def test_thirteen_curated_motions_exactly_as_designed(motions):
    assert [m.text for m in motions.values()] == CURATED_TEXTS
    assert [m.number for m in motions.values()] == list(range(1, 14))
    assert all(len(m.text) <= MAX_MOTION_CHARS for m in motions.values())


def test_only_motions_5_and_6_have_cards(motions):
    with_cards = {m.number: m.card_topics for m in motions.values() if m.card_topics}
    assert with_cards == {5: ["tourism"], 6: ["internal_security"]}
    for m in motions.values():
        # Ruling 29: no argument links a card until the owner writes real cards and confirms
        # each one supports the argument it is attached to.
        ids = [c for a in [*m.case.for_, *m.case.against] for c in a.card_ids]
        assert ids == [], m.id


def test_case_file_cards_exist_and_match_the_motion_topic(motions):
    s = Settings(_env_file=None)
    cards = load_cards(s.cards_dir, s.topics)
    assert [p for m in motions.values() for p in case_card_problems(m, cards)] == []


def test_every_case_file_has_both_sides_and_no_figures(motions):
    for m in motions.values():
        for side in Side:
            args = m.case.side(side)
            assert 3 <= len(args) <= 4, (m.id, side)
            for a in args:
                assert not mentions_number(a.claim) and not mentions_number(a.warrant), a
        # The opponent argues the side the user did not pick.
        assert m.opponent_case(Side.for_) == m.case.against
        assert m.opponent_case(Side.against) == m.case.for_


def test_case_arguments_reject_any_figure():
    for text in (
        "Phones cut marks by 12 percent.",
        "Half of all students agree.",
        "Teen lakh bachche phone use karte hain.",
        "It costs crores.",
        "Since 2019 things changed.",
    ):
        with pytest.raises(ValueError):
            CaseArgument(id="F1", claim=text, warrant="Because it is so.", keywords=["x"])
    # Everyday "one/two/ek/do" are not statistics (deliberate narrowing, see numbers.py).
    assert not mentions_number("Ek baat samajhiye, do minute dijiye, one-to-one help.")


def test_find_motion_by_number_or_id(motions):
    assert find_motion(motions, "5").id == "tourism_hill_towns"
    assert find_motion(motions, "filter_coffee_vs_chai").number == 12
    with pytest.raises(KeyError):
        find_motion(motions, "14")


def test_normalise_motion():
    assert normalise_motion("  ban   homework on weekends ") == (
        "This House would ban homework on weekends."
    )
    assert normalise_motion("Online exams are a disaster") == (
        "This House believes online exams are a disaster."
    )
    assert normalise_motion("this house would X") == "This House would X."
    assert normalise_motion("") == ""


# ---------------------------------------------------------------------------
# Safety screen (fails closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("This House believes the BJP has failed.", "partisan"),
        ("This House would vote for the incumbent.", "partisan"),
        ("This House believes Has the current government failed farmers.", "partisan"),
        ("This House believes temples should be taxed.", "religion"),
        ("This House would scrap caste-based reservation.", "caste"),
        ("This House believes riots are inevitable.", "communal"),
        ("This House would teach how to make a bomb.", "violence"),
        ("This House believes the accused is guilty.", "court_case"),
    ],
)
def test_rules_refuse_blocked_topics(text, category):
    res = rule_screen(text)
    assert res is not None and res.decision == ScreenDecision.refuse
    assert res.category == category and res.layer == ScreenLayer.rules


def test_rules_refuse_overlong_and_pass_benign():
    assert rule_screen("This House would " + "x" * MAX_MOTION_CHARS).category == "too_long"
    assert rule_screen("This House believes mangoes are the king of fruits.") is None


async def test_feature_off_refuses_every_custom_motion():
    res = await screen_motion("This House believes mangoes are great.", llm=None, enabled=False)
    assert res.decision == ScreenDecision.refuse and res.layer == ScreenLayer.feature_off
    assert Settings(_env_file=None).feature_custom_motions is False


@pytest.mark.parametrize(
    "reply",
    [
        LLMThrottledError("throttled"),
        RuntimeError("boom"),
        {"decision": "maybe", "category": "none", "reason": "", "neutral_rewording": ""},
        # A reframe whose rewording is partisan, or empty, is refused, never offered.
        {"decision": "reframe", "category": "loaded", "reason": "r",
         "neutral_rewording": "This House believes the BJP is great."},
        {"decision": "reframe", "category": "loaded", "reason": "r", "neutral_rewording": ""},
    ],
)
async def test_classifier_failures_fail_closed(reply):
    llm = FakeLLMClient(script={"motion_screen": [reply, reply]})
    res = await screen_motion("This House believes mangoes are great.", llm=llm, enabled=True)
    assert res.decision == ScreenDecision.refuse
    assert res.layer == ScreenLayer.fail_closed
    assert res.usable_motion is None


async def test_reframe_needs_consent_and_allow_is_usable(cards):
    llm = make_fake_llm(cards)
    res = await screen_motion("Online exams are a total disaster", llm=llm, enabled=True)
    assert res.decision == ScreenDecision.reframe
    assert res.reframed == "This House believes online exams do more harm than good."
    assert res.usable_motion is None  # the user must accept the rewording first
    ok = await screen_motion("This House believes mangoes are great.", llm=llm, enabled=True)
    assert ok.usable_motion == "This House believes mangoes are great."


async def test_labelled_motion_set_lets_no_partisan_motion_through(cards):
    """The ~40-motion gate (CTO_NOTES_04 §5), on the rules + fake-classifier path. Decisions
    made by the classifier layer are marked as needing a real-Nova run."""
    items = load_items()
    assert len(items) >= 40
    results = await run_items(make_fake_llm(cards), items, real=False)
    summary = summarize(results)
    assert summary["partisan_items"] >= 15
    assert summary["partisan_passed"] == []
    assert summary["unsafe_allowed"] == []
    assert summary["gate_passed"]
    assert all(r.needs_real_nova for r in results if r.layer == "classifier")


# ---------------------------------------------------------------------------
# Custom case file
# ---------------------------------------------------------------------------


async def test_custom_case_file_from_the_model(cards):
    args, how = await build_custom_case(make_fake_llm(cards), "This House would X.", Side.against)
    assert how == "model" and [a.id for a in args] == ["A1", "A2", "A3"]


async def test_custom_case_drops_figures_and_falls_back_to_generic():
    bad = {
        "arguments": [
            {"claim": "Studies show 70 percent agree with this.", "warrant": "Surveys say so.",
             "keywords": ["survey"]},
            {"claim": "It is fair to everyone involved.", "warrant": "Nobody is left out by it.",
             "keywords": ["fair"]},
        ]
    }
    llm = FakeLLMClient(script={"case_file": [bad]})
    args, how = await build_custom_case(llm, "This House would X.", Side.for_)
    assert how.startswith("generic_fallback") and args == generic_case(Side.for_)
    failing = FakeLLMClient(script={"case_file": [RuntimeError("down")]})
    args, how = await build_custom_case(failing, "This House would X.", Side.for_)
    assert how == "generic_fallback: RuntimeError"
    assert all(not mentions_number(a.claim + a.warrant) for a in args)
