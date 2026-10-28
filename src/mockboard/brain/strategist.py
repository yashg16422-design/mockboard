"""Strategist: chooses the board's next move with PURE deterministic rules. No LLM, no I/O.

It runs after every analysed segment, so a move is always precomputed by the time the
candidate stops speaking; the actor then only has to phrase it.

Rule table (first matching rule wins). This table is the specification; the unit tests in
tests/test_strategist.py check every row.

| Rule | Condition (on the current turn's segments so far)                          | Move           | Member   |
|------|-----------------------------------------------------------------------------|----------------|----------|
| R1   | the transcript is garbled (most words in garbled segments)                  | clarify        | chairman |
| R2   | FEATURE_BOARD_INTERRUPT on and the answer exceeds `interrupt_after_words`   | interrupt      | chairman |
| R3   | no words at all (silence), or a long pause before answering                 | reassure       | chairman |
|      | (response_latency_s >= reassure_latency_s), or the FIRST honest "I don't    |                |          |
|      | know" in this topic while the topic still has questions left                |                |          |
| R3b  | the SECOND (or later) honest "I don't know" in this topic, or ANY honest    | move_on        | next     |
|      | "I don't know" once the topic's question budget is used up (ruling 12)      |                | topic's  |
| R4   | an unchallenged claim from an earlier turn (>= min delay) is `contradicted` | fact_challenge | owner of |
|      | (model AND numeric check agreed), is numeric or date, and cites a card      |                | the card |
| R5   | topic budget used up, or `strong_streak_to_move_on` strong answers in a row | move_on        | next     |
|      | (target = next topic, or "close" when no topics are left)                   |                | topic's  |
|      |                                                                             |                | member   |
| R6   | the answer is one-sided: counter, or steelman if the last move was counter  | counter /      | topic    |
|      |                                                                             | steelman       | member   |
| R7   | the answer is thin (fewer than 2 reasons and under 25 words) AND pressure   | pressure       | topic    |
|      | is allowed (see "pressure caps" below)                                      |                |          |
| R8   | otherwise (including a thin answer where pressure is not allowed)           | probe          | topic    |

Honesty and anti-whiplash rules (orchestrator ruling 11, CTO_NOTES_02 M2):

* An honest "I don't know" is never punished: R3/R3b always catch it before R4-R7, so it
  never receives pressure, a counter or a fact challenge in that turn.
* Pressure caps: `pressure` is not allowed right after `reassure`, right after another
  `pressure` (at most `max_consecutive_pressure` in a row, default 1), once the topic has
  used `max_pressure_per_topic` pressures (default 2), when the candidate has just
  conceded an error, or when the turn has no real analysis (every segment's listener call
  failed or was abandoned, so "thin" is only a guess). In those cases the board probes.
* Concession preface: when the candidate concedes an error, whatever move is chosen carries
  `preface="acknowledge"`, so the actor first says a short acknowledgement. This is a field
  on the move, not a tenth move.

Safety properties enforced here, not in a prompt: a fact challenge needs a card id, a
"contradicted" verdict (which `librarian.gate_verdict` only issues when the model and the
numeric check agree), and a numeric/date claim. `possible_conflict` and `unverifiable`
claims can never be challenged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from mockboard.brain.schemas import (
    Analysis,
    Balance,
    BoardMove,
    Claim,
    Move,
    PointStrength,
    StrategyContext,
    Verdict,
)

PRESSURE: dict[Move, int] = {
    Move.probe: 1,
    Move.counter: 2,
    Move.steelman: 2,
    Move.fact_challenge: 2,
    Move.pressure: 3,
    Move.reassure: 0,
    Move.move_on: 1,
    Move.clarify: 0,
    Move.interrupt: 2,
    # Debate mode (CTO_NOTES_04 §2).
    Move.opening_statement: 1,
    Move.closing_statement: 1,
    Move.rebut: 2,
    Move.cross_question: 2,
    Move.concede_point: 0,
    Move.redirect: 1,
}
THIN_ANSWER_WORDS = 25
_STRENGTH_ORDER = [PointStrength.weak, PointStrength.adequate, PointStrength.strong]
CLOSE_TARGET = "close"  # move_on target meaning "no topics left: close the interview"


@dataclass(frozen=True)
class StrategyConfig:
    """Thresholds and board wiring. Built from Settings; plain data so the rules stay pure."""

    chairman: str
    topic_members: Mapping[str, str] = field(default_factory=dict)  # topic -> persona id
    card_topics: Mapping[str, str] = field(default_factory=dict)  # card id -> topic
    fact_challenge_min_delay_turns: int = 1
    strong_streak_to_move_on: int = 3
    reassure_latency_s: float = 4.0
    board_interrupt_enabled: bool = False
    interrupt_after_words: int = 150
    max_consecutive_pressure: int = 1
    max_pressure_per_topic: int = 2

    def __post_init__(self) -> None:
        # Defence in depth behind the Settings validator (ruling 9): a same-turn challenge
        # would need this turn's verdicts, which the fast graph never has.
        if self.fact_challenge_min_delay_turns < 1:
            raise ValueError("fact_challenge_min_delay_turns must be >= 1")

    def member_for(self, topic: str) -> str:
        return self.topic_members.get(topic, self.chairman)


def merge_analyses(analyses: Sequence[Analysis]) -> Analysis:
    """Combine the per-segment analyses of one turn into a single turn-level view."""
    if not analyses:
        return Analysis()
    balances = [a.balance for a in analyses if a.balance != Balance.unclear]
    if Balance.balanced in balances:
        balance = Balance.balanced
    elif balances:
        balance = Balance.one_sided
    else:
        balance = Balance.unclear
    total_words = sum(a.word_count for a in analyses)
    garbled_words = sum(a.word_count for a in analyses if a.garbled)
    reasons: list[str] = []
    for a in analyses:
        reasons.extend(r for r in a.reasons if r not in reasons)
    # Debate-mode fields (neutral defaults in UPSC mode, so this changes nothing there).
    rebuts: list[str] = []
    for a in analyses:
        rebuts.extend(r for r in a.rebuts if r not in rebuts)
    strengths = [a.point_strength for a in analyses if a.point_strength is not None]
    return Analysis(
        turn_id=analyses[-1].turn_id,
        segment_id=analyses[-1].segment_id,
        stance=next((a.stance for a in reversed(analyses) if a.stance), ""),
        reasons=reasons,
        claims=[c for a in analyses for c in a.claims],
        balance=balance,
        honest_unknown=any(a.honest_unknown for a in analyses),
        concedes_error=any(a.concedes_error for a in analyses),
        garbled=(total_words > 0 and garbled_words * 2 > total_words)
        or (total_words == 0 and any(a.garbled for a in analyses)),
        word_count=total_words,
        is_fallback=all(a.is_fallback for a in analyses),
        arguments=[x for a in analyses for x in a.arguments],
        rebuts=rebuts,
        question_to_opponent=next(
            (a.question_to_opponent for a in reversed(analyses) if a.question_to_opponent), ""
        ),
        concedes_point=any(a.concedes_point for a in analyses),
        point_strength=max(strengths, key=_STRENGTH_ORDER.index) if strengths else None,
        off_limits=any(a.off_limits for a in analyses),
    )


def challengeable_claims(
    ledger: Sequence[Claim], ctx: StrategyContext, min_delay_turns: int
) -> list[Claim]:
    """Claims that may be challenged now, oldest first."""
    seen: set[str] = set()
    out: list[Claim] = []
    for c in ledger:
        if c.id in seen:
            continue
        seen.add(c.id)
        if (
            c.verdict == Verdict.contradicted
            and c.card_id
            and c.is_numeric_or_date
            and c.id not in ctx.challenged_claim_ids
            and ctx.turn_index - c.turn_index >= min_delay_turns
        ):
            out.append(c)
    return sorted(out, key=lambda c: (c.turn_index, c.id))


def _move(move: Move, member: str, rule: str, why: str, target: str | None = None) -> BoardMove:
    return BoardMove(
        move=move,
        target=target,
        member=member,
        pressure=PRESSURE[move],
        reason=f"{rule}: {why}",
        rule_id=rule,
    )


def pressure_blocker(ctx: StrategyContext, turn: Analysis, cfg: StrategyConfig) -> str | None:
    """Why `pressure` is not allowed this turn, or None when it is (anti-whiplash caps)."""
    if turn.honest_unknown:
        return "honest 'I don't know' is never pressed"
    if turn.is_fallback:
        return "no real analysis of this answer (listener fallback): never press on a guess"
    if turn.concedes_error:
        return "candidate just conceded an error"
    if ctx.last_move == Move.reassure:
        return "no pressure right after reassure"
    if ctx.last_move == Move.pressure and cfg.max_consecutive_pressure <= 1:
        return "no pressure twice in a row"
    if ctx.pressure_in_topic >= cfg.max_pressure_per_topic:
        return f"pressure cap for this topic reached ({cfg.max_pressure_per_topic})"
    return None


def _budget_used(ctx: StrategyContext) -> bool:
    """True when this answer is the last one the topic's question budget allows."""
    return ctx.turns_in_topic + 1 >= max(1, ctx.max_turns_in_topic)


