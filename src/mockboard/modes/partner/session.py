"""Study partner session hooks: a warm, low-pressure friend ("Mitra") who helps the speaker's own
points get stronger. No scores, no wrap-up pressure, one question per line.

The session (`mockboard/session.py`) stays mode-agnostic: it asks this object for the opener, the
move after each turn (`decide_turn`, with the raw transcript), the spoken line, the state update,
trace fields and the report. The voice loop calls the same session methods as in the other modes.

* Opener: a fixed line per language naming the topic (no model call).
* Each turn: a request to end gets one spoken check ("stop here?"); only a clear yes
  (`director.is_clear_yes`) closes, and anything else carries on with the same question. A
  request to hear the question again, to leave the point, or for a moment to think gets a fixed
  line; otherwise the rule table (`strategist.decide_partner`).
* Every turn is a long turn (`expects_long_turn`): people explain concepts at length.
* Limits: a soft cap of `SOFT_CAP` substantive turns (then a recap and the close), a hard cap of
  `HARD_CAP` turns of any kind, and the voice time limit from settings (`voice_partner_max_s`,
  no wrap-up).
* The speech language is `PartnerSetup.language` (also `PartnerHooks.language`), one of
  hinglish, english, hindi, tanglish; the spoken mix is `PartnerHooks.mix`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mockboard.brain.actor import LineResult, token_budget
from mockboard.brain.requests import ChangeStrength, RequestKind, detect_request
from mockboard.brain.requests_ta import detect_tamil_request
from mockboard.brain.schemas import Analysis, BoardMove, Move, ReportLanguage, StrategyContext
from mockboard.brain.think_time import is_think_request
from mockboard.modes.partner.actor import compose_partner_line
from mockboard.modes.partner.lines import (
    CLOSE_TARGET,
    CONFIRM_END_TARGET,
    RECAP_TARGET,
    REPEAT_LEADS,
    REPEAT_TARGET,
    RESUME_LEADS,
    RESUME_TARGET,
    SKIP_TARGET,
    THINK_TARGET,
    clarify_line,
    confer_line,
    confirm_end_line,
    mix_for,
    opener_line,
    repeat_line,
    resume_line,
    says_unsaid_figure,
    skip_line,
    template_question,
    think_line,
)
from mockboard.modes.partner.strategist import (
    PARTNER,
    SOFT_CAP,
    PartnerSignals,
    PartnerState,
    confirm_end_move,
    decide_partner,
    end_move,
    repeat_move,
    resume_move,
    signals_from,
    skip_move,
    think_move,
)

if TYPE_CHECKING:  # pragma: no cover
    from mockboard.obs.trace import TurnTrace
    from mockboard.session import InterviewSession, OpenTurn

STUDY = "study"  # the one "topic" (round) of a partner session
HARD_CAP = 20  # turns of any kind; the soft cap closes well before it
LANGUAGES: tuple[str, ...] = ("hinglish", "english", "hindi", "tanglish")
TOPIC_MAX_CHARS = 80

# Letters and marks of any script, digits, spaces and basic punctuation. No angle brackets,
# braces, backslashes, at-signs or other markup characters.
_TOPIC_CHARS = re.compile(r"^[\w\s.,:;'’\"“”()?!&/+\-ऀ-ॿ஀-௿]+$")
_URL = re.compile(r"https?:|www\.|://|\b[\w-]+\.(?:com|org|net|in|io|ai|gov|edu|co)\b",
                  re.IGNORECASE)
# A topic names a subject; anything that reads like an order to the model is refused.
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass|jailbreak|pretend|act as|you are|you're"
    r"|roleplay|role play|system prompt|prompt|instructions?|developer mode|do not follow"
    r"|from now on|respond (?:only|with)|reply (?:only|with)|say (?:only|exactly)"
    r"|give (?:me )?(?:full )?(?:marks|score))\b",
    re.IGNORECASE,
)


def validate_topic(raw: str) -> str:
    """The topic, stripped and with inner spaces collapsed; ValueError (plain words) if refused."""
    if not isinstance(raw, str):
        raise ValueError("the topic must be text")
    topic = " ".join(unicodedata.normalize("NFC", raw).split())
    if not topic:
        raise ValueError("say which topic you are studying")
    if len(topic) > TOPIC_MAX_CHARS:
        raise ValueError(f"keep the topic under {TOPIC_MAX_CHARS} characters")
    if _URL.search(topic):
        raise ValueError("a topic cannot be a link")
    if not _TOPIC_CHARS.match(topic):
        raise ValueError("use letters, numbers, spaces and simple punctuation only")
    if not any(ch.isalpha() for ch in topic):
        raise ValueError("the topic needs at least one word")
    if _INSTRUCTION.search(topic):
        raise ValueError("name a subject only, not instructions")
    return topic


@dataclass(frozen=True)
class PartnerSetup:
    """The study partner's setup: the topic and the language. No names or identifiers."""

    topic: str
    language: str = "hinglish"

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic", validate_topic(self.topic))
        if self.language not in LANGUAGES:
            raise ValueError(f"language: one of {', '.join(LANGUAGES)}")

    @property
    def mix(self) -> str:
        return mix_for(self.language)


