"""Debate actor: the Moderator's and the Opponent's spoken lines (CTO_NOTES_04 §1, §3).

The same single-attempt streamed call as the UPSC actor (`brain.actor.call_actor`, rulings 14
and 17), with the debate prompt (`actor_debate.v1.md`), per-move word caps from the profile,
and the debate guards in code (`guards.py`):

* the number guard drops any sentence with a figure that is not on the motion's cards;
* a closing statement always concedes something (template backstop);
* a missed fair restatement is logged (soft);
* a fact challenge keeps the UPSC ask-never-assert guard and template.

If the actor fails, or the guards leave nothing to say, the board says a deterministic line:
a move that changes the debate's state (opening and closing statements, concede_point,
cross_question, move_on, fact_challenge) uses its move template, built from the hand-written
case file, so the state change is still heard; other moves use the speaking persona's own
fallback line, as in UPSC mode. After a masking filler (ruling 17) every move uses its
template. Every template is figure-free.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from mockboard.brain.actor import (
    ACKNOWLEDGE_LINES,
    LineResult,
    call_actor,
    challenge_facts,
    count_words,
    fact_challenge_template,
    guard_fact_challenge,
    persona_fallback_line,
    with_preface,
)
from mockboard.brain.llm import LLMClient
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    BoardMove,
    Claim,
    DebateContext,
    FactCard,
    Move,
    Persona,
)
from mockboard.modes.debate.guards import (
    RESTATE_MOVES,
    FigurePolicy,
    has_concession,
    number_guard,
    restates_user,
)
from mockboard.modes.debate.motions import CaseArgument
from mockboard.modes.debate.strategist import (
    CLOSE_TARGET,
    WARRANT_TARGET,
    opening_args,
    pick_argument,
)

_NO_FIGURES = FigurePolicy()

# Moves whose effect on the debate must be heard even when the actor fails (see docstring).
STATE_MOVES = frozenset(
    {
        Move.opening_statement,
        Move.closing_statement,
        Move.concede_point,
        Move.cross_question,
        Move.move_on,
        Move.fact_challenge,
    }
)


@dataclass(frozen=True)
class DebateBrief:
    """What the debate actor needs to know about the debate, built by the session."""

    motion: str
    user_side: str  # "for" | "against"
    round: str
    case: Sequence[CaseArgument]  # the opponent's case file
    debate: DebateContext  # standing / conceded / pressed / answered, as of this turn
    cards: Sequence[FactCard] = ()  # the motion's cards (empty for most motions)
    # Which figures the number guard allows, and where (ruling 29). The empty policy
    # allows none. Frozen, so sharing one default instance is safe.
    allowed: FigurePolicy = _NO_FIGURES
    mix: str = "hi-en"
    user_question: str = ""  # D7: the question to answer first
    user_point: str = ""  # the user's best argument this turn (for templates)

    @property
    def opponent_side(self) -> str:
        return "against" if self.user_side == "for" else "for"

    def arg(self, arg_id: str | None) -> CaseArgument | None:
        return next((a for a in self.case if a.id == arg_id), None)


@dataclass(frozen=True)
class DebateLine(LineResult):
    """A `LineResult` plus what the debate guards did, for the trace."""

    dropped_sentences: tuple[str, ...] = ()
    concession_added: bool = False
    restate_ok: bool | None = None  # None: not checked for this move
    word_cap: int = 0
    arguments_used: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Which case arguments a move uses (pure; the session applies the same choice to its state)
# ---------------------------------------------------------------------------


def arguments_for(move: BoardMove, d: DebateContext) -> list[str]:
    """Ids of the opponent's case arguments this move presses."""
    if move.move == Move.opening_statement:
        return [a.id for a in opening_args(d)]
    if move.move == Move.concede_point:
        pivot = pick_argument(d, exclude=[move.target or ""])
        return [pivot.id] if pivot else []
    if move.move in (Move.rebut, Move.closing_statement) or (
        move.move == Move.cross_question and move.target != WARRANT_TARGET
    ):
        return [move.target] if move.target else []
    return []


# ---------------------------------------------------------------------------
# Templates (deterministic, figure-free)
# ---------------------------------------------------------------------------