def _move_on(ctx: StrategyContext, cfg: StrategyConfig, rule: str, why: str) -> BoardMove:
    nxt = ctx.next_topic
    member = cfg.member_for(nxt) if nxt else cfg.chairman
    return _move(Move.move_on, member, rule, why, target=nxt or CLOSE_TARGET)


def decide_move(
    ctx: StrategyContext,
    analyses: Sequence[Analysis],
    ledger: Sequence[Claim],
    cfg: StrategyConfig,
) -> BoardMove:
    """Apply the rule table. `analyses` are this turn's segments so far."""
    turn = merge_analyses([a for a in analyses if a.turn_id == ctx.turn_id])
    move = _decide(ctx, turn, ledger, cfg)
    update: dict = {"for_turn": ctx.turn_id}
    if turn.concedes_error and move.move != Move.clarify:
        update["preface"] = "acknowledge"
        update["reason"] = move.reason + " (+ acknowledge the conceded error first)"
    return move.model_copy(update=update)


def _decide(
    ctx: StrategyContext,
    turn: Analysis,
    ledger: Sequence[Claim],
    cfg: StrategyConfig,
) -> BoardMove:
    topic_member = cfg.member_for(ctx.topic)
    signals = ctx.voice_signals

    # R1: clarify garbled transcripts (dialect fairness: ask, never penalise).
    if turn.garbled:
        return _move(Move.clarify, cfg.chairman, "R1", "transcript looks garbled; ask to repeat")

    # R2: board-initiated interruption (feature-flagged, off by default).
    if cfg.board_interrupt_enabled and turn.word_count > cfg.interrupt_after_words:
        return _move(
            Move.interrupt, cfg.chairman, "R2",
            f"answer is {turn.word_count} words (> {cfg.interrupt_after_words})",
        )

    # R3: reassure.
    if turn.word_count == 0:
        return _move(Move.reassure, cfg.chairman, "R3", "no answer yet (silence)")
    latency = signals.response_latency_s
    if latency is not None and latency >= cfg.reassure_latency_s:
        return _move(
            Move.reassure, cfg.chairman, "R3",
            f"long pause before answering ({signals.response_latency_s:.1f}s)",
        )
    # R3/R3b: honest "I don't know". Never punished: first time reassure, then move on.
    # Ruling 12: with the topic's budget used up, reassuring with a simpler question and
    # then leaving the topic at once reads oddly, so the board moves on straight away.
    if turn.honest_unknown:
        if _budget_used(ctx):
            return _move_on(
                ctx, cfg, "R3b",
                f"honest 'I don't know' and the topic budget is used "
                f"({ctx.turns_in_topic + 1}/{ctx.max_turns_in_topic}); move on",
            )
        if ctx.honest_unknowns_in_topic == 0:
            return _move(
                Move.reassure, cfg.chairman, "R3",
                "first honest 'I don't know' in this topic; reassure, ask something simpler",
            )
        return _move_on(
            ctx, cfg, "R3b",
            f"honest 'I don't know' #{ctx.honest_unknowns_in_topic + 1} in this topic; move on",
        )

    # R4: delayed fact challenge, only on gated numeric/date contradictions with a card.
    candidates = challengeable_claims(ledger, ctx, cfg.fact_challenge_min_delay_turns)
    if candidates:
        claim = candidates[0]
        card_topic = cfg.card_topics.get(claim.card_id or "", ctx.topic)
        return _move(
            Move.fact_challenge, cfg.member_for(card_topic), "R4",
            f"claim {claim.id} contradicts card {claim.card_id} (model + numeric check agree)",
            target=claim.id,
        )

    # R5: move on when the topic budget is used or the candidate is consistently strong.
    turns_after = ctx.turns_in_topic + 1
    streak_after = ctx.strong_streak + 1 if turn.is_strong else 0
    if _budget_used(ctx) or streak_after >= cfg.strong_streak_to_move_on:
        why = (
            f"topic budget used ({turns_after}/{ctx.max_turns_in_topic})"
            if turns_after >= ctx.max_turns_in_topic
            else f"{streak_after} strong answers in a row"
        )
        return _move_on(ctx, cfg, "R5", why)

    # R6: one-sided answers get a counter, then a steelman request.
    if turn.balance == Balance.one_sided:
        if ctx.last_move == Move.counter:
            return _move(Move.steelman, topic_member, "R6", "still one-sided after a counter")
        return _move(Move.counter, topic_member, "R6", "answer argues only one side")

    # R7: thin answers get polite pressure, within the anti-whiplash caps.
    thin = len(turn.reasons) < 2 and turn.word_count < THIN_ANSWER_WORDS
    if thin:
        blocker = pressure_blocker(ctx, turn, cfg)
        detail = f"thin answer ({len(turn.reasons)} reasons, {turn.word_count} words)"
        if blocker is None:
            return _move(Move.pressure, topic_member, "R7", detail)
        return _move(Move.probe, topic_member, "R8", f"{detail}, but {blocker}")

    # R8: default follow-up.
    return _move(Move.probe, topic_member, "R8", "go one level deeper")
