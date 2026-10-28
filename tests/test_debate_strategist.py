"""Debate rule table (DB3): one test per row D1-D12, the anti-whiplash rules, the fallback and
cap hooks, and a per-mode allowed-moves check for both rule tables."""

from __future__ import annotations

import random

import pytest

from conftest import make_analysis, make_claim
from mockboard.brain.schemas import (
    Balance,
    DebateArgument,
    DebateContext,
    Move,
    OpponentArg,
    PointStrength,
    StrategyContext,
    Verdict,
    VoiceSignals,
)
from mockboard.brain.strategist import StrategyConfig, decide_move
from mockboard.modes.debate.strategist import (
    DEBATE_MOVES,
    OPPONENT,
    WARRANT_TARGET,
    debate_cap_move,
    debate_fallback_move,
    decide_debate_move,
    opening_args,
    pick_argument,
)
from mockboard.modes.upsc import UPSC_MOVES

CFG = StrategyConfig(
    chairman="moderator",
    card_topics={"test_tourism_gdp_share": "tourism", "test_security_budget": "internal_security"},
)
ARGS = [
    OpponentArg(id="A1", claim="Phones are learning tools.", keywords=["learning", "apps"]),
    OpponentArg(id="A2", claim="Parents need contact.", keywords=["parents", "safety"]),
    OpponentArg(id="A3", claim="Teach responsible use.", keywords=["responsible"]),
]
WARRANTED = [DebateArgument(claim="Phones distract", warrant="because notifications pull focus")]


def dctx(rnd: str = "clash", *, turns_in_topic: int = 0, max_turns: int = 3, last_move=None,
         card_topics=(), signals=None, **debate) -> StrategyContext:
    nxt = {"opening": "clash", "clash": "closing", "closing": None}[rnd]
    return StrategyContext(
        mode="debate",
        turn_id="t2",
        turn_index=2,
        topic=rnd,
        next_topic=nxt,
        turns_in_topic=turns_in_topic,
        max_turns_in_topic=max_turns,
        last_move=last_move,
        voice_signals=signals or VoiceSignals(),
        debate=DebateContext(
            motion="This House would ban smartphones in schools up to Class 10.",
            user_side="for",
            opponent_args=ARGS,
            card_topics=list(card_topics),
            **debate,
        ),
    )


def speech(turn_id: str = "t2", **kw):
    kw.setdefault("arguments", WARRANTED)
    kw.setdefault("balance", Balance.one_sided)
    return make_analysis(turn_id, **kw)


def decide(ctx, analysis=None, ledger=()):
    return decide_debate_move(ctx, [analysis or speech()], list(ledger), CFG)


def test_d1_garbled_gets_clarify_from_the_moderator():
    m = decide(dctx(), speech(garbled=True))
    assert (m.move, m.member, m.rule_id) == (Move.clarify, "moderator", "D1")


def test_d1b_interrupt_only_when_the_flag_is_on():
    long = speech(words=400)
    assert decide(dctx(), long).move != Move.interrupt
    on = StrategyConfig(**{**CFG.__dict__, "board_interrupt_enabled": True})
    m = decide_debate_move(dctx(), [long], [], on)
    assert (m.move, m.rule_id) == (Move.interrupt, "D1b")


def test_d2_off_limits_gets_redirect_from_the_moderator():
    m = decide(dctx(), speech(off_limits=True, question_to_opponent="Why?"))
    assert (m.move, m.member, m.rule_id) == (Move.redirect, "moderator", "D2")
    assert m.preface is None


def test_d3_silence_and_long_pause_get_reassure():
    m = decide_debate_move(dctx(), [], [], CFG)
    assert (m.move, m.rule_id, m.member) == (Move.reassure, "D3", "moderator")
    slow = dctx(signals=VoiceSignals(response_latency_s=6.0))
    assert decide(slow).rule_id == "D3"


@pytest.mark.parametrize(
    ("rnd", "turns", "expected"),
    [
        ("opening", 0, Move.opening_statement),
        ("closing", 0, Move.closing_statement),
        ("clash", 0, Move.rebut),
        ("clash", 2, Move.move_on),  # budget used: move on, never pounce
    ],
)
def test_d4_honest_unknown_gets_grace_and_is_never_pounced_on(rnd, turns, expected):
    ledger = [make_claim("t0.s0.c0", verdict=Verdict.contradicted,
                         card_id="test_tourism_gdp_share")]
    ctx = dctx(rnd, turns_in_topic=turns, card_topics=["tourism"], last_opponent_arg="A1")
    m = decide(ctx, speech(honest_unknown=True, arguments=[DebateArgument(claim="x")]), ledger)
    assert (m.move, m.rule_id, m.preface) == (expected, "D4", "acknowledge")
    assert m.move not in (Move.cross_question, Move.fact_challenge)


