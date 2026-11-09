"""Mock Board's MCP tools as plain async methods (CTO_NOTES_18 §2). No MCP package needed.

Each session is the real `InterviewSession` (UPSC panel or study partner) on a `ClientBrainLLM`:
the director, agenda, request rules, situations and twists, guards and code-built reports are the
same code the voice room runs. The client model supplies the listener's analysis and speaks the
lines; this module turns the board's decision into lines marked `verbatim` (fixed lines, said
word for word) or `own_words` (the model's part, with our draft, the move and the limits).

Errors the client can fix are `ToolError` (plain words; the transport sends them as a tool error,
not a protocol error). Nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mockboard.brain.agenda import is_situation
from mockboard.brain.deps import BrainDeps, build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.judge import assemble_report, check_citations
from mockboard.brain.likely_questions import template_questions
from mockboard.brain.panel_actor import SEED_LINE_WORDS
from mockboard.brain.schemas import (
    PRACTICE_INDICATORS,
    CandidateForm,
    Dimension,
    Move,
    ReportLanguage,
)
from mockboard.brain.tools import JudgeOutput, PartnerSummaryOutput
from mockboard.config import Settings
from mockboard.mcp import guidance
from mockboard.mcp.client_brain import EMPTY_CARDS, ClientBrainLLM, clean_analysis
from mockboard.mcp.safety import RefusedInput, check_form, check_topic, clean_speech
from mockboard.modes.partner.lines import CLOSE_TARGET, RECAP_TARGET
from mockboard.modes.partner.report import check_citations as check_partner_citations
from mockboard.modes.partner.session import LANGUAGES as PARTNER_LANGUAGES
from mockboard.modes.partner.session import PartnerSetup
from mockboard.modes.upsc_panel.setup import PanelSetup
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

UPSC = "upsc_interview"
PARTNER = "study_partner"
MAX_SESSIONS = 4
IDLE_S = 3 * 3600
MIN_ANSWERS, MAX_ANSWERS = 5, 20  # five members, one question each at least
MOVE_DEADLINE_S = 5.0  # our "listener" is instant; this only bounds a broken task
MAX_RATIONALE_CHARS = 300
FIXED_GUARDS = frozenset({"fixed_line", "clarify_template", "no_card", "partner_template"})

# The seven qualities UPSC publishes for the Personality Test, then Mock Board's two practice
# indicators (labelled as ours, never as UPSC criteria). Same rubric as the judge (Ruling 6).
RUBRIC_LABELS: dict[Dimension, str] = {
    Dimension.mental_alertness: "Mental alertness",
    Dimension.critical_assimilation: "Critical powers of assimilation",
    Dimension.clear_logical_exposition: "Clear and logical exposition",
    Dimension.balance_of_judgement: "Balance of judgement",
    Dimension.variety_depth_of_interest: "Variety and depth of interest",
    Dimension.social_cohesion_leadership: "Ability for social cohesion and leadership",
    Dimension.intellectual_moral_integrity: "Intellectual and moral integrity",
}

SCORING_STEPS = (
    "Score each of the nine dimensions 1-5, or 'insufficient_evidence' when the sitting did not "
    "show it (an honest gap beats an invented score). There is no accent, grammar or fluency "
    "score. Every numeric score needs at least one evidence item: a word-for-word quote from the "
    "candidate's own words in one turn, with that turn's id. Then call check_scores and show "
    "only what it keeps."
)


class ToolError(ValueError):
    """A problem the client can fix; the message is safe to show."""


def default_settings() -> Settings:
    """Settings without `.env` (no secret is needed here) and without the agenda plan."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(update={"agenda_plan_enabled": False})


@dataclass
class McpSession:
    id: str
    mode: str
    session: InterviewSession
    llm: ClientBrainLLM
    language: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = 0.0
    finished: bool = False
    report: Any = None  # SessionReport or PartnerReport, after end_session
    you_said: dict[str, str] = field(default_factory=dict)  # turn id -> the assistant's own line