# A weak "move along" phrase ("chaliye aage") can close an ordinary sentence; it leaves a point
# only when it is nearly the whole turn.
WEAK_SKIP_MAX_WORDS = 6
# Request lines Mitra may have said last: stripped before the earlier question is said again.
_LEADS = tuple(sorted({*REPEAT_LEADS.values(), *RESUME_LEADS.values()}, key=len, reverse=True))


def _request(text: str) -> str:
    """"end", "repeat", "skip", "think" or "" for one turn's raw transcript. The frozen
    detector goes first (its own short-turn and tail windows apply), then the Tamil list, then
    the think detector. A clarify-the-question request is answered by saying the question again:
    Mitra's questions are already plain."""
    if not text.strip():
        return ""
    hit = detect_request(text) or detect_tamil_request(text)
    if hit is not None:
        if hit.kind is RequestKind.END:
            return "end"
        if hit.kind in (RequestKind.REPEAT, RequestKind.CLARIFY_QUESTION):
            return "repeat"
        if hit.kind is RequestKind.SKIP:
            return "skip"
        if hit.kind is RequestKind.CHANGE_TOPIC:
            weak = hit.strength is ChangeStrength.WEAK
            if not weak or len(text.split()) <= WEAK_SKIP_MAX_WORDS:
                return "skip"
    if is_think_request(text):
        return "think"
    return ""


def earlier_question(line: str) -> str:
    """The question inside Mitra's last line, without a repeat or resume lead-in, so asking
    twice never stacks lead-ins ("Zaroor, phir se: Zaroor, phir se: ...")."""
    line = (line or "").strip()
    changed = True
    while changed:
        changed = False
        for lead in _LEADS:
            if line.startswith(lead):
                line = line[len(lead):].strip()
                changed = True
    return line


