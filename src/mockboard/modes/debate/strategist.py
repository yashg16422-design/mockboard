"""Debate strategist: the board's next move in debate mode, by PURE deterministic rules.

Same contract as the UPSC strategist (`brain/strategist.py`): no LLM, no I/O, runs after
every analysed segment, first matching rule wins, every move carries its rule id. This table
is the specification (CTO_NOTES_04 §2); tests/test_debate_strategist.py checks every row.

The rounds are the session's "topics": opening, clash, closing (`ctx.topic`). The Moderator
is the profile's chairman persona; the Opponent is the generic member id `opponent`, which the
session maps to the chosen opponent persona (logician or wit).

| Rule | Condition (this turn's segments so far)                 | Move                | Speaker   |
|------|---------------------------------------------------------|---------------------|-----------|
| D1   | garbled transcript                                      | clarify             | Moderator |
| D1b  | FEATURE_BOARD_INTERRUPT on, speech over the word limit  | interrupt           | Moderator |
| D2   | `off_limits` (parties, leaders, elections, groups)      | redirect            | Moderator |
| D3   | silence, or a long pause before speaking                | reassure            | Moderator |
| D4   | honest "I don't know": preface "acknowledge", then the  | statement / rebut / | Opponent  |
|      | round's statement, a rebut, or move_on if the clash     | move_on             | (Moderator|
|      | budget is used; never a cross-question or challenge     |                     | for move) |
| D5   | the user has just opened / closed                       | opening_statement / | Opponent  |
|      |                                                         | closing_statement   |           |
| D6   | clash round budget used                                 | move_on (closing)   | Moderator |
| D7   | `question_to_opponent` present                          | rebut, answer first | Opponent  |
| D8   | a strong point rebuts a STANDING case argument          | concede_point, pivot| Opponent  |
| D9   | delayed contradiction (R4 gates), motion's own cards    | fact_challenge      | Opponent  |
| D10  | the user ignored the opponent's last point (once each)  | cross_question      | Opponent  |
| D11  | all of the user's arguments lack a warrant              | cross_question (why)| Opponent  |
| D12  | otherwise: strongest least-pressed standing argument;   | rebut / steelman    | Opponent  |
|      | steelman once per debate when nothing fresh is left     |                     |           |

Anti-whiplash (carried over from the UPSC rules): never two cross-questions in a row, and
never a cross-question straight after the user concedes (a point or an error). When the user
concedes, the move carries `preface="acknowledge"`, as in UPSC mode. Debate never emits
`probe`, `counter` or `pressure`.
"""

from __future__ import annotations

from collections.abc import Sequence

from mockboard.brain.schemas import (
    Analysis,
    BoardMove,
    Claim,
    DebateContext,
    Move,
    OpponentArg,
    PointStrength,
    StrategyContext,
)
from mockboard.brain.strategist import (
    PRESSURE,
    StrategyConfig,
    challengeable_claims,
    merge_analyses,
)

OPPONENT = "opponent"  # generic member id; the session maps it to the chosen persona
OPENING, CLASH, CLOSING = "opening", "clash", "closing"
ROUNDS: tuple[str, ...] = (OPENING, CLASH, CLOSING)
CLOSE_TARGET = "close"
WARRANT_TARGET = "warrant"  # cross_question target for D11 ("why does that follow?")

DEBATE_MOVES: frozenset[Move] = frozenset(
    {
        Move.clarify,
        Move.interrupt,
        Move.redirect,
        Move.reassure,
        Move.opening_statement,
        Move.closing_statement,
        Move.move_on,
        Move.rebut,
        Move.concede_point,
        Move.fact_challenge,
        Move.cross_question,
        Move.steelman,
    }
)


def _move(move: Move, member: str, rule: str, why: str, target: str | None = None) -> BoardMove:
    return BoardMove(
        move=move,
        target=target,
        member=member,
        pressure=PRESSURE[move],
        reason=f"{rule}: {why}",
        rule_id=rule,
    )


# ---------------------------------------------------------------------------
# Choosing the opponent's argument (pure helpers, shared with the session and the actor)
# ---------------------------------------------------------------------------


def pick_argument(d: DebateContext, exclude: Sequence[str] = ()) -> OpponentArg | None:
    """The strongest standing argument that has been pressed least: case order breaks ties,
    so the opponent leads with its best point but does not repeat it every turn."""
    standing = [a for a in d.standing if a.id not in exclude]
    if not standing:
        return None
    order = {a.id: i for i, a in enumerate(d.opponent_args)}
    return min(standing, key=lambda a: (d.pressed_counts.get(a.id, 0), order[a.id]))