def test_d5_opening_and_closing_statements():
    m = decide(dctx("opening"))
    assert (m.move, m.member, m.rule_id, m.target) == (
        Move.opening_statement, OPPONENT, "D5", "A1"
    )
    m = decide(dctx("closing", pressed_counts={"A1": 2}))
    assert (m.move, m.rule_id, m.target) == (Move.closing_statement, "D5", "A2")


def test_d6_clash_budget_used_moves_on_to_closing():
    m = decide(dctx(turns_in_topic=2, max_turns=3))
    assert (m.move, m.member, m.rule_id, m.target) == (
        Move.move_on, "moderator", "D6", "closing"
    )


def test_d7_a_question_to_the_opponent_is_answered_first():
    m = decide(dctx(), speech(question_to_opponent="Parents kaise contact karenge?"))
    assert (m.move, m.member, m.rule_id) == (Move.rebut, OPPONENT, "D7")


def test_d8_strong_point_on_a_standing_argument_wins_a_concession():
    strong = speech(point_strength=PointStrength.strong, rebuts=["A2"])
    m = decide(dctx(), strong)
    assert (m.move, m.rule_id, m.target) == (Move.concede_point, "D8", "A2")
    # Already conceded: no second concession on the same point.
    m = decide(dctx(conceded_ids=["A2"]), strong)
    assert m.move != Move.concede_point
    # Adequate is not enough.
    m = decide(dctx(), speech(point_strength=PointStrength.adequate, rebuts=["A2"]))
    assert m.move != Move.concede_point


def test_d9_fact_challenge_only_on_the_motions_own_cards():
    tourism = make_claim("t0.s0.c0", verdict=Verdict.contradicted,
                         card_id="test_tourism_gdp_share")
    m = decide(dctx(card_topics=["tourism"]), speech(rebuts=["A1"]), [tourism])
    assert (m.move, m.rule_id, m.target) == (Move.fact_challenge, "D9", "t0.s0.c0")
    # A motion without cards (or with other cards) never challenges a figure.
    assert decide(dctx(), speech(rebuts=["A1"]), [tourism]).move != Move.fact_challenge
    other = dctx(card_topics=["internal_security"])
    assert decide(other, speech(rebuts=["A1"]), [tourism]).move != Move.fact_challenge
    # Same-turn, fallback-free gates are R4's: a claim from this very turn is not challenged.
    fresh = make_claim("t2.s0.c0", verdict=Verdict.contradicted, turn_index=2,
                       card_id="test_tourism_gdp_share")
    assert decide(dctx(card_topics=["tourism"]), speech(rebuts=["A1"]), [fresh]).rule_id != "D9"


def test_d10_ignored_last_point_gets_one_cross_question():
    ctx = dctx(last_opponent_arg="A2")
    m = decide(ctx, speech(rebuts=["A1"]))
    assert (m.move, m.rule_id, m.target) == (Move.cross_question, "D10", "A2")
    # Answered: no cross-question.
    assert decide(ctx, speech(rebuts=["A2"])).rule_id != "D10"
    # Never twice on the same point.
    again = dctx(last_opponent_arg="A2", cross_questioned_ids=["A2"])
    assert decide(again, speech(rebuts=["A1"])).rule_id != "D10"


def test_d11_claims_without_a_warrant_get_why_does_that_follow():
    bare = speech(arguments=[DebateArgument(claim="Phones are bad"), DebateArgument(claim="x")])
    m = decide(dctx(), bare)
    assert (m.move, m.rule_id, m.target) == (Move.cross_question, "D11", WARRANT_TARGET)


def test_d12_rebut_with_the_strongest_least_pressed_standing_argument():
    m = decide(dctx())
    assert (m.move, m.rule_id, m.target) == (Move.rebut, "D12", "A1")
    m = decide(dctx(pressed_counts={"A1": 1}))
    assert m.target == "A2"
    m = decide(dctx(conceded_ids=["A1", "A2"]))
    assert m.target == "A3"


def test_d12_steelman_once_when_no_fresh_argument_is_left():
    worn = {"A1": 2, "A2": 2, "A3": 2}
    m = decide(dctx(pressed_counts=worn))
    assert (m.move, m.rule_id) == (Move.steelman, "D12")
    m = decide(dctx(pressed_counts=worn, steelman_used=True))
    assert m.move == Move.rebut


def test_no_two_cross_questions_in_a_row():
    ctx = dctx(last_opponent_arg="A2", last_move=Move.cross_question)
    m = decide(ctx, speech(rebuts=["A1"], arguments=[DebateArgument(claim="bare")]))
    assert m.move != Move.cross_question


