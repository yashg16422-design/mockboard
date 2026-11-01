"""Panel session hooks (P4): the agenda, the director, five members, hand-offs, memory and a close,
behind the session's mode-hook seam (ADR-009, CTO_NOTES_06 §2, §7).

The session (`mockboard/session.py`) stays mode-agnostic. With a `PanelSetup` it asks this object
for the topics (the agenda's segment ids), the per-turn context, the move after the director has
had its say (`decide_turn`), the spoken line, the state update after a move, trace fields and the
report. The voice loop calls the same session methods and never sees any of this.

What this module owns, and the director does not:
- the session's position (`topic_index`, `turns_in_topic`) and the counters. `decide_turn` writes
  nothing; only `apply` moves them, once per finished turn (CTO_NOTES_12 §9.5);
- who speaks: the segment's member speaks everything in the segment, including reassure and
  clarify, where the strategist would give the Chairperson the line;
- refusals: after a topic change the refused terms, widened across scripts, are kept from every
  later line, seed and fact challenge (A1c), and that segment's unchallenged claims are never
  challenged later;
- the fixed `asked` question the director's memory keeps, so a repeat replays a real question.

Nothing here calls a model except the line itself (`compose_panel_line`) and the opener.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mockboard.brain.actor import LineResult, language_mix
from mockboard.brain.agenda import (
    NEUTRAL_SEEDS,
    build_fallback_agenda,
    capped_budgets,
    seed_alternatives,
)
from mockboard.brain.avoid_terms import expand_terms, find_avoided
from mockboard.brain.director import PanelDirector, opening_memory
from mockboard.brain.graphs import generate_report
from mockboard.brain.panel_actor import (
    PanelLine,
    compose_panel_line,
    fixed_parts,
    generate_panel_opener,
    joined,
    question_part,
)
from mockboard.brain.panel_lines import RITUAL_LINES, line_for
from mockboard.brain.personas import PERSONA_DIR, chairman, load_personas
from mockboard.brain.prompts import build_prompt_kit, check_prefix_budget
from mockboard.brain.schemas import (
    Analysis,
    BoardMove,
    CompletedTurn,
    DirectorTurn,
    Move,
    PanelDecision,
    PanelSettings,
    PrefacePart,
    StrategyContext,
)
from mockboard.brain.strategist import PRESSURE
from mockboard.modes.upsc_panel.setup import PanelSetup

if TYPE_CHECKING:  # pragma: no cover
    from mockboard.obs.trace import TurnTrace
    from mockboard.session import InterviewSession, OpenTurn

# Moves that say a fixed line or rephrase: they leave `last_move` alone, so "please repeat" cannot
# let a second pressure through (the strategist guards on `last_move`; CTO_NOTES_15 §9.2).
KEEPS_LAST_MOVE = frozenset({Move.repeat_question, Move.rephrase_question, Move.clarify})
# `format_report` prints this wording as a note; Ruling 51 binds it until P12 gives a real figure
# (CTO_NOTES_09 §4, verbatim).
REQUEST_ACCURACY_NOTE = (
    "Listed request sentences work every time, in our tests, with the listener up or down, in "
    "Latin and Devanagari. Rephrased requests are caught less often: 0.75 on held-out synthetic "
    "lines by rules alone. Accuracy on real speech has not been measured yet."
)
_SOURCE_WORDS = {
    "rules": "found by the board's rules",
    "rules+llm": "found by the board's rules and confirmed by the listener",
    "llm": "found by the listener",
}


@dataclass
class PanelHooks:
    """Panel-mode behaviour behind the session's mode-hook seam."""

    session: InterviewSession
    setup: PanelSetup
    # Set by `session.request_wrap_up()`: the next finished turn closes with the time line.
    wrap_up_due: bool = False
    decision: PanelDecision | None = field(init=False, default=None)
    answers_used: int = field(init=False, default=0)
    free_used: int = field(init=False, default=0)
    last_move: Move | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        s = self.session
        if s.form is None:
            raise ValueError("a panel session needs the candidate's application form")
        self.settings = s.deps.settings
        self.personas = load_personas(PERSONA_DIR / "panel")
        self.chair_id = chairman(self.personas).id
        self.kit = build_prompt_kit(
            self.personas, s.deps.cards, self.settings.panel_prompt_versions()
        )
        check_prefix_budget(self.kit.shared_prefix, self.settings.cache_prefix_token_budget)
        self.agenda = build_fallback_agenda(s.form, capped_budgets(self.setup.max_answers))
        self.mix = language_mix(self.personas[self.chair_id], s.form)
        self.cfg = PanelSettings(
            chairman=self.chair_id,
            names={p.id: p.name for p in self.personas.values()},
            mix=self.mix,
            max_answers=self.setup.max_answers,
            free_turns=self.setup.free_turns,
        )
        self.director = PanelDirector(s.session_id, self.agenda, self.cfg)
        # Terms the board must not say, widened across scripts, and the turns whose claims must
        # never be challenged: both set only by `apply`, so a reopened turn changes neither.
        self._refused: list[str] = []
        self._refused_turns: set[str] = set()
        self._line: PanelLine | None = None
        self._director_ms: float = 0.0
        self._raw = ""
        self._row: dict[str, Any] = {}
        self._finished = False

    # -- wiring ---------------------------------------------------------------

    def topics(self) -> list[str]:
        return self.agenda.topics

    def max_turns_in(self, topic: str) -> int:
        return self.agenda.segment(topic).budget

    def max_turns(self) -> int:
        """The hard turn cap: the budget total plus the free turns (CTO_NOTES_12 §9.5). Without
        this the server's nine turns would end every panel early."""
        return self.agenda.total_budget + self.setup.free_turns

    def persona(self, member: str):
        return self.personas.get(member) or self.personas[self.chair_id]

    def persona_id(self, member: str) -> str:
        return member

    def speaker_name(self, member: str) -> str:
        return self.persona(member).name

    @property
    def fact_check_enabled(self) -> bool:
        return True  # the fact checker itself skips a run with no real cards (Ruling 36)

    def prompt_versions(self) -> dict[str, int]:
        return self.settings.panel_prompt_versions()

    def request_wrap_up(self) -> None:
        self.wrap_up_due = True

    def ritual_line(self) -> str:
        return line_for(RITUAL_LINES, self.mix)

    def ctx(self, ctx: StrategyContext) -> StrategyContext:
        """The strategist's view: `last_move` from here, and every claim of a segment the candidate
        asked to leave (or that says a refused term) is closed to a challenge for good."""
        closed = set(ctx.challenged_claim_ids)
        for claim in self.session.state.claim_ledger:
            if claim.turn_id in self._refused_turns or (
                self._refused and find_avoided(f"{claim.text} {claim.claim_en}", self._refused)
            ):
                closed.add(claim.id)
        return ctx.model_copy(
            update={
                "last_move": self.last_move,
                "challenged_claim_ids": sorted(closed),
                "panel": True,  # the fast graph's listener is then the panel listener (P5)
            }
        )

    # -- open -----------------------------------------------------------------

    async def open(self) -> tuple[LineResult, BoardMove, dict[str, Any]]:
        """The ritual, then the first seed of the Chairperson's own segment, asked once."""
        s = self.session
        first = self.agenda.segments[0]
        seed = first.seeds[0].text
        chair = self.chair_id
        tuning = self.settings
        opener = await generate_panel_opener(
            s.deps.llm, self.kit, s.form, s.deps.cards, seed,
            max_words=max(tuning.actor_max_words, 40), max_tokens=tuning.actor_max_tokens,
            turn_id="opener", first_token_budget_s=tuning.actor_first_token_budget_s,
        )
        self.director.memory = opening_memory(self.agenda, opener.text)
        parts = (
            PrefacePart(member=chair, text=self.ritual_line()),
            PrefacePart(member=chair, text=opener.text),
        )
        line = PanelLine(
            joined(parts), is_fallback=opener.is_fallback, guard=opener.guard,
            fallback_kind=opener.fallback_kind, error=opener.error, parts=parts,
        )
        move = BoardMove(
            move=Move.next_question, target=first.id, member=chair,
            pressure=PRESSURE[Move.next_question],
            reason="OPEN: the ritual, then the first seed of form_hr", rule_id="OPEN",
            for_turn="t0",
        )
        extra = {
            "mode": "upsc_interview",
            "panel": "on",
            "agenda": {
                "source": self.agenda.source,
                "max_answers": self.setup.max_answers,
                "segments": [
                    {"id": g.id, "member": g.member, "budget": g.budget, "seeds": len(g.seeds)}
                    for g in self.agenda.segments
                ],
            },
            "segment_id": first.id,
            "seed_id": f"{first.id}#0",
            "member": chair,
            "member_name": self.speaker_name(chair),
        }
        return line, move, extra

    # -- one turn -------------------------------------------------------------

    async def decide_turn(self, turn: OpenTurn, move: BoardMove, analysis: Analysis) -> BoardMove:
        """The last, instant step on top of the strategist's move: requests, budgets, seeds and
        hand-offs. Never raises (the director falls back to its pure function)."""
        started = time.monotonic()
        raw = " ".join(seg.text for seg in turn.segments).strip()
        self._raw = raw  # a reopened turn overwrites it; `apply` reads the last one
        dturn = DirectorTurn(
            ctx=turn.ctx, base_move=move, analysis=analysis, raw_text=raw,
            answers_used=self.answers_used, free_used=self.free_used,
            wrap_up_due=self.wrap_up_due,
        )
        decision = await self.director.decide_turn(dturn)
        self.decision = self._finish(decision)
        self._line = None
        self._director_ms = round((time.monotonic() - started) * 1000, 2)
        return self.decision.move

    def _refusal_terms(self, d: PanelDecision) -> tuple[str, ...]:
        """Terms this turn's topic change refuses: the candidate's, widened, plus the segment's own
        aliases, because "aage badhte hain" carries no term at all."""
        segment = self.agenda.segment(d.answered_segment)
        return expand_terms(d.avoid_terms, segment.aliases)

    def avoid_for(self, d: PanelDecision) -> tuple[str, ...]:
        extra = self._refusal_terms(d) if d.request_kind == "change_topic" else ()
        return tuple(dict.fromkeys((*self._refused, *extra)))

    def _finish(self, d: PanelDecision) -> PanelDecision:
        """Who speaks, and a seed clear of every refused term."""
        update: dict[str, Any] = {}
        move = d.move
        if (
            d.node in ("next_question", "handle_request")
            and not d.line_complete
            and move.member == self.chair_id
            and d.panel.member != self.chair_id
        ):
            update["move"] = move.model_copy(update={"member": d.panel.member})
        avoid = self.avoid_for(d)
        if d.seed and avoid and find_avoided(d.seed, avoid) is not None:
            seed = self._clear_seed(d, avoid)
            update["seed"] = seed
            update["panel"] = d.panel.model_copy(update={"seed": seed})
        return d.model_copy(update=update) if update else d

    def _clear_seed(self, d: PanelDecision, avoid: tuple[str, ...]) -> str:
        form = self.session.form
        index = d.seed_index if d.seed_index is not None else -1
        for candidate in seed_alternatives(d.seed_segment or d.panel.segment_id, index, form):
            if find_avoided(candidate, avoid) is None:
                return candidate
        return NEUTRAL_SEEDS.get(self.mix, NEUTRAL_SEEDS["hi-en"])

    async def speak(self, move: BoardMove, answer: str, turn: OpenTurn, tr: TurnTrace) -> PanelLine:
        d = self.decision
        if d is None or d.turn_id != turn.turn_id:
            raise RuntimeError("speak() before decide_turn() for this turn")
        s = self.session
        tuning = self.settings
        claim = card = None
        if move.move == Move.fact_challenge:
            claim = s._claim(move.target)
            card = s.deps.cards.get(claim.card_id) if claim else None
        tr.mark("actor_request")
        line = await compose_panel_line(
            s.deps.llm, self.kit, self.persona(move.member), move, d.panel, self.director.memory,
            form=s.form, preface=d.preface, ack_member=d.answered_member,
            repeat_text=d.repeat_text, line_complete=d.line_complete,
            after_handoff=d.node == "handoff", last_question=self.director.memory.last_question,
            last_answer=answer, avoid=self.avoid_for(d), max_words=tuning.actor_max_words,
            max_tokens=tuning.actor_max_tokens, turn_id=turn.turn_id, claim=claim, card=card,
            on_first_token=lambda: tr.mark("actor_first_token"),
            first_token_budget_s=tuning.actor_first_token_budget_s,
            filler_played=lambda: turn.filler_played,
            fast_throttle_retry_window_s=(
                tuning.actor_fast_throttle_window_ms / 1000
                if tuning.actor_fast_throttle_retry
                else None
            ),
        )
        tr.mark("actor_done")
        self._line = line
        return line

    def spoken_preface(self, move: BoardMove) -> str:
        """The fixed lines the voice loop says before the model's part, in order (`speak_stream`).
        Read before the model has answered, so it comes from the decision alone."""
        d = self.decision
        if d is None or move.for_turn != d.turn_id:
            return ""
        persona = self.persona(move.member)
        mix = language_mix(persona, self.session.form)
        return joined(fixed_parts(d.preface, move, mix, d.answered_member))

    # -- state ----------------------------------------------------------------

    def _asked(self, move: BoardMove, d: PanelDecision) -> str | None:
        """The question the board really put, for the director's memory. None for any fixed line,
        a clarify, or a fallback that is a pause rather than a question (CTO_NOTES_15 §9.5)."""
        line = self._line
        if d.line_complete or move.move == Move.clarify or line is None or not line.parts:
            return None
        if line.is_fallback and line.fallback_kind in ("persona_line", "challenge_template"):
            return None
        text = question_part(line.parts[-1].text)
        return text if text.endswith("?") else None

    def apply(self, move: BoardMove, analysis: Analysis) -> bool:
        """The state update after a move; True when the interview is over. The only place the
        position and the counters move, so a reopened turn re-decides on clean state."""
        d = self.decision
        if d is None or d.turn_id != move.for_turn:
            raise RuntimeError("apply() before decide_turn() for this turn")
        st = self.session.state
        st.moves.append(move)
        st.turn_index += 1
        if move.move == Move.fact_challenge and move.target:
            st.challenged_claim_ids.append(move.target)
        self.answers_used += int(d.counted)
        self.free_used += int(d.free)
        if move.move not in KEEPS_LAST_MOVE:
            self.last_move = move.move
        if d.request_kind == "change_topic":
            self._note_refusal(d)
        if d.counted:
            st.turns_in_topic += 1
            st.strong_streak = st.strong_streak + 1 if analysis.is_strong else 0
            st.honest_unknowns_in_topic += int(analysis.honest_unknown)
            st.pressure_in_topic += int(move.move == Move.pressure)
        if d.finished:
            st.finished = True
        elif d.node == "handoff":
            st.topic_index += 1
            st.turns_in_topic = 0
            st.strong_streak = 0
            st.honest_unknowns_in_topic = 0
            st.pressure_in_topic = 0
        self.director.queue_completed(
            CompletedTurn(analysis=analysis, decision=d, asked=self._asked(move, d), raw=self._raw)
        )
        self._row = {
            "answers_used": self.answers_used,
            "free_used": self.free_used,
            "turns_in_segment": st.turns_in_topic,
        }
        return st.finished

    def _note_refusal(self, d: PanelDecision) -> None:
        self._refused = list(dict.fromkeys((*self._refused, *self._refusal_terms(d))))
        self._refused_turns |= {
            t.turn_id for t in self.session.state.turns if t.topic == d.answered_segment
        }

    # -- trace ----------------------------------------------------------------

    def trace_fields(self, move: BoardMove, line: LineResult) -> dict[str, Any]:
        d = self.decision
        if d is None:
            return {}
        parts = list(getattr(line, "parts", ()) or ())
        memory = self.director.memory
        panel = {
            "agenda_source": self.agenda.source,
            "segment_id": d.panel.segment_id,
            "segment_index": self.agenda.topics.index(d.panel.segment_id),
            "answered_segment": d.answered_segment,
            "seed_id": f"{d.seed_segment}#{d.seed_index}" if d.seed_index is not None else None,
            "member": move.member,
            "member_name": self.speaker_name(move.member),
            "route": d.node,
            "rule": move.rule_id,
            "handoff_from": d.answered_member if d.node == "handoff" else None,
            "preface_speakers": [p.member for p in d.preface],
            "parts": [
                {"member": p.member, "name": self.speaker_name(p.member), "text": p.text}
                for p in parts
            ],
            "candidate_request": d.request_kind,
            "request_source": d.request_source,
            "request_pattern": d.request_pattern,
            "request_disagree": d.request_disagree,
            "avoid_terms": list(self.avoid_for(d)),
            "counted": d.counted,
            "free": d.free,
            "close_reason": d.close_reason,
            "max_answers": self.setup.max_answers,
            "memory_words": sum(len(n.gist.split()) for n in memory.notes),
            "director_ms": self._director_ms,
            "director_error": d.director_error,
            **self._row,
        }
        return {"mode": "upsc_interview", "panel": panel}

    # -- close and report -----------------------------------------------------

    async def aclose(self) -> None:
        """Fold the last turn and delete the checkpointer thread. Safe to call twice, and meant to
        run on every exit path, a disconnect included: the saver is shared, so an unfinished
        session would leak its thread for the life of the server (CTO_NOTES_15 §9.1)."""
        if not self._finished:
            self._finished = True
            await self.director.finish()

    def request_notes(self) -> list[str]:
        """Each detected request and how it was found, then the honest accuracy note (Ruling 51)."""
        notes = []
        for r in self.director.requests:
            how = _SOURCE_WORDS.get(r.source, r.source)
            pattern = f" ({r.pattern_id})" if r.pattern_id else ""
            notes.append(f"Request at {r.turn_id}: {r.kind}, {how}{pattern}.")
        if not notes:
            notes.append("No candidate request was detected in this session.")
        notes.append(REQUEST_ACCURACY_NOTE)
        return notes

    async def report(self, language: Any = None):
        s = self.session
        await self.aclose()
        report = await generate_report(
            s.deps,
            session_id=s.session_id,
            turns=s.state.turns,
            ledger=s.state.claim_ledger,
            moves=s.state.moves,
            language=language,
            callbacks=s.callbacks,
        )
        return report.model_copy(
            update={
                "requests": list(self.director.requests),
                "notes": [*report.notes, *self.request_notes()],
                "prompt_versions": self.prompt_versions(),
            }
        )