def opening_args(d: DebateContext) -> list[OpponentArg]:
    """The two arguments the opening statement makes (strongest standing first)."""
    first = pick_argument(d)
    if first is None:
        return []
    second = pick_argument(d, exclude=[first.id])
    return [first] if second is None else [first, second]


def fresh_argument_left(d: DebateContext) -> bool:
    """False once every standing argument has been pressed at least twice."""
    return any(d.pressed_counts.get(a.id, 0) < 2 for a in d.standing)


def _budget_used(ctx: StrategyContext) -> bool:
    return ctx.turns_in_topic + 1 >= max(1, ctx.max_turns_in_topic)


def _next_round(ctx: StrategyContext) -> str:
    return ctx.next_topic or CLOSE_TARGET


# ---------------------------------------------------------------------------
# The rule table
# ---------------------------------------------------------------------------


def decide_debate_move(
    ctx: StrategyContext,
    analyses: Sequence[Analysis],
    ledger: Sequence[Claim],
    cfg: StrategyConfig,
) -> BoardMove:
    """Apply the debate rule table. `analyses` are this turn's segments so far."""
    if ctx.debate is None:
        raise ValueError("debate mode needs ctx.debate (the motion and the case file)")
    turn = merge_analyses([a for a in analyses if a.turn_id == ctx.turn_id])
    move = _decide(ctx, ctx.debate, turn, ledger, cfg)
    update: dict = {"for_turn": ctx.turn_id}
    conceded = turn.concedes_error or turn.concedes_point
    # The Opponent acknowledges a concession; the Moderator does not take sides, so its
    # moves carry no acknowledgement (D4's grace for an honest "I don't know" is separate).
    if conceded and move.member == OPPONENT and move.preface is None:
        update["preface"] = "acknowledge"
        update["reason"] = move.reason + " (+ acknowledge the user's concession first)"
    return move.model_copy(update=update)


def _decide(
    ctx: StrategyContext,
    d: DebateContext,
    turn: Analysis,
    ledger: Sequence[Claim],
    cfg: StrategyConfig,
) -> BoardMove:
    moderator = cfg.chairman
    rnd = ctx.topic

    # D1: clarify a garbled transcript (ask, never penalise).
    if turn.garbled:
        return _move(Move.clarify, moderator, "D1", "transcript looks garbled; ask to repeat")

    # D1b: board interruption, feature-flagged and off by default (as UPSC R2).
    if cfg.board_interrupt_enabled and turn.word_count > cfg.interrupt_after_words:
        return _move(
            Move.interrupt, moderator, "D1b",
            f"speech is {turn.word_count} words (> {cfg.interrupt_after_words})",
        )

    # D2: parties, leaders, elections or attacks on groups: the Moderator steers back.
    if turn.off_limits:
        return _move(Move.redirect, moderator, "D2", "off-limits content; back to the motion")

    # D3: silence or a long pause.
    if turn.word_count == 0:
        return _move(Move.reassure, moderator, "D3", "no speech yet (silence)")
    latency = ctx.voice_signals.response_latency_s
    if latency is not None and latency >= cfg.reassure_latency_s:
        return _move(Move.reassure, moderator, "D3", f"long pause before speaking ({latency:.1f}s)")

    # D4: an honest "I don't know" is met with grace and never pounced on.
    if turn.honest_unknown:
        grace = "honest 'I don't know': acknowledge, no cross-question or fact challenge"
        if rnd == OPENING:
            move = _statement(Move.opening_statement, d, "D4", grace)
        elif rnd == CLOSING:
            move = _statement(Move.closing_statement, d, "D4", grace)
        elif _budget_used(ctx):
            move = _move(Move.move_on, moderator, "D4", f"{grace}; clash budget used",
                         target=_next_round(ctx))
        else:
            arg = pick_argument(d)
            move = (
                _move(Move.rebut, OPPONENT, "D4", grace, target=arg.id)
                if arg is not None
                else _move(Move.steelman, OPPONENT, "D4", f"{grace}; no argument standing")
            )
        return move.model_copy(update={"preface": "acknowledge"})

    # D5: the user has just opened or closed: the opponent's statement.
    if rnd == OPENING:
        return _statement(Move.opening_statement, d, "D5", "the user has opened")
    if rnd == CLOSING:
        return _statement(Move.closing_statement, d, "D5", "the user has closed")

    # D6: the clash round's budget is used: the Moderator calls the next round.
    if _budget_used(ctx):
        return _move(
            Move.move_on, moderator, "D6",
            f"round budget used ({ctx.turns_in_topic + 1}/{ctx.max_turns_in_topic})",
            target=_next_round(ctx),
        )

    # D7: a direct question to the opponent is answered first.
    if turn.question_to_opponent:
        arg = pick_argument(d)
        return _move(
            Move.rebut, OPPONENT, "D7", "the user asked the opponent a question: answer it first",
            target=arg.id if arg else None,
        )

    # D8: a strong point that rebuts a standing argument wins a concession.
    standing_ids = [a.id for a in d.standing]
    if turn.point_strength == PointStrength.strong:
        hit = next((i for i in standing_ids if i in turn.rebuts), None)
        if hit is not None:
            return _move(
                Move.concede_point, OPPONENT, "D8",
                f"strong point answers standing argument {hit}: concede it, then pivot",
                target=hit,
            )

    # D9: a delayed fact challenge, only on the motion's own cards (R4's gates).
    allowed_topics = set(d.card_topics)
    candidates = [
        c
        for c in challengeable_claims(ledger, ctx, cfg.fact_challenge_min_delay_turns)
        if cfg.card_topics.get(c.card_id or "") in allowed_topics
    ]
    if candidates:
        claim = candidates[0]
        return _move(
            Move.fact_challenge, OPPONENT, "D9",
            f"claim {claim.id} contradicts card {claim.card_id} (model + numeric check agree)",
            target=claim.id,
        )

    # Cross-questions are never back to back, and never straight after a concession.
    conceded = turn.concedes_point or turn.concedes_error
    may_cross = ctx.last_move != Move.cross_question and not conceded

    # D10: the user ignored the opponent's last point (each point cross-questioned once).
    last = d.last_opponent_arg
    if (
        may_cross
        and last in standing_ids
        and last not in turn.rebuts
        and last not in d.cross_questioned_ids
    ):
        return _move(
            Move.cross_question, OPPONENT, "D10",
            f"the user did not answer the opponent's last point {last}", target=last,
        )

    # D11: claims without a warrant.
    if may_cross and turn.arguments and not any(a.warrant for a in turn.arguments):
        return _move(
            Move.cross_question, OPPONENT, "D11", "claims without a reason: why does that follow?",
            target=WARRANT_TARGET,
        )

    # D12: rebut with the strongest standing argument; steelman once when nothing fresh is left.
    if not d.steelman_used and not fresh_argument_left(d):
        return _move(
            Move.steelman, OPPONENT, "D12",
            "no fresh argument left: ask the user to make the best case for the opponent's side",
        )
    arg = pick_argument(d)
    if arg is None:
        return _move(Move.rebut, OPPONENT, "D12", "every argument conceded; rebut on the motion")
    return _move(Move.rebut, OPPONENT, "D12", f"rebut with standing argument {arg.id}",
                 target=arg.id)