class BoardService:
    """Every tool, with in-memory sessions. One per server process."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_sessions: int = MAX_SESSIONS,
        idle_s: float = IDLE_S,
    ) -> None:
        self.settings = settings or default_settings()
        self.clock = clock
        self.max_sessions = max_sessions
        self.idle_s = idle_s
        self.sessions: dict[str, McpSession] = {}
        self._base: BrainDeps | None = None

    # -- plumbing -------------------------------------------------------------

    def _deps(self, llm: ClientBrainLLM) -> BrainDeps:
        if self._base is None:  # personas, prompt kits and mode runtimes: built once
            self._base = build_deps(
                self.settings, ClientBrainLLM(), cards=EMPTY_CARDS, allow_synthetic_cards=False
            )
        return dataclasses.replace(self._base, llm=llm)

    async def _drop_idle(self) -> None:
        now = self.clock()
        for sid, ms in list(self.sessions.items()):
            if now - ms.last_used > self.idle_s and not ms.lock.locked():
                del self.sessions[sid]
                await ms.session.close()

    async def _new(self, mode: str, options: Any, form: CandidateForm | None,
                   language: str) -> McpSession:
        await self._drop_idle()
        if len(self.sessions) >= self.max_sessions:
            raise ToolError(
                f"at most {self.max_sessions} sittings at once: call end_session on one first"
            )
        sid = f"mcp-{secrets.token_hex(6)}"
        llm = ClientBrainLLM()
        deps = self._deps(llm)
        session = InterviewSession(
            deps=deps, form=form, session_id=sid,
            recorder=TraceRecorder(Path("."), sid, enabled=False),  # no file is ever written
            analyzer=TurnAnalyzer(deps), mode=mode, mode_options=options,
        )
        ms = McpSession(sid, mode, session, llm, language, last_used=self.clock())
        self.sessions[sid] = ms
        return ms

    def _get(self, session_id: object) -> McpSession:
        ms = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if ms is None:
            raise ToolError("no such session (it may have ended or been idle for 3 hours): "
                            "start a new one")
        ms.last_used = self.clock()
        return ms

    async def aclose(self) -> None:
        for ms in list(self.sessions.values()):
            await ms.session.close()
        self.sessions.clear()

    # -- tools: starting ------------------------------------------------------

    async def start_interview(self, form: object, max_answers: object = None) -> dict[str, Any]:
        try:
            checked = check_form(form)
        except RefusedInput as exc:
            raise ToolError(str(exc)) from None
        cap: int | None = None
        if max_answers is not None:
            if (not isinstance(max_answers, int) or isinstance(max_answers, bool)
                    or not MIN_ANSWERS <= max_answers <= MAX_ANSWERS):
                raise ToolError(f"max_answers: a whole number from {MIN_ANSWERS} to {MAX_ANSWERS}")
            cap = max_answers
        ms = await self._new(UPSC, PanelSetup(max_answers=cap, skip_judge=True), checked,
                             checked.interview_language)
        async with ms.lock:
            line = await ms.session.open()
            hooks = ms.session.hooks
            first = hooks.agenda.segments[0]
            parts = list(line.parts)
            say = [self._verbatim(ms, p.member, p.text) for p in parts[:-1]]
            say.append(self._own_words(
                ms, parts[-1].member, parts[-1].text, Move.next_question.value,
                question=first.seeds[0].text, max_words=max(self.settings.actor_max_words, 40),
                how_to=("The opening question. " + guidance.MOVE_GUIDE["next_question"]),
            ))
        return {
            "session_id": ms.id,
            "mode": UPSC,
            "language": guidance.LANGUAGE_NAMES[ms.language],
            "members": self._members(ms),
            "answers_budget": hooks.agenda.total_budget,
            "say": say,
            "finished": False,
            "next_step": _NEXT_STEP,
            "rules": list(guidance.HONESTY_RULES),
        }

    async def start_partner(self, topic: object, language: object = "hinglish") -> dict[str, Any]:
        try:
            clean_topic = check_topic(topic)
        except RefusedInput as exc:
            raise ToolError(str(exc)) from None
        if language not in PARTNER_LANGUAGES:
            raise ToolError(f"language: one of {', '.join(PARTNER_LANGUAGES)}")
        ms = await self._new(PARTNER, PartnerSetup(topic=clean_topic, language=language), None,
                             str(language))
        async with ms.lock:
            line = await ms.session.open()
        return {
            "session_id": ms.id,
            "mode": PARTNER,
            "language": guidance.LANGUAGE_NAMES[ms.language],
            "topic": clean_topic,
            "members": self._members(ms),
            "say": [self._verbatim(ms, "mitra", line.text)],
            "finished": False,
            "next_step": _NEXT_STEP,
            "rules": list(guidance.HONESTY_RULES),
        }

    # -- tools: one turn ------------------------------------------------------

    async def next_turn(
        self,
        session_id: object,
        candidate_said: object,
        analysis: object = None,
        you_said: object = None,
    ) -> dict[str, Any]:
        ms = self._get(session_id)
        try:
            text = clean_speech(candidate_said)
            said_by_you = clean_speech(you_said) if you_said is not None else ""
        except RefusedInput as exc:
            raise ToolError(str(exc)) from None
        if analysis is not None and not isinstance(analysis, Mapping):
            raise ToolError("analysis: an object with the listed fields, or leave it out")
        async with ms.lock:
            if ms.finished:
                raise ToolError("this sitting has finished: call end_session")
            s = ms.session
            ms.llm.analysis = clean_analysis(analysis)  # type: ignore[arg-type]
            try:
                if text:
                    s.submit_segment(text, segment_index=0)
                move = await s.end_of_turn(MOVE_DEADLINE_S)
                if said_by_you and s.turn is not None:  # the line this answer answered
                    ms.you_said[s.turn.turn_id] = said_by_you
                out = await s.speak()
            finally:
                ms.llm.analysis = {}
            ms.finished = out.finished
            if ms.mode == UPSC:
                say, board = self._upsc_turn(ms, move, out.line)
            else:
                say, board = self._partner_turn(ms, move, out.line)
        return {
            "session_id": ms.id,
            "turn_id": out.turn_id,
            "say": say,
            "board": board,
            "finished": out.finished,
            "next_step": _DONE_STEP if out.finished else _NEXT_STEP,
        }

    def _upsc_turn(self, ms: McpSession, move: Any, line: Any) -> tuple[list, dict]:
        hooks = ms.session.hooks
        d = hooks.decision
        parts = list(line.parts)
        fixed_only = d.line_complete or line.guard in FIXED_GUARDS or not parts
        n_fixed = len(parts) if fixed_only else len(hooks.spoken_preface_parts(move))
        say = [self._verbatim(ms, p.member, p.text) for p in parts[:n_fixed] if p.text]
        seeded = move.move in (Move.next_question, Move.move_on) and bool(d.seed)
        for p in parts[n_fixed:]:
            say.append(self._own_words(
                ms, p.member, p.text, move.move.value,
                question=d.seed if seeded else None,
                max_words=SEED_LINE_WORDS if seeded else self.settings.actor_max_words,
                how_to=guidance.MOVE_GUIDE.get(move.move.value, guidance.MOVE_GUIDE["probe"]),
                avoid=list(hooks.avoid_for(d)),
            ))
        board = {
            "move": move.move.value,
            "rule": move.rule_id,
            "why": move.reason,
            "request": d.request_kind,
            "counted_as_answer": d.counted,
            "answers_used": hooks.answers_used,
            "answers_budget": hooks.agenda.total_budget,
            "segment": d.panel.segment_title,
            "situation": bool(d.seed and is_situation(d.seed)),
            "twist": move.rule_id == "D-TWIST",
            "end_confirmation_pending": d.pending_end,  # the value after this turn
            "long_answer_expected": ms.session.expects_long_turn,
        }
        return say, board

    def _partner_turn(self, ms: McpSession, move: Any, line: Any) -> tuple[list, dict]:
        hooks = ms.session.hooks
        if line.guard in FIXED_GUARDS:
            say = [self._verbatim(ms, "mitra", line.text)]
        else:
            key = (f"{move.move.value}:{move.target}"
                   if move.target in (RECAP_TARGET, CLOSE_TARGET) else move.move.value)
            say = [self._own_words(
                ms, "mitra", line.text, key, question=None, max_words=40,
                how_to=guidance.PARTNER_MOVE_GUIDE.get(key, guidance.PARTNER_MOVE_GUIDE["probe"]),
            )]
        board = {
            "move": move.move.value,
            "target": move.target,
            "rule": move.rule_id,
            "why": move.reason,
            "points_turns_used": hooks.pstate.counted,
            "soft_cap": hooks.pstate.soft_cap,
            "long_answer_expected": True,
        }
        return say, board

    # -- tools: the end -------------------------------------------------------

    async def end_session(self, session_id: object) -> dict[str, Any]:
        ms = self._get(session_id)
        async with ms.lock:
            if ms.report is None:
                ms.report = await ms.session.finish()
                ms.finished = True
                await ms.session.close()
            return self._report_view(ms)

    def _turn_rows(self, ms: McpSession) -> list[dict[str, Any]]:
        rows = []
        for t in ms.session.state.turns:
            row = {"turn_id": t.turn_id, "board_asked": t.question, "candidate_said": t.transcript}
            asked_by_you = ms.you_said.get(t.turn_id)
            if asked_by_you:
                row["you_said"] = asked_by_you
            rows.append(row)
        return rows

    def _report_view(self, ms: McpSession) -> dict[str, Any]:
        rep = ms.report
        if ms.mode == UPSC:
            body = rep.model_dump(mode="json", include={
                "by_member", "weakest", "practice_plan", "discussion", "heard", "requests"})
            notes = [n for n in rep.notes if not n.startswith("judge unavailable")]
            notes.insert(0, "Mock Board writes no scores in this channel. To score, use the rubric "
                         "below and call check_scores.")
            return {
                "session_id": ms.id, "mode": UPSC, "report": body, "notes": notes,
                "turns": self._turn_rows(ms), "rubric": self.rubric(),
                "how_to_score": SCORING_STEPS, "next_step": "Show the report. To add scores, "
                "draft them and call check_scores.",
            }
        body = rep.model_dump(mode="json", include={
            "title", "topic", "points", "next_steps", "heard"})
        notes = [n for n in rep.notes if not n.startswith("The notes were written by code")]
        notes.insert(0, "These notes were drafted by Mock Board's code from the speaker's own "
                     "words. To refine them, draft notes and call check_notes.")
        return {
            "session_id": ms.id, "mode": PARTNER, "report": body, "notes": notes,
            "turns": self._turn_rows(ms),
            "how_to_write_notes": (
                "Each point, reason, example and answer must be the speaker's own words, quoted "
                "exactly; a counterpoint must be words Mitra (you) said. No scores, no figures "
                "the speaker did not say. Call check_notes and show only what it keeps."
            ),
            "next_step": "Show the notes. To refine them, call check_notes.",
        }

    def _ended(self, session_id: object, mode: str) -> McpSession:
        ms = self._get(session_id)
        if ms.report is None:
            raise ToolError("call end_session first")
        if ms.mode != mode:
            other = "check_scores" if ms.mode == UPSC else "check_notes"
            raise ToolError(f"this is a {ms.mode} session: use {other}")
        return ms

    async def check_scores(
        self, session_id: object, scores: object, evidence: object = None,
        composure_moments: object = None,
    ) -> dict[str, Any]:
        ms = self._ended(session_id, UPSC)
        draft_raw = {
            "scores": _capped_rationales(scores),
            "evidence": evidence if evidence is not None else [],
            "composure_moments": composure_moments if composure_moments is not None else [],
            "model_answer": "",  # a model answer could carry facts of the client's own: not taken
        }
        try:
            draft = JudgeOutput.model_validate(draft_raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'draft'}: {e['msg']}"
                for e in exc.errors()[:6]
            )
            raise ToolError(
                f"the draft was not accepted, nothing was checked: {problems}"
            ) from None
        turns = ms.session.state.turns
        failed = check_citations(draft.evidence, turns)
        s = ms.session
        checked = assemble_report(
            draft, session_id=ms.id, turns=turns, ledger=s.state.claim_ledger,
            moves=s.state.moves, cards=EMPTY_CARDS, language=ReportLanguage.en, revisions=0,
            feature_flags={}, prompt_versions={},
        )
        return {
            "session_id": ms.id,
            "quote_check_passed": not failed,
            "dropped_quotes": [
                {"dimension": e.dimension.value, "turn_id": e.turn_id, "quote": e.quote}
                for e in failed
            ],
            "scores": [
                {
                    "dimension": sc.dimension.value,
                    "label": _label(sc.dimension),
                    "score": sc.score,
                    "rationale": sc.rationale,
                    "evidence": [ev.model_dump() for ev in sc.evidence],
                    "mock_board_practice_indicator": sc.is_practice_indicator,
                }
                for sc in checked.scores
            ],
            "composure_moments": [m.model_dump() for m in checked.composure_moments],
            "notes": checked.notes,
            "next_step": (
                "Show only these scores. A dropped quote was not found word for word in that "
                "turn; you may fix it and call check_scores again."
            ),
        }

    async def check_notes(
        self, session_id: object, points: object, next_steps: object = None
    ) -> dict[str, Any]:
        ms = self._ended(session_id, PARTNER)
        draft = PartnerSummaryOutput.model_validate(
            {"points": points if isinstance(points, list) else [],
             "next_steps": [x for x in next_steps if isinstance(x, str)]
             if isinstance(next_steps, list) else []}
        )
        s = ms.session
        speaker_texts = [t.transcript for t in s.state.turns if t.transcript]
        partner_texts = [b.text for b in s.state.board_lines] + list(ms.you_said.values())
        kept, steps, problems = check_partner_citations(
            draft, speaker_texts, partner_texts, ms.report.points
        )
        return {
            "session_id": ms.id,
            "quote_check_passed": not problems,
            "points": [p.model_dump(exclude={"turn_ids"}) for p in kept],
            "next_steps": steps,
            "problems": problems,
            "next_step": "Show only these notes. If no point was kept, show the code notes from "
            "end_session instead.",
        }

    # -- tools: before a sitting ----------------------------------------------

    def likely_questions(self, form: object) -> dict[str, Any]:
        try:
            checked = check_form(form)
        except RefusedInput as exc:
            raise ToolError(str(exc)) from None
        names = self._names(UPSC)
        questions = template_questions(checked, names)
        return {
            "questions": [
                {"member": q.member, "text": q.text, "hint": q.hint} for q in questions
            ],
            "note": "Template questions from the form alone; no model wrote them. The real "
            "sitting picks some of these and follows your answers.",
        }

    # -- helpers ----------------------------------------------------------------

    def _names(self, mode: str) -> dict[str, str]:
        rt = self._deps(ClientBrainLLM()).mode(mode)  # personas only; no session is made
        return {p.id: p.name for p in rt.personas.values()}

    def _members(self, ms: McpSession) -> list[dict[str, str]]:
        return _persona_rows(ms.session.deps, ms.mode)

    def board_members(self) -> dict[str, Any]:
        """Every persona of both modes, for the members resource. All fictional."""
        deps = self._deps(ClientBrainLLM())
        return {
            UPSC: _persona_rows(deps, UPSC),
            PARTNER: _persona_rows(deps, PARTNER),
            "note": "Every member is fictional and not modelled on any real person.",
        }

    def _verbatim(self, ms: McpSession, member: str, text: str) -> dict[str, Any]:
        return {"speaker": ms.session.speaker_name(member), "member": member, "how": "verbatim",
                "text": text}

    def _own_words(
        self, ms: McpSession, member: str, draft: str, move: str, *, question: str | None,
        max_words: int, how_to: str, avoid: Sequence[str] = (),
    ) -> dict[str, Any]:
        line: dict[str, Any] = {
            "speaker": ms.session.speaker_name(member), "member": member, "how": "own_words",
            "move": move, "draft": draft, "max_words": max_words,
            "language": guidance_language(ms.language), "guidance": how_to,
        }
        if ms.language != "hinglish":  # the stand-in drafts are mostly Hinglish (`--fake`'s)
            line["draft_note"] = (
                f"The draft is Mock Board's rough wording and may be in Hinglish: say the line in "
                f"{guidance_language(ms.language)}."
            )
        if question:
            line["question"] = question
        if avoid:
            line["avoid"] = list(avoid)
        return line

    @staticmethod
    def rubric() -> list[dict[str, Any]]:
        return [
            {"dimension": d.value, "label": _label(d),
             "mock_board_practice_indicator": d in PRACTICE_INDICATORS}
            for d in Dimension
        ]


_NEXT_STEP = ("Say the lines in `say` in order. Then wait for the person's answer and call "
              "next_turn with their exact words, your analysis, and what you actually said.")
_DONE_STEP = "Say the lines in `say`. The sitting has finished: call end_session."


def _persona_rows(deps: BrainDeps, mode: str) -> list[dict[str, str]]:
    return [
        {"id": p.id, "name": p.name, "title": p.title,
         "domain": getattr(p, "domain", "") or "",
         "manner": " ".join((getattr(p, "style", "") or "").split())}
        for p in deps.mode(mode).personas.values()
    ]


def guidance_language(language: str) -> str:
    return guidance.LANGUAGE_NAMES.get(language, language)


def _label(d: Dimension) -> str:
    if d in RUBRIC_LABELS:
        return RUBRIC_LABELS[d]
    return d.value.replace("_", " ").capitalize() + " (Mock Board practice indicator)"


def _capped_rationales(scores: object) -> object:
    if not isinstance(scores, list):
        return scores
    out = []
    for s in scores:
        if isinstance(s, Mapping) and isinstance(s.get("rationale"), str):
            s = {**s, "rationale": " ".join(s["rationale"].split())[:MAX_RATIONALE_CHARS]}
        out.append(s)
    return out
