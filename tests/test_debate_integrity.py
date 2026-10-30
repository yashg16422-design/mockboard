"""Debate integrity (Ruling 29, CTO_NOTES_05 M1, M2, should-fix 6 and 7).

The two leaks the CTO reproduced, as tests that failed before the fix:

* M1: a synthetic test card authorised a figure in a real debate. For motion 5,
  "Tourism is 12 percent of India's GDP." passed the guard because 12 is the Exampleland
  test card's value.
* M2: the motion's own numbers were allowed anywhere ("Nearly 10 percent of students are
  addicted." passed for motion 1 because of "Class 10"), and a card's figure was allowed
  in any sentence, whatever it was about.
"""

from __future__ import annotations

import json

import pytest

from mockboard.brain.cards import load_cards
from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import BoardMove, Move
from mockboard.config import REPO_ROOT
from mockboard.modes.debate.motions import Side, load_curated_motions
from mockboard.modes.debate.session import DebateSetup
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

MOTIONS = load_curated_motions()
REBUT = BoardMove(move=Move.rebut, member="opponent", reason="D12: x", rule_id="D12")


class NotFakeLLM:
    """Stands in for the real Bedrock client: anything that is not the offline FakeLLMClient
    counts as a real run. Never called by these tests."""

    model_id = "amazon.nova-pro-v1:0"


def session_for(settings, llm, motion_id: str, *, cards=None, side=Side.against):
    cards = cards if cards is not None else load_cards(settings.cards_dir, settings.topics)
    deps = build_deps(settings, llm, cards=cards)
    setup = DebateSetup(motion=MOTIONS[motion_id], user_side=side)
    return InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(settings.trace_dir, "integ", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="integ", mode="debate", mode_options=setup,
    )


# ---------------------------------------------------------------------------
# M1: synthetic cards never authorise a figure outside --fake runs
# ---------------------------------------------------------------------------


def test_m1_synthetic_card_figure_is_dropped_in_a_real_debate(settings):
    s = session_for(settings, NotFakeLLM(), "tourism_hill_towns")
    assert all(c.synthetic_example for c in s.deps.cards.cards.values())  # Exampleland data
    assert not s.guard_sentence(REBUT, "Tourism is 12 percent of India's GDP.")
    assert not s.guard_sentence(REBUT, "Exampleland tourism share of GDP is 12 percent.")


def test_m1_real_debate_has_no_synthetic_cards_and_no_card_fact_checks(settings):
    s = session_for(settings, NotFakeLLM(), "tourism_hill_towns")
    assert s.hooks.cards == [] and not s.hooks.fact_check_enabled
    assert s.hooks.state.card_topics == []  # nothing to cite, so the prompt says so


def test_m1_fake_runs_keep_synthetic_cards_for_offline_tests(deps):
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(deps.settings.trace_dir, "f", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="f", mode="debate",
        mode_options=DebateSetup(motion=MOTIONS["tourism_hill_towns"], user_side=Side.against),
    )
    assert s.hooks.cards and s.hooks.fact_check_enabled


def test_m1_explicit_setting_overrides_the_llm_check(settings, fake_llm):
    deps = build_deps(settings, fake_llm, cards=load_cards(settings.cards_dir, settings.topics))
    setup = DebateSetup(motion=MOTIONS["tourism_hill_towns"], user_side=Side.against,
                        allow_synthetic_cards=False)
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(settings.trace_dir, "o", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="o", mode="debate", mode_options=setup,
    )
    assert s.hooks.cards == []


def test_m1_motions_5_and_6_carry_no_card_links():
    raw = json.loads((REPO_ROOT / "data" / "motions" / "curated.json").read_text())
    motions = raw["motions"] if isinstance(raw, dict) else raw
    for m in motions:
        for side in ("for", "against"):
            for arg in m["case"][side]:
                assert not arg.get("card_ids"), (m["id"], arg["id"])


# ---------------------------------------------------------------------------
# M2: figures only in context
# ---------------------------------------------------------------------------


def test_m2_motion_number_only_inside_the_motions_own_phrase(settings, fake_llm):
    s = session_for(settings, fake_llm, "smartphones_in_schools", side=Side.for_)
    assert not s.guard_sentence(REBUT, "Nearly 10 percent of students are addicted.")
    assert not s.guard_sentence(REBUT, "About 10 students in every class agree.")
    assert not s.guard_sentence(REBUT, "Class 10 percent results dropped.")  # next to a figure
    assert s.guard_sentence(REBUT, "Class 10 tak ke bachchon ko phone ki zaroorat nahi.")
    assert s.guard_sentence(REBUT, "Up to class 10, a ban is simple to enforce.")


@pytest.mark.parametrize(("sentence", "ok"), [
    ("Testland received 15 lakh foreign tourist arrivals in 2022.", True),
    ("Hill towns got 15 lakh visitors in 2022.", False),  # the card's figure, wrong subject
    ("Testland tourism share of GDP is 9 percent.", True),
    ("Only 9 percent of hill families earn from homestays.", False),
])
def test_m2_card_figure_only_in_a_sentence_about_that_card(deps, sentence, ok):
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(deps.settings.trace_dir, "c", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="c", mode="debate",
        mode_options=DebateSetup(motion=MOTIONS["tourism_hill_towns"], user_side=Side.against),
    )
    assert s.guard_sentence(REBUT, sentence) is ok
