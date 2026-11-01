"""The strategist rule table (strategist.py docstring), one test per row, plus safety rules."""

from __future__ import annotations

import pytest

from conftest import make_analysis, make_claim, make_ctx
from mockboard.brain.schemas import Balance, ClaimKind, Move, Verdict, VoiceSignals
from mockboard.brain.strategist import CLOSE_TARGET, StrategyConfig, decide_move, merge_analyses
from mockboard.modes.upsc import UPSC_MOVES

# The director's three moves are in `UPSC_MOVES`; the rule table can never emit them.
PANEL_MOVES = {Move.repeat_question, Move.rephrase_question, Move.next_question}
RULE_TABLE_MOVES = UPSC_MOVES - PANEL_MOVES

CFG = StrategyConfig(
    chairman="chairman",
    topic_members={"tourism": "economist", "internal_security": "ips_officer"},
    card_topics={"builtin_tourism_gdp_share": "tourism"},
)


def contradicted(turn_index: int = 0, **kw):
    return make_claim(
        verdict=Verdict.contradicted,
        card_id="builtin_tourism_gdp_share",
        turn_index=turn_index,
        model_verdict="contradicted",
        numeric_check="mismatch",
        **kw,
    )


def test_r1_garbled_asks_to_clarify():
    m = decide_move(make_ctx(), [make_analysis(garbled=True)], [], CFG)
    assert (m.move, m.member, m.rule_id) == (Move.clarify, "chairman", "R1")


def test_r1_beats_a_pending_fact_challenge():
    m = decide_move(make_ctx(), [make_analysis(garbled=True)], [contradicted()], CFG)
    assert m.move == Move.clarify


def test_r2_interrupt_only_when_flag_on():
    long = make_analysis(words=400)
    assert decide_move(make_ctx(), [long], [], CFG).move != Move.interrupt
    on = StrategyConfig(**{**CFG.__dict__, "board_interrupt_enabled": True})
    assert decide_move(make_ctx(), [long], [], on).move == Move.interrupt


@pytest.mark.parametrize(
    "analysis, signals, last",
    [
        (make_analysis(words=0, reasons=0), VoiceSignals(), None),
        (make_analysis(), VoiceSignals(response_latency_s=6.0), None),
        (make_analysis(honest_unknown=True), VoiceSignals(), Move.counter),
    ],
    ids=["silence", "long_pause", "honest_unknown_after_counter"],
)
def test_r3_reassure(analysis, signals, last):
    m = decide_move(make_ctx(voice_signals=signals, last_move=last), [analysis], [], CFG)
    assert (m.move, m.member, m.pressure) == (Move.reassure, "chairman", 0)


@pytest.mark.parametrize("last", [None, Move.probe, Move.reassure, Move.move_on, Move.counter])
def test_first_honest_unknown_in_topic_is_reassured_whatever_came_before(last):
    """Defect A: an honest 'I don't know' after None/probe/reassure/move_on used to get
    pressure. Now the first one in a topic is always reassured."""
    thin_unknown = make_analysis(words=8, reasons=0, balance=Balance.unclear, honest_unknown=True)
    m = decide_move(make_ctx(last_move=last), [thin_unknown], [], CFG)
    assert (m.move, m.rule_id, m.pressure) == (Move.reassure, "R3", 0)


def test_second_honest_unknown_in_topic_moves_on():
    ctx = make_ctx(honest_unknowns_in_topic=1, last_move=Move.reassure)
    m = decide_move(ctx, [make_analysis(honest_unknown=True)], [], CFG)
    assert (m.move, m.rule_id, m.target) == (Move.move_on, "R3b", "internal_security")


@pytest.mark.parametrize("unknowns_before", [0, 1])
def test_honest_unknown_with_the_topic_budget_used_moves_on(unknowns_before):
    """Ruling 12: on the topic's last question an honest unknown moves on (R3b), even the
    first one: reassuring and then leaving the topic at once reads oddly."""
    ctx = make_ctx(turns_in_topic=3, max_turns_in_topic=4, honest_unknowns_in_topic=unknowns_before)
    m = decide_move(ctx, [make_analysis(honest_unknown=True)], [], CFG)
    assert (m.move, m.rule_id, m.target) == (Move.move_on, "R3b", "internal_security")
    assert "budget" in m.reason
    one_left = make_ctx(turns_in_topic=2, max_turns_in_topic=4)
    assert decide_move(one_left, [make_analysis(honest_unknown=True)], [], CFG).move == (
        Move.reassure
    )


