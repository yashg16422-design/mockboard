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


# ---------------------------------------------------------------------------
# Off-limits backstop (ruling 29, CTO should-fix 6)
# ---------------------------------------------------------------------------


def test_opponent_party_religion_caste_sentences_are_dropped(deps):
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(deps.settings.trace_dir, "x", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="x", mode="debate",
        mode_options=DebateSetup(motion=MOTIONS["street_food"], user_side=Side.for_),
    )
    for bad in ("Yeh bilkul BJP ki soch jaisa hai.", "Har religion ka khana alag hai.",
                "Caste ke hisaab se khana badalta hai.", "Isse communal tension badhti hai."):
        assert not s.guard_sentence(REBUT, bad), bad
    assert s.guard_sentence(REBUT, "Street food sasta aur taaza hota hai.")
    redirect = BoardMove(move=Move.redirect, member="moderator", reason="D2: x", rule_id="D2")
    # The Moderator's own redirect names parties to keep them out; it is not an opponent line.
    assert s.guard_sentence(redirect, "Chaliye, parties aur netaon ko alag rakhte hain.")


def test_opponent_line_loses_only_its_off_limits_sentence():
    from mockboard.modes.debate.actor import apply_guards
    from test_debate_actor import brief_for

    line = "Yeh Congress jaisi baat hai. Lekin street food mein hygiene ek asli sawaal hai."
    out = apply_guards(line, REBUT, brief_for("street_food"), last_answer="", max_words=45)
    assert out.text == "Lekin street food mein hygiene ek asli sawaal hai."
    assert "off_limits_guard" in out.guard and out.dropped_sentences


async def test_an_opponent_that_only_says_off_limits_things_falls_back_to_the_template(
    settings, cards
):
    from conftest import answer
    from mockboard.brain.fake_responders import fake_responders
    from mockboard.brain.llm import FakeLLMClient
    from test_debate_session import CLASH_PLAIN, CLOSING, OPENING

    responders = fake_responders(cards)
    responders["actor"] = lambda system, user: "Yeh sab BJP aur Congress ka khel hai."
    deps = build_deps(settings, FakeLLMClient(responders=responders), cards=cards)
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(settings.trace_dir, "y", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="y", mode="debate",
        mode_options=DebateSetup(motion=MOTIONS["smartphones_in_schools"], user_side=Side.for_),
    )
    await s.open()
    for text in (OPENING, CLASH_PLAIN, CLASH_PLAIN, CLOSING):
        if not s.state.finished:
            out = await answer(s, text)
            if out.move.member == "opponent":
                assert "BJP" not in out.line.text and "Congress" not in out.line.text
                assert out.line.text.strip()  # a template, never silence


def test_user_off_limits_gets_redirect_even_when_the_listener_misses_it(deps):
    from mockboard.brain.schemas import Analysis, DebateArgument
    from mockboard.modes.debate.strategist import decide_debate_move
    from test_debate_strategist import dctx

    missed = Analysis(turn_id="t2", word_count=12, off_limits=False,
                      arguments=[DebateArgument(claim="Yeh sab Congress ki galti hai",
                                                warrant="kyunki unhone vaada kiya")])
    ctx = dctx()
    move = decide_debate_move(ctx, [missed], [], deps.mode("debate").strategy)
    assert (move.move, move.rule_id) == (Move.redirect, "D2")
    assert "rule backstop: partisan" in move.reason
    fine = missed.model_copy(update={"arguments": [DebateArgument(
        claim="Mandir ke bahar ki chaat sabse achhi hoti hai", warrant="kyunki taaza hai")]})
    assert decide_debate_move(ctx, [fine], [], deps.mode("debate").strategy).move != Move.redirect


# ---------------------------------------------------------------------------
# The closing concession names a real argument the user made (ruling 29, should-fix 7)
# ---------------------------------------------------------------------------


def _brief(**kw):
    from dataclasses import replace

    from test_debate_actor import brief_for

    m = MOTIONS["smartphones_in_schools"]
    kw.setdefault("user_case", tuple(m.opponent_case(Side.against)))  # the FOR side's case
    return replace(brief_for("smartphones_in_schools"), **kw)


def test_concession_names_the_users_own_side_argument():
    from mockboard.modes.debate.actor import concession_text, user_made_arguments

    b = _brief(user_speech=("Cyberbullying aur social media ka harm school tak aata hai.",))
    assert user_made_arguments(b)[0].startswith("A ban protects younger students")
    assert concession_text(b).startswith("aapke is tark mein dum hai: a ban protects")


