"""Debate session hooks (DB5): rounds, the case file, the Moderator's intro and close.

The session (`mockboard/session.py`) stays mode-agnostic: when its mode's profile provides
`session_hooks`, it asks this object for the rounds, the per-turn strategy context, the
spoken line, the state update after a move, extra trace fields, and the report. The voice
loop calls the same three session methods as in UPSC mode and never sees any of this.

Rounds reuse the topics machinery (CTO_NOTES_04 §2): topics are opening, clash and closing.
* opening: one user turn; the opponent's `opening_statement` moves the debate to the clash.
* clash: `exchanges + 1` user turns (Quick: one exchange, Standard: three). On the turn after
  the last exchange the Moderator calls time (D6, `move_on`) and asks for closings.
* closing: the user closes, the opponent's `closing_statement` ends the debate. Then the
  Moderator says "The board will confer" while the judge runs, and reads the verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mockboard.brain.actor import LineResult
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.schemas import (
    Analysis,
    BoardMove,
    DebateContext,
    FactCard,
    Move,
    OpponentArg,
    StrategyContext,
)
from mockboard.modes.debate.actor import DebateBrief, DebateLine, arguments_for, compose_debate_line
from mockboard.modes.debate.guards import (
    OPPONENT_OFF_LIMITS,
    FigurePolicy,
    allowed_figures,
    cards_for_topics,
    off_limits_category,
    sentence_allowed,
)
from mockboard.modes.debate.motions import CaseArgument, Motion, Side
from mockboard.modes.debate.strategist import (
    CLASH,
    CLOSE_TARGET,
    CLOSING,
    OPENING,
    OPPONENT,
    ROUNDS,
    WARRANT_TARGET,
)

if TYPE_CHECKING:  # pragma: no cover
    from mockboard.obs.trace import TurnTrace
    from mockboard.session import InterviewSession, OpenTurn


class Length(StrEnum):
    quick = "quick"  # about 5 minutes, one clash exchange
    standard = "standard"  # about 10 minutes, three clash exchanges


class OpponentStyle(StrEnum):
    logician = "logician"
    wit = "wit"


CLASH_EXCHANGES = {Length.quick: 1, Length.standard: 3}
LANGUAGE_MIX = {"hinglish": "hi-en", "english": "en", "hindi": "hi"}


@dataclass(frozen=True)
class DebateSetup:
    """The setup form (CTO_NOTES_04 §1): motion, side, length, language, opponent style.
    No names or identifiers are asked."""

    motion: Motion
    user_side: Side
    length: Length = Length.quick
    language: str = "hinglish"  # hinglish | hindi | english
    opponent_style: OpponentStyle = OpponentStyle.logician
    case_source: str = "curated"  # "curated", "model" or "generic_fallback: ..."
    case: Sequence[CaseArgument] | None = None  # overrides the motion's case (custom motions)
    # Ruling 29 (CTO_NOTES_05 M1): synthetic test cards never authorise a figure in a real
    # debate. None means "only when the session runs on the offline fake LLM"; an explicit
    # value overrides that (tests).
    allow_synthetic_cards: bool | None = None

    @property
    def opponent_case(self) -> list[CaseArgument]:
        return list(self.case) if self.case is not None else self.motion.opponent_case(
            self.user_side
        )

    @property
    def mix(self) -> str:
        # UNVERIFIED: a Devanagari ("hi") debate falls back to Hinglish templates for now.
        return LANGUAGE_MIX.get(self.language, "hi-en")


_INTRO = {
    "hi-en": (
        "Namaste, aur Mock Board debate mein swagat hai. Aaj ka motion hai: {motion} Aap "
        "motion ke {side} mein bolenge, aur {opponent} doosri taraf se. Rounds honge: "
        "opening, clash aur closing. Faisla bahas ke tareeke par hoga, kaun sa paksh sahi hai "
        "is par nahi. Pehle aapka opening statement, shuru kijiye."
    ),
    "en": (
        "Welcome to the Mock Board debate. Today's motion: {motion} You will speak {side} the "
        "motion, and {opponent} will take the other side. The rounds are opening, clash and "
        "closing. The verdict judges how the debate is argued, not which side is right. Your "
        "opening statement, please."
    ),
}
_SIDE_WORD = {
    "hi-en": {Side.for_: "paksh", Side.against: "virodh"},
    "en": {Side.for_: "for", Side.against: "against"},
}
CONFER = {
    "hi-en": "The board will confer. Ek pal, hum faisla taiyaar kar rahe hain.",
    "en": "The board will confer. One moment while we prepare the verdict.",
}


@dataclass
class DebateSessionHooks:
    """Debate-mode behaviour behind the session's mode-hook seam (ADR-009)."""

    session: InterviewSession
    setup: DebateSetup
    state: DebateContext = field(init=False)
    cards: list[FactCard] = field(init=False)
    allowed: FigurePolicy = field(init=False)
    # One row per candidate turn, the same data the trace line's `debate` block carries; the
    # scoreboard is computed from these rows (or from the trace file) by code (DB6).
    rows: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        motion = self.setup.motion
        allow_synthetic = self.setup.allow_synthetic_cards
        if allow_synthetic is None:
            allow_synthetic = isinstance(self.session.deps.llm, FakeLLMClient)
        self.cards = [
            c for c in cards_for_topics(
                list(self.session.deps.cards.cards.values()), motion.card_topics
            )
            if allow_synthetic or not c.synthetic_example
        ]
        self.allowed = allowed_figures(self.cards, motion.text)
        self.state = DebateContext(
            motion=motion.text,
            user_side=self.setup.user_side.value,
            opponent_args=[
                OpponentArg(id=a.id, claim=a.claim, keywords=list(a.keywords))
                for a in self.setup.opponent_case
            ],
            # Only topics with a card that may be cited: with none, the opponent and the
            # listener are told the motion has no cards, and nothing is fact-checked.
            card_topics=[t for t in motion.card_topics if any(c.topic == t for c in self.cards)],
        )

    # -- wiring ---------------------------------------------------------------

    @property
    def rt(self):
        return self.session.deps.mode(self.session.mode)

    @property
    def mix(self) -> str:
        return self.setup.mix

    def topics(self) -> list[str]:
        return list(ROUNDS)

    def max_turns_in(self, topic: str) -> int:
        if topic == CLASH:
            return CLASH_EXCHANGES[self.setup.length] + 1
        return 1

    def persona_id(self, member: str) -> str:
        if member == OPPONENT:
            return f"opponent_{self.setup.opponent_style.value}"
        return member

    def persona(self, member: str):
        personas = self.rt.personas
        return personas.get(self.persona_id(member)) or personas[self.rt.strategy.chairman]

    def speaker_name(self, member: str) -> str:
        return self.persona(member).name

    @property
    def fact_check_enabled(self) -> bool:
        """Without cards there is nothing to check a figure against (CTO_NOTES_04 §6): the
        user's figures are reported as unverifiable, and no librarian call is made."""
        return bool(self.cards)

    def ctx(self, ctx: StrategyContext) -> StrategyContext:
        return ctx.model_copy(update={"debate": self.state})

    # -- open and close -------------------------------------------------------

    async def open(self) -> tuple[LineResult, BoardMove, dict[str, Any]]:
        mix = self.mix if self.mix in _INTRO else "hi-en"
        text = _INTRO[mix].format(
            motion=self.setup.motion.text,
            side=_SIDE_WORD[mix][self.setup.user_side],
            opponent=self.speaker_name(OPPONENT),
        )
        move = BoardMove(
            move=Move.move_on, target=OPENING, member=self.rt.strategy.chairman, pressure=0,
            reason="INTRO: the Moderator opens the debate", rule_id="INTRO", for_turn="t0",
        )
        extra = {
            "mode": "debate",
            "motion_id": self.setup.motion.id,
            "motion_curated": self.setup.motion.curated,
            "user_side": self.setup.user_side.value,
            "length": self.setup.length.value,
            "language": self.setup.language,
            "opponent_style": self.setup.opponent_style.value,
            "case_source": self.setup.case_source,
            "card_topics": list(self.setup.motion.card_topics),
        }
        return LineResult(text, guard="intro_template"), move, extra

    def guard_sentence(self, move: BoardMove, sentence: str) -> bool:
        """The debate guards, one sentence at a time (voice mode, before TTS): the number
        guard, and for the opponent the off-limits backstop (ruling 29)."""
        if move.move == Move.fact_challenge:
            return True  # the ask-never-assert guard and template own fact challenges
        if move.member == OPPONENT and off_limits_category(sentence, OPPONENT_OFF_LIMITS):
            return False
        return sentence_allowed(sentence, self.allowed)

    def expects_long_turn(self) -> bool:
        st = self.session.state
        return not st.finished and st.topic in (OPENING, CLOSING)

    def confer_line(self) -> str:
        return CONFER.get(self.mix, CONFER["hi-en"])

    # -- one turn -------------------------------------------------------------

    def brief(self, analysis: Analysis, answer: str = "") -> DebateBrief:
        return DebateBrief(
            motion=self.setup.motion.text,
            user_side=self.setup.user_side.value,
            round=self.session.state.topic,
            case=self.setup.opponent_case,
            debate=self.state,
            cards=self.cards,
            allowed=self.allowed,
            mix=self.mix,
            user_question=analysis.question_to_opponent,
            user_point=analysis.arguments[0].claim if analysis.arguments else "",
        )

    async def speak(
        self, move: BoardMove, answer: str, turn: OpenTurn, tr: TurnTrace
    ) -> DebateLine:
        s = self.session.deps.settings
        claim = card = None
        if move.move == Move.fact_challenge:
            claim = self.session._claim(move.target)
            card = self.session.deps.cards.get(claim.card_id) if claim else None
        cap = self.rt.profile.word_cap(move.move, s.actor_max_words)
        tr.mark("actor_request")
        line = await compose_debate_line(
            self.session.deps.llm,
            self.rt.kit,
            self.persona(move.member),
            move,
            self.brief(turn.analysis or Analysis(), answer),
            last_question=self.session.last_question,
            last_answer=answer,
            max_words=cap,
            # UNVERIFIED: about three tokens per Hinglish word leaves room for a 70-word line.
            max_tokens=max(s.actor_max_tokens, cap * 3),
            turn_id=turn.turn_id,
            claim=claim,
            card=card,
            on_first_token=lambda: tr.mark("actor_first_token"),
            first_token_budget_s=s.actor_first_token_budget_s,
            filler_played=lambda: turn.filler_played,
            fast_throttle_retry_window_s=(
                s.actor_fast_throttle_window_ms / 1000 if s.actor_fast_throttle_retry else None
            ),
            prompt_id=self.rt.profile.prompt_id("actor"),
        )
        tr.mark("actor_done")
        return line

    def apply(self, move: BoardMove, analysis: Analysis) -> bool:
        """Deterministic state update after a move. Returns True when the debate ends."""
        st, d = self.session.state, self.state
        before = d
        used = arguments_for(move, before)
        case_ids = {a.id for a in d.opponent_args}
        answered = list(d.answered_ids)
        answered += [r for r in analysis.rebuts if r in case_ids and r not in answered]
        pressed = dict(d.pressed_counts)
        for arg_id in used:
            pressed[arg_id] = pressed.get(arg_id, 0) + 1
        update: dict[str, Any] = {"answered_ids": answered, "pressed_counts": pressed}
        if used:
            update["last_opponent_arg"] = used[0]
        if move.move == Move.concede_point and move.target in case_ids:
            update["conceded_ids"] = [*d.conceded_ids, move.target]
        if move.move == Move.cross_question and move.target != WARRANT_TARGET and move.target:
            update["cross_questioned_ids"] = [*d.cross_questioned_ids, move.target]
            update["last_opponent_arg"] = move.target
        if move.move == Move.steelman:
            update["steelman_used"] = True
        self.state = d.model_copy(update=update)
        self.rows.append(self._row(move, analysis, used, before))

        st.moves.append(move)
        st.turn_index += 1
        if move.move == Move.fact_challenge and move.target:
            st.challenged_claim_ids.append(move.target)
        advance = (
            (move.move == Move.opening_statement and st.topic == OPENING)
            or move.move == Move.move_on
        )
        if move.move == Move.closing_statement or (
            move.move == Move.move_on and move.target == CLOSE_TARGET
        ):
            st.finished = True
        elif advance:
            st.turns_in_topic = 0
            st.honest_unknowns_in_topic = 0
            if st.topic_index + 1 >= len(st.topics):
                st.finished = True
            else:
                st.topic_index += 1
        else:
            st.turns_in_topic += 1
            st.honest_unknowns_in_topic += int(analysis.honest_unknown)
        return st.finished

    def _row(
        self, move: BoardMove, analysis: Analysis, used: list[str], before: DebateContext
    ) -> dict[str, Any]:
        return {
            "turn_id": move.for_turn,
            "round": self.session.state.topic,
            "move": move.move.value,
            "rule_id": move.rule_id,
            "target": move.target,
            "arguments_made": len(analysis.arguments),
            "rebuts": [r for r in analysis.rebuts if r in {a.id for a in before.opponent_args}],
            "question_to_opponent": bool(analysis.question_to_opponent),
            "concedes_point": analysis.concedes_point,
            "off_limits": analysis.off_limits,
            "point_strength": analysis.point_strength.value if analysis.point_strength else None,
            "opponent_arguments_used": used,
            "conceded_ids": list(self.state.conceded_ids),
            "answered_ids": list(self.state.answered_ids),
            "standing_ids": [a.id for a in self.state.standing],
        }

    def trace_fields(self, move: BoardMove, line: LineResult) -> dict[str, Any]:
        row = self.rows[-1] if self.rows else {}
        extra = {
            "mode": "debate",
            "speaker": self.persona_id(move.member),
            "debate": row,
        }
        if isinstance(line, DebateLine):
            extra["debate_line"] = {
                "word_cap": line.word_cap,
                "dropped_sentences": list(line.dropped_sentences),
                "concession_added": line.concession_added,
                "restate_ok": line.restate_ok,
            }
        return extra

    # -- report ---------------------------------------------------------------

    async def report(self, language: Any = None):
        """The debate report (DB6): the judge's rubric and best line (citation-checked, one
        revision at most), the scoreboard computed from `rows`, and the templated verdict."""
        # Imported here: the judge module imports the graphs, which import the mode registry.
        from mockboard.modes.debate.judge import generate_debate_report

        st = self.session.state
        return await generate_debate_report(
            self.session.deps,
            session_id=self.session.session_id,
            turns=st.turns,
            ledger=st.claim_ledger,
            moves=st.moves,
            board_lines=st.board_lines,
            rows=self.rows,
            motion=self.setup.motion.text,
            user_side=self.setup.user_side.value,
            case=self.setup.opponent_case,
            allowed=self.allowed,
            language=language,
            callbacks=self.session.callbacks,
        )