_T: dict[str, dict[str, str]] = {
    "hi-en": {
        "opening": "Aapne apna paksh achhe se rakha. Ab doosri taraf se: {a1}{a2} "
        "Ab aap jawab dijiye.",
        "second": " Aur doosri baat: {claim}",
        "rebut": "Aapki baat samajh aati hai. Lekin {claim} {warrant} Iska jawab kya hai?",
        "concede": "Yeh point aapka mazboot hai, ise maanna padega. Lekin {claim}",
        "concede_none": "Yeh point aapka mazboot hai, ise maanna padega.",
        "cross": "Aapne mere is point ka jawab nahi diya: {claim} Iska jawab kya hai?",
        "cross_warrant": "Yeh baat kyun sach hai? Aapka tark kya hai?",
        "closing": "Maanna padega ki {concession} Phir bhi, {claim} Isliye mera paksh wahi "
        "hai.",
        "concession_answered": "'{claim}' par aapka jawab mazboot tha.",
        "concession_generic": "aapne kuch baatein bahut achhi rakhi.",
        "steelman": "Ek sentence mein mere paksh ka sabse mazboot tark bataiye.",
        "redirect": "Chaliye, parties, netaon aur samoohon ko alag rakhte hain. Motion par "
        "wapas aaiye: {motion}",
        "reassure": "Koi baat nahi, aaram se. Apna sabse mazboot point rakhiye.",
        "clarify": "Maaf kijiye, main theek se sun nahi paaya. Kya aap phir se bata sakte hain?",
        "move_on": "Samay ho gaya. Ab closing statements: pehle aap, apni sabse mazboot baat "
        "rakhiye.",
        "close": "Shukriya. Debate yahin samaapt hoti hai.",
        "interrupt": "Ek minute, main beech mein rokta hoon. Apne mukhya point par aaiye.",
    },
    "en": {
        "opening": "You have made your case well. Let me take the other side: {a1}{a2} "
        "Over to you.",
        "second": " And second: {claim}",
        "rebut": "I see your point. But {claim} {warrant} How do you answer that?",
        "concede": "That is a strong point, and I concede it. But {claim}",
        "concede_none": "That is a strong point, and I concede it.",
        "cross": "You have not answered this point: {claim} What is your answer?",
        "cross_warrant": "Why does that follow? What is your reason?",
        "closing": "I accept that {concession} Even so, {claim} So I stand by my side.",
        "concession_answered": "your answer on '{claim}' was strong.",
        "concession_generic": "you made some very good points.",
        "steelman": "In one sentence, make the best case for my side.",
        "redirect": "Let's keep parties, personalities and groups out of this. Back to the "
        "motion: {motion}",
        "reassure": "No problem, take your time. Give us your strongest point.",
        "clarify": "Sorry, I did not catch that. Could you say it again?",
        "move_on": "Time. On to closing statements: you first, with your strongest point.",
        "close": "Thank you. That ends the debate.",
        "interrupt": "One moment, let me stop you there. Please come to your main point.",
    },
}


def _t(mix: str) -> dict[str, str]:
    return _T.get(mix, _T["hi-en"])  # Hindi (Devanagari) falls back to Hinglish for now