@dataclass
class PartnerHooks:
    """Study partner behaviour behind the session's mode-hook seam (ADR-009)."""

    session: InterviewSession
    setup: PartnerSetup
    pstate: PartnerState = field(default_factory=PartnerState)
    # One row per turn (the trace's `partner` block): the signals and the decision.
    rows: list[dict[str, Any]] = field(default_factory=list)
    # The listener's note of each point's first turn (English, its own words), for recaps.
    point_notes: list[str] = field(default_factory=list)
    _open: bool = False  # a point is in progress (since its first counted turn, until a recap)
    # The question that stands while Mitra asks "stop here?" (said again if the answer is no).
    _resume_question: str = ""
    _pending: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- wiring ---------------------------------------------------------------

    @property
    def rt(self):
        return self.session.deps.mode(self.session.mode)

    @property
    def mix(self) -> str:
        return self.setup.mix

    @property
    def language(self) -> str:
        """The speech language (Prisma's locale, Timbre's text filter)."""
        return self.setup.language

    def topics(self) -> list[str]:
        return [STUDY]

    def max_turns_in(self, topic: str) -> int:
        return self.pstate.recap_every

    def max_turns(self) -> int:
        return HARD_CAP

    def persona_id(self, member: str) -> str:
        return PARTNER

    def speaker_name(self, member: str) -> str:
        persona = self.rt.personas.get(PARTNER)
        return persona.name if persona is not None else "Mitra"

    @property
    def fact_check_enabled(self) -> bool:
        return False  # the partner never checks or cites facts

    def ctx(self, ctx: StrategyContext) -> StrategyContext:
        return ctx

    def expects_long_turn(self) -> bool:
        return not self.session.state.finished

    def confer_line(self) -> str:
        return confer_line(self.mix)

    def template_line(self, move: BoardMove) -> str | None:
        return template_question(move.move, move.target, self.mix)

    def guard_sentence(self, move: BoardMove, sentence: str) -> bool:
        """Voice mode, one sentence at a time: no figure the speaker never said, and nothing after
        the line's first question (one question per line)."""
        turn = self.session.turn
        sent = list(turn.sent) if turn is not None else []
        if any("?" in x for x in sent):
            return False
        return not says_unsaid_figure(sentence, self._speech())

    def _speech(self, extra: str = "") -> str:
        said = [t.transcript for t in self.session.state.turns if t.transcript]
        if extra and extra not in said:
            said.append(extra)
        return " ".join(said)

    # -- open -----------------------------------------------------------------

    async def open(self) -> tuple[LineResult, BoardMove, dict[str, Any]]:
        move = BoardMove(
            move=Move.move_on, target=RECAP_TARGET, member=PARTNER, pressure=0,
            reason="OPEN: Mitra names the topic and asks for the first point", rule_id="OPEN",
            for_turn="t0",
        )
        extra = {
            "mode": "study_partner",
            "topic": self.setup.topic,
            "language": self.setup.language,
            "soft_cap": self.pstate.soft_cap,
        }
        line = LineResult(opener_line(self.setup.topic, self.mix), guard="partner_template")
        return line, move, extra

    # -- one turn -------------------------------------------------------------

    async def decide_turn(self, turn: OpenTurn, move: BoardMove, analysis: Analysis) -> BoardMove:
        """The partner's decision, with the raw transcript and the full state. The session's hard
        cap (rule CAP) always stands."""
        text = " ".join(s.text for s in turn.segments).strip()
        sig = signals_from(analysis, text)
        request = _request(text)
        if self.pstate.pending_end:  # the reply to "stop here?" (Ruling 52's rule, as the board)
            from mockboard.brain.director import is_clear_yes  # lazy: the director imports much

            request = "end" if is_clear_yes(text) else "continue"
        if move.rule_id == "CAP":
            chosen = move
        elif request == "continue":
            chosen = resume_move(turn.turn_id)
        elif request == "end" and self.pstate.pending_end:
            chosen = end_move(turn.turn_id)
        elif request == "end":
            self._resume_question = earlier_question(self.session.last_question)
            chosen = confirm_end_move(turn.turn_id)
        elif request == "repeat":
            chosen = repeat_move(turn.turn_id)
        elif request == "skip":
            chosen = skip_move(turn.turn_id)
        elif request == "think":
            chosen = think_move(turn.turn_id)
        else:
            chosen = decide_partner(sig, self.pstate, turn_id=turn.turn_id)
            if sig.listener_failed and text:
                chosen = chosen.model_copy(
                    update={"reason": f"{chosen.reason} (listener unavailable: raw-text rules)"}
                )
        self._pending[turn.turn_id] = self._row(sig, request)
        return chosen

    @staticmethod
    def _row(sig: PartnerSignals, request: str) -> dict[str, Any]:
        return {
            "words": sig.words,
            "garbled": sig.garbled,
            "honest_unknown": sig.honest_unknown,
            "has_reason": sig.has_reason,
            "has_example": sig.has_example,
            "concedes": sig.concedes,
            "listener_failed": sig.listener_failed,
            "request": request,
            "counted": sig.counted and not request,
        }

    def _note(self, analysis: Analysis | None) -> str:
        if analysis is None or analysis.is_fallback:
            return ""
        return "; ".join(p.strip() for p in [analysis.stance, *analysis.reasons[:2]] if p.strip())

    @staticmethod
    def _gist(analysis: Analysis | None) -> str:
        """One point's gist for a recap: the listener's stance, or its first reason."""
        if analysis is None or analysis.is_fallback:
            return ""
        return (analysis.stance or next(iter(analysis.reasons), "")).strip()

    async def speak(self, move: BoardMove, answer: str, turn: OpenTurn, tr: TurnTrace
                    ) -> LineResult:
        if move.move == Move.clarify:
            return LineResult(clarify_line(self.mix), guard="partner_template")
        if move.move == Move.reassure and move.target == THINK_TARGET:
            return LineResult(think_line(self.mix), guard="partner_template")
        if move.target == REPEAT_TARGET:
            question = earlier_question(self.session.last_question)
            return LineResult(repeat_line(question, self.mix), guard="partner_template")
        if move.target == CONFIRM_END_TARGET:
            return LineResult(confirm_end_line(self.mix), guard="partner_template")
        if move.target == RESUME_TARGET:
            return LineResult(resume_line(self._resume_question, self.mix),
                              guard="partner_template")
        if move.target == SKIP_TARGET:
            return LineResult(skip_line(self.mix), guard="partner_template")
        s = self.session.deps.settings
        cap = self.rt.profile.word_cap(move.move, s.actor_max_words)
        points = [n for n in self.point_notes if n]
        row = self._pending.get(turn.turn_id, {})
        if move.move == Move.move_on and row.get("counted") and not self._open:
            points.append(self._gist(turn.analysis))  # this turn opens a point and closes it
        tr.mark("actor_request")
        line = await compose_partner_line(
            self.session.deps.llm,
            self.rt.kit,
            move,
            mix=self.mix,
            topic=self.setup.topic,
            previous_line=self.session.last_question,
            answer=answer,
            speech=self._speech(answer),
            note=self._note(turn.analysis),
            points=points,
            max_words=cap,
            max_tokens=token_budget(max(s.actor_max_tokens, cap * 3), self.mix),
            turn_id=turn.turn_id,
            prompt_id=self.rt.profile.prompt_id("actor"),
            on_first_token=lambda: tr.mark("actor_first_token"),
            first_token_budget_s=s.actor_first_token_budget_s,
            fast_throttle_retry_window_s=(
                s.actor_fast_throttle_window_ms / 1000 if s.actor_fast_throttle_retry else None
            ),
        )
        tr.mark("actor_done")
        return line

    def apply(self, move: BoardMove, analysis: Analysis) -> bool:
        """Deterministic state update after a move. Returns True when the session ends."""
        st, p = self.session.state, self.pstate
        row = self._pending.pop(move.for_turn, None) or self._row(signals_from(analysis), "")
        counted = bool(row["counted"])
        since = p.since_recap
        if move.move == Move.move_on and move.target in (RECAP_TARGET, SKIP_TARGET):
            since = 0
        elif counted:
            since += 1
        awaiting = True if move.move in (Move.counter, Move.steelman) else (False if counted else
                                                           p.awaiting_answer)
        reason = p.point_reason or (counted and bool(row["has_reason"]))
        example = p.point_example or (counted and bool(row["has_example"]))
        if counted and not self._open:
            self.point_notes.append(self._gist(analysis))
        closes_point = move.move == Move.move_on
        # A request line (time to think, the question again, "stop here?" and its reply) leaves
        # the earlier question standing, so the rules still see the move that asked it.
        holds = move.move == Move.reassure and move.target in (
            THINK_TARGET, REPEAT_TARGET, CONFIRM_END_TARGET, RESUME_TARGET)
        self.pstate = PartnerState(
            counted=p.counted + int(counted), since_recap=since,
            last_move=p.last_move if holds else move.move,
            pending_end=move.target == CONFIRM_END_TARGET,
            awaiting_answer=awaiting and not closes_point,
            point_reason=reason and not closes_point, point_example=example and not closes_point,
            soft_cap=p.soft_cap, recap_every=p.recap_every,
        )
        self._open = (counted or self._open) and not closes_point
        self.rows.append({"turn_id": move.for_turn, "move": move.move.value,
                          "rule_id": move.rule_id, "target": move.target, **row,
                          "counted_total": self.pstate.counted})
        st.moves.append(move)
        st.turn_index += 1
        st.turns_in_topic = since
        st.honest_unknowns_in_topic += int(bool(row["honest_unknown"]))
        if move.move == Move.move_on and move.target == CLOSE_TARGET:
            st.finished = True
        return st.finished

    def trace_fields(self, move: BoardMove, line: LineResult) -> dict[str, Any]:
        return {
            "mode": "study_partner",
            "speaker": PARTNER,
            "partner": self.rows[-1] if self.rows else {},
        }

    # -- report ---------------------------------------------------------------

    async def report(self, language: Any = None):
        from mockboard.modes.partner.report import generate_partner_report

        st = self.session.state
        s = self.session.deps.settings
        lang = language or s.report_language
        try:
            report_language = ReportLanguage(lang)
        except ValueError:
            report_language = ReportLanguage.en
        return await generate_partner_report(
            self.session.deps.llm,
            self.rt.kit,
            prompt_id=self.rt.profile.prompt_id("summary"),
            session_id=self.session.session_id,
            topic=self.setup.topic,
            language=self.setup.language,
            mix=self.mix,
            turns=st.turns,
            moves=st.moves,
            board_lines=st.board_lines,
            rows={r["turn_id"]: r for r in self.rows},
            report_language=report_language,
            prompt_versions=s.prompt_versions(self.session.mode),
        )


__all__ = [
    "CLOSE_TARGET",
    "HARD_CAP",
    "LANGUAGES",
    "SOFT_CAP",
    "PartnerHooks",
    "PartnerSetup",
    "validate_topic",
]