def test_no_cross_question_straight_after_the_user_concedes():
    for kw in ({"concedes_point": True}, {"concedes_error": True}):
        m = decide(dctx(last_opponent_arg="A2"), speech(rebuts=["A1"], **kw))
        assert m.move != Move.cross_question
        assert m.preface == "acknowledge"


def test_debate_needs_its_context():
    ctx = dctx().model_copy(update={"debate": None})
    with pytest.raises(ValueError):
        decide(ctx)


def test_fallback_and_cap_hooks():
    silent = debate_fallback_move(dctx(), False, "deadline", CFG)
    assert (silent.move, silent.rule_id) == (Move.reassure, "FALLBACK")
    assert debate_fallback_move(dctx("opening"), True, "x", CFG).move == Move.opening_statement
    assert debate_fallback_move(dctx("closing"), True, "x", CFG).move == Move.closing_statement
    clash = debate_fallback_move(dctx(), True, "x", CFG)
    assert (clash.move, clash.reason) == (Move.rebut, "FALLBACK: x")
    rebut = decide(dctx())
    assert debate_cap_move(dctx("closing"), rebut, 9, CFG).move == Move.closing_statement
    capped = debate_cap_move(dctx(), rebut, 9, CFG)
    assert (capped.move, capped.target, capped.rule_id) == (Move.move_on, "close", "CAP")


def test_helpers_pick_and_opening_args():
    d = dctx().debate
    assert [a.id for a in opening_args(d)] == ["A1", "A2"]
    assert pick_argument(d, exclude=["A1"]).id == "A2"
    assert pick_argument(d.model_copy(update={"conceded_ids": ["A1", "A2", "A3"]})) is None


# ---------------------------------------------------------------------------
# Allowed moves, per mode
# ---------------------------------------------------------------------------


def test_the_mode_move_sets():
    assert len(UPSC_MOVES) == 9
    assert {Move.probe, Move.counter, Move.pressure}.isdisjoint(DEBATE_MOVES)
    assert {
        Move.opening_statement, Move.closing_statement, Move.rebut, Move.cross_question,
        Move.concede_point, Move.redirect,
    } <= DEBATE_MOVES
    assert {
        Move.opening_statement, Move.closing_statement, Move.rebut, Move.cross_question,
        Move.concede_point, Move.redirect,
    }.isdisjoint(UPSC_MOVES)


def _random_analysis(rng: random.Random, turn_id: str):
    return make_analysis(
        turn_id,
        words=rng.choice([0, 5, 30, 200]),
        reasons=rng.randint(0, 3),
        balance=rng.choice(list(Balance)),
        garbled=rng.random() < 0.1,
        honest_unknown=rng.random() < 0.15,
        concedes_error=rng.random() < 0.1,
        concedes_point=rng.random() < 0.15,
        off_limits=rng.random() < 0.1,
        question_to_opponent=rng.choice(["", "", "Why?"]),
        point_strength=rng.choice([None, *PointStrength]),
        rebuts=rng.sample(["A1", "A2", "A3"], rng.randint(0, 2)),
        arguments=rng.choice([[], WARRANTED, [DebateArgument(claim="bare")]]),
    )


@pytest.mark.parametrize("seed", range(5))
def test_each_rule_table_emits_only_its_own_moves(seed):
    rng = random.Random(seed)
    ledger = [make_claim("t0.s0.c0", verdict=Verdict.contradicted,
                         card_id="test_tourism_gdp_share")]
    seen_debate: set[Move] = set()
    for _ in range(300):
        rnd = rng.choice(["opening", "clash", "closing"])
        ctx = dctx(
            rnd,
            turns_in_topic=rng.randint(0, 3),
            last_move=rng.choice([None, *DEBATE_MOVES]),
            card_topics=rng.choice([(), ("tourism",)]),
            last_opponent_arg=rng.choice([None, "A1", "A2"]),
            conceded_ids=rng.sample(["A1", "A2", "A3"], rng.randint(0, 3)),
            pressed_counts={"A1": rng.randint(0, 3), "A2": rng.randint(0, 3)},
            steelman_used=rng.random() < 0.5,
        )
        a = _random_analysis(rng, "t2")
        m = decide_debate_move(ctx, [a], ledger, CFG)
        assert m.move in DEBATE_MOVES, m
        seen_debate.add(m.move)
        upsc_ctx = StrategyContext(turn_id="t2", turn_index=2, topic="tourism",
                                   next_topic="internal_security",
                                   turns_in_topic=rng.randint(0, 3),
                                   last_move=rng.choice([None, *UPSC_MOVES]))
        assert decide_move(upsc_ctx, [a], ledger, CFG).move in UPSC_MOVES
    if seed == 0:  # the fuzz really exercises the table
        assert len(seen_debate) >= 9, seen_debate