def test_honest_unknown_is_never_fact_challenged_in_that_turn():
    m = decide_move(make_ctx(turn_index=1), [make_analysis(honest_unknown=True)],
                    [contradicted(0)], CFG)
    assert m.move == Move.reassure


THIN = dict(words=10, reasons=1, balance=Balance.unclear)


@pytest.mark.parametrize(
    "ctx_kw, why",
    [
        ({"last_move": Move.reassure}, "after reassure"),
        ({"last_move": Move.pressure}, "twice in a row"),
        ({"pressure_in_topic": 2, "last_move": Move.probe}, "cap for this topic"),
    ],
    ids=["after_reassure", "twice_in_a_row", "topic_cap"],
)
def test_pressure_caps_fall_back_to_probe(ctx_kw, why):
    m = decide_move(make_ctx(**ctx_kw), [make_analysis(**THIN)], [], CFG)
    assert (m.move, m.rule_id) == (Move.probe, "R8") and why in m.reason


def test_pressure_allowed_again_after_a_different_move():
    m = decide_move(make_ctx(last_move=Move.probe, pressure_in_topic=1),
                    [make_analysis(**THIN)], [], CFG)
    assert m.move == Move.pressure


def test_consecutive_pressure_limit_is_configurable():
    two = StrategyConfig(**{**CFG.__dict__, "max_consecutive_pressure": 2})
    m = decide_move(make_ctx(last_move=Move.pressure, pressure_in_topic=1),
                    [make_analysis(**THIN)], [], two)
    assert m.move == Move.pressure


def test_a_fallback_analysis_is_never_pressed_but_old_verdicts_still_count():
    """When the listener failed or was abandoned, "thin" is only a guess: probe instead.
    A gated contradiction from an earlier turn is real, so it can still be challenged."""
    guess = make_analysis(**THIN, is_fallback=True)
    m = decide_move(make_ctx(), [guess], [], CFG)
    assert (m.move, m.rule_id) == (Move.probe, "R8") and "listener fallback" in m.reason
    m = decide_move(make_ctx(turn_index=1), [guess], [contradicted(0)], CFG)
    assert m.move == Move.fact_challenge


def test_concession_is_acknowledged_and_never_pressed():
    conceded = make_analysis(**THIN, concedes_error=True)
    m = decide_move(make_ctx(last_move=Move.fact_challenge), [conceded], [], CFG)
    assert m.move == Move.probe and m.preface == "acknowledge"
    assert "acknowledge" in m.reason


def test_concession_preface_rides_on_any_move_without_adding_a_tenth():
    conceded = make_analysis(concedes_error=True, balance=Balance.one_sided)
    m = decide_move(make_ctx(), [conceded], [], CFG)
    assert m.move == Move.counter and m.preface == "acknowledge"
    assert len(RULE_TABLE_MOVES) == 9  # the rule table's moves stay nine (the panel adds its own)


def test_no_preface_without_a_concession_or_on_clarify():
    assert decide_move(make_ctx(), [make_analysis()], [], CFG).preface is None
    garbled = make_analysis(garbled=True, concedes_error=True)
    assert decide_move(make_ctx(), [garbled], [], CFG).preface is None


def test_r4_delayed_fact_challenge_targets_claim_and_card_owner():
    m = decide_move(make_ctx(turn_index=1), [make_analysis()], [contradicted(0)], CFG)
    assert (m.move, m.target, m.member, m.rule_id) == (
        Move.fact_challenge,
        "t0.s0.c0",
        "economist",
        "R4",
    )
    assert m.for_turn == "t1"


def test_r4_waits_for_the_minimum_delay():
    m = decide_move(make_ctx(turn_index=0), [make_analysis("t0")], [contradicted(0)], CFG)
    assert m.move != Move.fact_challenge