def _lc(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def move_template(move: BoardMove, brief: DebateBrief) -> str:
    """The deterministic line for a move, from the case file. Figure-free by construction."""
    t, d = _t(brief.mix), brief.debate
    if move.move == Move.opening_statement:
        args = [brief.arg(i) for i in arguments_for(move, d)]
        args = [a for a in args if a]
        a1 = f"{args[0].claim} {args[0].warrant}" if args else ""
        a2 = t["second"].format(claim=args[1].claim) if len(args) > 1 else ""
        return t["opening"].format(a1=a1, a2=a2)
    if move.move == Move.rebut:
        a = brief.arg(move.target) or (pick_argument(d) and brief.arg(pick_argument(d).id))
        if a is None:
            return t["reassure"]
        return t["rebut"].format(claim=_lc(a.claim), warrant=a.warrant)
    if move.move == Move.concede_point:
        pivot = [brief.arg(i) for i in arguments_for(move, d)]
        if pivot and pivot[0]:
            return t["concede"].format(claim=_lc(pivot[0].claim))
        return t["concede_none"]
    if move.move == Move.cross_question:
        a = brief.arg(move.target)
        if move.target == WARRANT_TARGET or a is None:
            return t["cross_warrant"]
        return t["cross"].format(claim=a.claim)
    if move.move == Move.closing_statement:
        a = brief.arg(move.target) or brief.arg((pick_argument(d) or brief.case[0]).id)
        return t["closing"].format(concession=concession_text(brief), claim=_lc(a.claim))
    if move.move == Move.steelman:
        return t["steelman"]
    if move.move == Move.redirect:
        return t["redirect"].format(motion=brief.motion)
    if move.move == Move.move_on:
        return t["close"] if move.target == CLOSE_TARGET else t["move_on"]
    if move.move in (Move.reassure, Move.clarify, Move.interrupt):
        return t[move.move.value]
    return t["reassure"]


def concession_text(brief: DebateBrief) -> str:
    """The honest concession a closing statement makes: the user's answer on one of the
    opponent's arguments they actually answered, else a generic one."""
    t, d = _t(brief.mix), brief.debate
    answered = [a for a in brief.case if a.id in d.answered_ids or a.id in d.conceded_ids]
    if answered:
        return t["concession_answered"].format(claim=answered[0].claim.rstrip("."))
    return t["concession_generic"]


def closing_concession_sentence(brief: DebateBrief) -> str:
    mix = brief.mix
    # Gender-neutral on purpose: Hindi verbs agree with the speaker, and opponents differ.
    lead = "I accept that " if mix == "en" else "Maanna padega ki "
    text = concession_text(brief)
    return lead + text


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------


def _arg_json(a: CaseArgument) -> dict[str, str]:
    return {"id": a.id, "claim": a.claim, "reason": a.warrant}


def build_debate_actor_user(
    persona: Persona,
    move: BoardMove,
    brief: DebateBrief,
    *,
    last_question: str,
    last_answer: str,
    max_words: int,
    claim: Claim | None = None,
    card: FactCard | None = None,
) -> str:
    d = brief.debate
    used = [brief.arg(i) for i in arguments_for(move, d)]
    board = {
        "speaker": f"{persona.name}, {persona.title}",
        "speaker_style": persona.style,
        "move": move.move.value,
        "move_reason": move.reason,
        "round": brief.round,
        "motion": brief.motion,
        "user_side": brief.user_side,
        "opponent_side": brief.opponent_side,
        "language_mix": brief.mix,
        "max_words": max_words,
        "preface_already_spoken": move.preface,
        "use_arguments": [_arg_json(a) for a in used if a],
    }
    if move.move == Move.rebut and move.rule_id == "D7" and brief.user_question:
        board["answer_first"] = brief.user_question
    if move.move == Move.concede_point:
        conceded = brief.arg(move.target)
        board["concede"] = _arg_json(conceded) if conceded else None
    if move.move == Move.cross_question:
        board["cross_on"] = "warrant" if move.target == WARRANT_TARGET else move.target
    if move.move == Move.move_on:
        board["next_round"] = move.target
    if move.move == Move.closing_statement:
        board["concede_first"] = concession_text(brief)
    cards = [
        {
            "figure": f"{c.value:g} {c.unit or ''}".strip() if c.value is not None else "",
            "year": c.year,
            "source_short": c.source_short or "",
        }
        for c in brief.cards
    ]
    parts = [
        tag("debate_board", json.dumps(board, ensure_ascii=False, indent=1)),
        tag("motion_cards", json.dumps(cards, ensure_ascii=False)),
        tag("last_question", last_question),
        tag("candidate_speech", last_answer),
    ]
    if move.move == Move.fact_challenge and claim and card:
        parts.append(tag("claim_to_revisit", claim.text))
        facts = challenge_facts(claim, card, brief.mix)
        parts.append(tag("fact_challenge", facts.as_prompt_json()))
    parts.append("Say the line now.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Composing the line
# ---------------------------------------------------------------------------


def preface_for(move: BoardMove, mix: str) -> str:
    if move.preface == "acknowledge":
        return ACKNOWLEDGE_LINES.get(mix, ACKNOWLEDGE_LINES["hi-en"])
    return ""


def debate_fallback(
    move: BoardMove, persona: Persona, brief: DebateBrief, turn_id: str, *, error: str,
    guard: str, filler_played: bool, claim: Claim | None = None, card: FactCard | None = None,
) -> DebateLine:
    if move.move == Move.fact_challenge and claim and card:
        text, kind = fact_challenge_template(claim, card, brief.mix), "challenge_template"
    elif move.move in STATE_MOVES or filler_played:
        text, kind = move_template(move, brief), "move_template"
    else:
        text, kind = persona_fallback_line(persona, turn_id), "persona_line"
    return DebateLine(text, is_fallback=True, guard=guard, fallback_kind=kind, error=error)


def apply_guards(
    text: str, move: BoardMove, brief: DebateBrief, *, last_answer: str, max_words: int,
    claim: Claim | None = None, card: FactCard | None = None,
) -> DebateLine:
    """The debate guards on a model-written line (or on any line, in tests)."""
    if move.move == Move.fact_challenge and claim and card:
        return DebateLine(**_fields(guard_fact_challenge(text, claim, card, brief.mix)))
    guarded = number_guard(text, brief.allowed)
    text, guards = guarded.text, (["number_guard"] if guarded.dropped else [])
    concession_added = False
    if move.move == Move.closing_statement and not has_concession(text):
        lead = closing_concession_sentence(brief)
        room = max_words - count_words(lead)
        words = text.split()
        text = (lead + " " + " ".join(words[: max(room, 0)])).strip()
        concession_added = True
        guards.append("closing_concession")
    restate = restates_user(text, last_answer) if move.move in RESTATE_MOVES else None
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words])
    return DebateLine(
        text,
        guard="+".join(guards),
        dropped_sentences=guarded.dropped,
        concession_added=concession_added,
        restate_ok=restate,
    )