def test_concession_falls_back_to_the_users_own_words_then_to_the_side():
    from mockboard.modes.debate.actor import concession_text

    own = _brief(user_speech=("Bachchon ko khelne ka samay chahiye.",),
                 user_arguments=("Bachchon ko khelne ka zyada samay milna chahiye",),
                 user_case=())
    assert "khelne ka zyada samay" in concession_text(own)
    figure = _brief(user_speech=("x",), user_arguments=("40 percent bachche addicted hain sach",),
                    user_case=())
    assert "40" not in concession_text(figure)  # the user's uncarded figure is never repeated
    side = _brief(user_speech=("Kuch nahi.",))
    assert concession_text(side).startswith("aapke paksh ke is tark mein dum hai")


def test_no_concession_praises_the_user():
    from mockboard.modes.debate.actor import _T

    for mix in _T.values():
        for key, text in mix.items():
            if key.startswith("concession"):
                for praise in ("strong", "mazboot", "very good", "achhi", "great"):
                    assert praise not in text.lower(), (key, text)


async def test_fake_debate_closing_names_a_user_argument(deps, tmp_path):
    from conftest import answer
    from test_debate_session import CLASH_PLAIN, CLASH_STRONG, CLOSING, OPENING, make_session

    s = make_session(deps, tmp_path)
    await s.open()
    outs = [await answer(s, t) for t in (OPENING, CLASH_STRONG, CLASH_PLAIN, CLOSING)]
    closing = outs[-1].line.text
    assert closing.startswith("Maanna padega ki aapke is tark mein dum hai: phones in class "
                              "pull attention away from learning.")


# ---------------------------------------------------------------------------
# Ruling 32: the user-side check also runs on the raw transcript
# ---------------------------------------------------------------------------

PARTY_SPEECH = "Yeh sab BJP ki galti hai, kyunki unhone har school mein yahi vaada kiya tha."


def _paraphrasing_listener(base):
    """A listener that paraphrases the party name away and never flags off-limits, as a
    real model might."""
    def listener(system: str, user: str) -> dict:
        out = base(system, user)
        text = json.dumps(out, ensure_ascii=False).replace("BJP", "a certain group")
        out = json.loads(text)
        if "off_limits" in out:
            out["off_limits"] = False
        return out
    return listener


async def test_paraphrased_off_limits_speech_still_gets_the_redirect(settings, cards):
    from conftest import answer
    from mockboard.brain.fake_responders import fake_responders
    from mockboard.brain.llm import FakeLLMClient
    from test_debate_session import OPENING

    responders = fake_responders(cards)
    responders["listener"] = _paraphrasing_listener(responders["listener"])
    llm = FakeLLMClient(responders=responders)
    deps = build_deps(settings, llm, cards=cards)
    s = InterviewSession(
        deps=deps, form=None, recorder=TraceRecorder(settings.trace_dir, "r32", enabled=False),
        analyzer=TurnAnalyzer(deps), session_id="r32", mode="debate",
        mode_options=DebateSetup(motion=MOTIONS["smartphones_in_schools"], user_side=Side.for_),
    )
    await s.open()
    await answer(s, OPENING)
    out = await answer(s, PARTY_SPEECH)
    listener_call = llm.calls_named("listener")[-1]
    assert "BJP" in listener_call.user  # the listener heard it ...
    a = s.state.turns[-1].analysis
    texts = " ".join([a.stance, *a.reasons, *(x.claim for x in a.arguments)])
    assert "BJP" not in texts  # ... and paraphrased it away
    assert (out.move.move, out.move.rule_id, out.move.member) == (Move.redirect, "D2",
                                                                  "moderator")


def test_raw_text_hook_is_debate_only():
    from mockboard.brain.listener import to_analysis
    from mockboard.brain.schemas import Segment
    from mockboard.brain.tools import ListenerOutput
    from mockboard.modes.debate.listener import ScreenedDebateListenerOutput

    seg = Segment(segment_id="s0", segment_index=0, text=PARTY_SPEECH)
    upsc = to_analysis(ListenerOutput(), turn_id="t1", turn_index=1, segment=seg)
    assert upsc.off_limits is False  # UPSC: no hook, unchanged
    debate = to_analysis(ScreenedDebateListenerOutput(off_limits=False), turn_id="t1",
                         turn_index=1, segment=seg)
    assert debate.off_limits is True
    clean = Segment(segment_id="s0", segment_index=0, text="Mandir ke bahar chaat milti hai.")
    assert to_analysis(ScreenedDebateListenerOutput(), turn_id="t1", turn_index=1,
                       segment=clean).off_limits is False