def test_r4_never_challenges_twice():
    ctx = make_ctx(turn_index=2, challenged_claim_ids=["t0.s0.c0"])
    assert (
        decide_move(ctx, [make_analysis("t2")], [contradicted(0)], CFG).move != Move.fact_challenge
    )


@pytest.mark.parametrize(
    "claim",
    [
        make_claim(verdict=Verdict.possible_conflict, card_id="builtin_tourism_gdp_share"),
        make_claim(verdict=Verdict.unverifiable, card_id="builtin_tourism_gdp_share"),
        make_claim(verdict=Verdict.supported, card_id="builtin_tourism_gdp_share"),
        make_claim(verdict=Verdict.contradicted, card_id=None),
        make_claim(
            verdict=Verdict.contradicted,
            card_id="builtin_tourism_gdp_share",
            kind=ClaimKind.qualitative,
            value=None,
            unit=None,
        ),
    ],
    ids=["possible_conflict", "unverifiable", "supported", "no_card", "non_numeric"],
)
def test_r4_only_numeric_or_date_contradictions_with_a_card(claim):
    m = decide_move(make_ctx(turn_index=1), [make_analysis()], [claim], CFG)
    assert m.move != Move.fact_challenge


def test_r4_date_claims_can_be_challenged():
    claim = make_claim(
        kind=ClaimKind.date,
        value=2015,
        unit="year",
        year=2015,
        verdict=Verdict.contradicted,
        card_id="builtin_tourism_gdp_share",
    )
    assert decide_move(make_ctx(), [make_analysis()], [claim], CFG).move == Move.fact_challenge


def test_r5_move_on_when_topic_budget_used():
    m = decide_move(make_ctx(turns_in_topic=3, max_turns_in_topic=4), [make_analysis()], [], CFG)
    assert (m.move, m.target, m.member) == (Move.move_on, "internal_security", "ips_officer")


def test_r5_move_on_after_strong_streak():
    m = decide_move(make_ctx(strong_streak=2), [make_analysis()], [], CFG)
    assert m.move == Move.move_on and "strong" in m.reason


def test_r5_close_when_no_topics_left():
    ctx = make_ctx(turns_in_topic=3, max_turns_in_topic=4, next_topic=None)
    m = decide_move(ctx, [make_analysis()], [], CFG)
    assert (m.move, m.target, m.member) == (Move.move_on, CLOSE_TARGET, "chairman")


def test_r6_counter_then_steelman():
    one_sided = make_analysis(balance=Balance.one_sided)
    assert decide_move(make_ctx(), [one_sided], [], CFG).move == Move.counter
    m = decide_move(make_ctx(last_move=Move.counter), [one_sided], [], CFG)
    assert (m.move, m.member) == (Move.steelman, "economist")


def test_r7_thin_answer_gets_pressure():
    m = decide_move(make_ctx(), [make_analysis(**THIN)], [], CFG)
    assert (m.move, m.pressure) == (Move.pressure, 3)


def test_r8_default_probe():
    m = decide_move(
        make_ctx(), [make_analysis(words=60, reasons=1, balance=Balance.unclear)], [], CFG
    )
    assert (m.move, m.member, m.rule_id) == (Move.probe, "economist", "R8")


def test_only_current_turn_analyses_count():
    stale = make_analysis("t0", garbled=True)
    m = decide_move(make_ctx(turn_index=1), [stale, make_analysis("t1")], [], CFG)
    assert m.move != Move.clarify


def test_merge_analyses_balance_and_garbled():
    merged = merge_analyses(
        [
            make_analysis(balance=Balance.one_sided, words=10),
            make_analysis(balance=Balance.balanced, words=10, garbled=True),
        ]
    )
    assert merged.balance == Balance.balanced
    assert merged.word_count == 20
    assert not merged.garbled  # exactly half the words: not "most"


def test_every_move_has_a_rule():
    reachable = {
        Move.clarify,
        Move.interrupt,
        Move.reassure,
        Move.fact_challenge,
        Move.move_on,
        Move.counter,
        Move.steelman,
        Move.pressure,
        Move.probe,
    }
    assert reachable == RULE_TABLE_MOVES