def _fields(line: LineResult) -> dict:
    return {
        "text": line.text, "is_fallback": line.is_fallback, "guard": line.guard,
        "fallback_kind": line.fallback_kind, "error": line.error, "retried": line.retried,
    }


async def compose_debate_line(
    llm: LLMClient,
    kit: PromptKit,
    persona: Persona,
    move: BoardMove,
    brief: DebateBrief,
    *,
    last_question: str,
    last_answer: str,
    max_words: int,
    max_tokens: int,
    turn_id: str,
    claim: Claim | None = None,
    card: FactCard | None = None,
    on_first_token: Callable[[], None] | None = None,
    first_token_budget_s: float | None = None,
    filler_played: Callable[[], bool] | None = None,
    fast_throttle_retry_window_s: float | None = None,
    prompt_id: str = "actor_debate",
) -> DebateLine:
    """Phrase a debate move: one attempt, the debate guards, then the preface."""
    preface = preface_for(move, brief.mix)
    cap = max_words - count_words(preface)
    used = tuple(arguments_for(move, brief.debate))

    def finish(line: DebateLine) -> DebateLine:
        out = with_preface(preface, line)
        return replace(out, word_cap=max_words, arguments_used=used)

    if move.move == Move.fact_challenge and (claim is None or card is None):
        return finish(DebateLine(move_template(
            move.model_copy(update={"move": Move.rebut}), brief), is_fallback=True,
            guard="no_card"))
    user = build_debate_actor_user(
        persona, move, brief, last_question=last_question, last_answer=last_answer,
        max_words=cap, claim=claim, card=card,
    )
    call = await call_actor(
        llm, kit, user, max_words=cap, max_tokens=max_tokens, turn_id=turn_id,
        on_first_token=on_first_token, first_token_budget_s=first_token_budget_s,
        fast_throttle_retry_window_s=fast_throttle_retry_window_s, prompt_id=prompt_id,
    )
    played = bool(filler_played()) if filler_played is not None else False
    if call.error:
        line = debate_fallback(move, persona, brief, turn_id, error=call.error,
                               guard=call.guard, filler_played=played, claim=claim, card=card)
        return finish(replace(line, retried=call.retried))
    line = apply_guards(call.text, move, brief, last_answer=last_answer, max_words=cap,
                        claim=claim, card=card)
    if not line.text.strip():
        # The guards left nothing to say (every sentence had an uncarded figure).
        fb = debate_fallback(move, persona, brief, turn_id, error="", guard=line.guard,
                             filler_played=True)  # template, never a bare pause
        line = replace(fb, is_fallback=True, dropped_sentences=line.dropped_sentences)
    return finish(replace(line, retried=call.retried))