def _statement(move: Move, d: DebateContext, rule: str, why: str) -> BoardMove:
    arg = pick_argument(d)
    return _move(move, OPPONENT, rule, why, target=arg.id if arg else None)


# ---------------------------------------------------------------------------
# Deadline fallback and turn cap (mode hooks, ADR-009)
# ---------------------------------------------------------------------------


def debate_fallback_move(
    ctx: StrategyContext, spoke: bool, reason: str, cfg: StrategyConfig
) -> BoardMove:
    """No analysis of this turn finished in time: silence -> reassure (Moderator); otherwise
    keep the debate moving with the round's statement or a plain rebut. Never a challenge or
    a cross-question, since nothing about the speech is known."""
    if not spoke:
        move = _move(Move.reassure, cfg.chairman, "FALLBACK", reason)
    else:
        d = ctx.debate
        arg = pick_argument(d) if d is not None else None
        kind = {OPENING: Move.opening_statement, CLOSING: Move.closing_statement}.get(
            ctx.topic, Move.rebut
        )
        move = _move(kind, OPPONENT, "FALLBACK", reason, target=arg.id if arg else None)
    return move.model_copy(update={"reason": f"FALLBACK: {reason}", "for_turn": ctx.turn_id})


def debate_cap_move(
    ctx: StrategyContext, move: BoardMove, max_turns: int, cfg: StrategyConfig
) -> BoardMove:
    """The session's hard turn cap: the closing statement if we are in the closing round,
    otherwise the Moderator closes the debate."""
    if move.move == Move.closing_statement or (
        move.move == Move.move_on and move.target == CLOSE_TARGET
    ):
        return move
    if ctx.topic == CLOSING:
        d = ctx.debate
        arg = pick_argument(d) if d is not None else None
        kind, member, target = Move.closing_statement, OPPONENT, (arg.id if arg else None)
    else:
        kind, member, target = Move.move_on, cfg.chairman, CLOSE_TARGET
    return BoardMove(
        move=kind,
        target=target,
        member=member,
        pressure=PRESSURE[kind],
        reason=f"CAP: session turn cap ({max_turns}) reached",
        rule_id="CAP",
        for_turn=ctx.turn_id,
        preface=move.preface,
    )
