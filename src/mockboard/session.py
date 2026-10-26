"""Text-mode session controller: one interview from the opener to the report.

Order of one turn (text mode):
1. The board's question is shown (trace stage `question_shown`).
2. The candidate's answer arrives (`end_of_turn`). It is split into segments the way Prisma's
   server VAD would split speech, and each segment is submitted to the turn-analysis graph as
   a background task (serialised per session inside `TurnAnalyzer`).
3. The controller waits for this turn's analyses (bounded by `analysis_wait_timeout_s`) and
   takes the strategist's precomputed move. A result for another turn is ignored (stale guard).
4. The actor phrases the move (a plain streaming call, not a graph node; guards in code).
5. Counters (topic, streak, challenged claims) are updated deterministically and one trace
   line is written.

The voice path (phase 2) will reuse this class: Prisma segments go to `submit_segment` as they
arrive, so analysis overlaps with speech instead of starting at end of turn.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from mockboard.brain.actor import LineResult, compose_line, generate_opener
from mockboard.brain.deps import BACKGROUND_TOPIC, BrainDeps
from mockboard.brain.graphs import (
    AnalysisResult,
    FactChecker,
    FactCheckResult,
    TurnAnalyzer,
    generate_report,
    needs_fact_check,
    upsert_claims,
)
from mockboard.brain.schemas import (
    Analysis,
    BoardLine,
    BoardMove,
    CandidateForm,
    CandidateTurn,
    Claim,
    Move,
    ReportLanguage,
    Segment,
    SessionReport,
    SessionState,
    StrategyContext,
    VoiceSignals,
)
from mockboard.brain.strategist import CLOSE_TARGET, PRESSURE, merge_analyses
from mockboard.obs.trace import TraceRecorder

SEGMENT_MARK = " | "  # typing " | " in an answer forces a segment boundary
SENTENCES_PER_SEGMENT = 2


def split_segments(text: str, turn_id: str) -> list[Segment]:
    """Split a typed answer into transcript segments (stand-in for Prisma's server VAD)."""
    if SEGMENT_MARK in text:
        parts = [p.strip() for p in text.split(SEGMENT_MARK)]
    else:
        sentences = [s.strip() for s in re.split(r"(?<=[.?!।])\s+", text.strip()) if s.strip()]
        parts = [
            " ".join(sentences[i : i + SENTENCES_PER_SEGMENT])
            for i in range(0, len(sentences), SENTENCES_PER_SEGMENT)
        ]
    parts = [p for p in parts if p] or [""]
    return [Segment(segment_id=f"s{i}", segment_index=i, text=p) for i, p in enumerate(parts)]


@dataclass
class TurnOutcome:
    turn_id: str
    move: BoardMove
    line: LineResult
    results: list[AnalysisResult]
    trace: dict[str, Any]
    finished: bool


@dataclass
class InterviewSession:
    deps: BrainDeps
    form: CandidateForm
    recorder: TraceRecorder
    analyzer: TurnAnalyzer
    session_id: str = field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:10]}")
    max_turns: int = 9
    callbacks: Sequence[Any] = ()
    fact_checker: FactChecker | None = None
    state: SessionState = field(init=False)
    last_question: str = ""
    # Background fact checks (never awaited by a move) and the ones finished since the last
    # trace line was written.
    _fact_checks: set[asyncio.Task] = field(init=False, default_factory=set)
    _fact_checks_done: list[FactCheckResult] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if self.fact_checker is None:
            self.fact_checker = FactChecker(self.deps, callbacks=self.callbacks)
        self.state = SessionState(
            session_id=self.session_id,
            form=self.form,
            topics=[BACKGROUND_TOPIC, *self.deps.settings.topics],
        )

    # -- helpers -------------------------------------------------------------

    def _max_turns_in(self, topic: str) -> int:
        return 1 if topic == BACKGROUND_TOPIC else self.deps.settings.max_turns_per_topic

    def _next_topic(self) -> str | None:
        i = self.state.topic_index + 1
        return self.state.topics[i] if i < len(self.state.topics) else None

    def _ctx(self, turn_id: str, signals: VoiceSignals) -> StrategyContext:
        s = self.state
        return StrategyContext(
            turn_id=turn_id,
            turn_index=s.turn_index,
            topic=s.topic,
            next_topic=self._next_topic(),
            turns_in_topic=s.turns_in_topic,
            max_turns_in_topic=self._max_turns_in(s.topic),
            strong_streak=s.strong_streak,
            challenged_claim_ids=list(s.challenged_claim_ids),
            last_move=s.moves[-1].move if s.moves else None,
            honest_unknowns_in_topic=s.honest_unknowns_in_topic,
            pressure_in_topic=s.pressure_in_topic,
            voice_signals=signals,
        )

    def _start_fact_check(self, analysis_task: asyncio.Task[AnalysisResult]) -> None:
        """Done-callback of a fast analysis: start the background fact check for its claims.

        The task is kept in `_fact_checks` but no move ever awaits it; its gated verdicts are
        upserted into the ledger when it ends, and the strategist sees them on a later turn.
        """
        if analysis_task.cancelled() or analysis_task.exception() is not None:
            return
        result = analysis_task.result()
        if not needs_fact_check(result.analysis):
            return
        task = asyncio.create_task(
            self.fact_checker.check(
                result.analysis.claims, turn_id=result.turn_id, segment_id=result.segment_id
            )
        )
        self._fact_checks.add(task)
        task.add_done_callback(self._fact_check_done)

    def _fact_check_done(self, task: asyncio.Task[FactCheckResult]) -> None:
        self._fact_checks.discard(task)
        if task.cancelled() or task.exception() is not None:  # check() itself never raises
            return
        res = task.result()
        self.state.claim_ledger = upsert_claims(self.state.claim_ledger, res.claims)
        self._fact_checks_done.append(res)

    def _fact_check_trace(self) -> list[dict[str, Any]]:
        """Fact checks finished since the last trace line, with their own llm calls."""
        done, self._fact_checks_done = self._fact_checks_done, []
        return [
            {
                "for_turn": r.turn_id,
                "segment_id": r.segment_id,
                "fact_check_ms": r.fact_check_ms,
                "claims": [
                    {"id": c.id, "verdict": c.verdict.value, "card_id": c.card_id}
                    for c in r.claims
                ],
                "fallbacks": r.fallbacks,
                "llm_calls": self.recorder.pop_calls(r.trace_tag),
            }
            for r in done
        ]

    async def drain_fact_checks(self, timeout_s: float) -> int:
        """Wait (bounded) for background fact checks; cancel the rest. Returns how many were
        cancelled. Used before the report, never on the move's path."""
        pending = set(self._fact_checks)
        if not pending:
            return 0
        _, still = await asyncio.wait(pending, timeout=timeout_s)
        for task in still:
            task.cancel()
        return len(still)

    def _claim(self, claim_id: str | None) -> Claim | None:
        return next((c for c in self.state.claim_ledger if c.id == claim_id), None)

    # -- public API ----------------------------------------------------------

    async def open(self) -> LineResult:
        s = self.deps.settings
        line = await generate_opener(
            self.deps.llm,
            self.deps.kit,
            self.form,
            self.deps.cards,
            max_words=max(s.actor_max_words, 40),
            max_tokens=s.actor_max_tokens,
            turn_id="opener",
        )
        self.last_question = line.text
        self.state.board_lines.append(
            BoardLine(
                turn_id="t0",
                member=self.deps.strategy.chairman,
                move=Move.probe,
                text=line.text,
                is_fallback=line.is_fallback,
            )
        )
        self.recorder.write_event(
            "session_start",
            tag="opener",
            opener_guard=line.guard,
            opener_fallback=line.is_fallback,
            card_set_version=self.deps.cards.version,
            cards_all_synthetic=self.deps.cards.all_synthetic,
            feature_flags=s.feature_flags(),
            prompt_versions=s.prompt_versions(),
        )
        return line

    async def answer(self, text: str, signals: VoiceSignals | None = None) -> TurnOutcome:
        st, s = self.state, self.deps.settings
        signals = signals or VoiceSignals()
        turn_id = f"t{st.turn_index}"
        tr = self.recorder.start_turn(turn_id, st.turn_index, st.topic)
        tr.mark("question_shown", tr.t0)
        tr.mark("end_of_turn")
        ctx = self._ctx(turn_id, signals)
        segments = split_segments(text, turn_id)
        tasks = []
        for segment in segments:
            task = self.analyzer.submit(
                session_id=self.session_id,
                turn_id=turn_id,
                turn_index=st.turn_index,
                question=self.last_question,
                segment=segment,
                ctx=ctx,
                ledger=lambda: self.state.claim_ledger,
            )
            task.add_done_callback(self._start_fact_check)
            tasks.append(task)
            tr.mark(f"segment_{segment.segment_index}_submitted")

        results: list[AnalysisResult] = []
        fallbacks: list[dict[str, str]] = []
        try:
            done = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=s.analysis_wait_timeout_s
            )
        except TimeoutError:
            done = []
            fallbacks.append({"stage": "analysis", "turn_id": turn_id, "error": "timeout"})
        for item in done:
            if isinstance(item, AnalysisResult):
                results.append(item)
            else:
                fallbacks.append(
                    {
                        "stage": "analysis",
                        "turn_id": turn_id,
                        "error": f"{type(item).__name__}: {item}"[:300],
                    }
                )
        tr.mark("analysis_done")
        for r in results:
            fallbacks.extend(r.fallbacks)
            for t in r.timings:
                tr.mark(f"{r.segment_id}.{t['stage']}", t["t"])

        # The strategist's latest move for THIS turn (stale results are ignored).
        fresh = [r for r in results if r.move.for_turn == turn_id]
        if fresh:
            move = fresh[-1].move
        else:
            move = BoardMove(
                move=Move.probe,
                member=self.deps.strategy.member_for(st.topic),
                pressure=PRESSURE[Move.probe],
                reason="FALLBACK: no analysis",
                rule_id="FALLBACK",
                for_turn=turn_id,
            )
        if st.turn_index + 1 >= self.max_turns and not (
            move.move == Move.move_on and move.target == CLOSE_TARGET
        ):
            move = BoardMove(
                move=Move.move_on,
                target=CLOSE_TARGET,
                member=self.deps.strategy.chairman,
                pressure=PRESSURE[Move.move_on],
                reason=f"CAP: session turn cap ({self.max_turns}) reached",
                rule_id="CAP",
                for_turn=turn_id,
                preface=move.preface,  # a conceded error is still acknowledged at the close
            )
        tr.mark("move_ready")

        analysis = (
            merge_analyses([r.analysis for r in fresh])
            if fresh
            else Analysis(turn_id=turn_id, is_fallback=True)
        )
        st.turns.append(
            CandidateTurn(
                turn_id=turn_id,
                turn_index=st.turn_index,
                topic=st.topic,
                question=self.last_question,
                segments=segments,
                voice_signals=signals,
                analysis=analysis,
            )
        )

        line = await self._speak(move, text, turn_id, tr)
        finished = self._apply(move, analysis)
        self.last_question = line.text
        st.board_lines.append(
            BoardLine(
                turn_id=turn_id,
                member=move.member,
                move=move.move,
                text=line.text,
                is_fallback=line.is_fallback,
            )
        )
        tr.set(
            move=move.move.value,
            rule_id=move.rule_id,
            move_reason=move.reason,
            member=move.member,
            target=move.target,
            board_line=line.text,
            actor_guard=line.guard,
            actor_fallback=line.is_fallback,
            librarian_skipped=not any(needs_fact_check(r.analysis) for r in fresh),
            segments=len(segments),
            # This turn's claims; "pending" while their background fact check runs.
            claims=[
                {
                    "id": c.id,
                    "verdict": c.verdict.value,
                    "card_id": c.card_id,
                    "numeric_check": c.numeric_check,
                }
                for c in (self._claim(x.id) or x for x in analysis.claims)
            ],
            fact_checks=self._fact_check_trace(),
            fallbacks=fallbacks,
            prompt_versions=s.prompt_versions(),
        )
        trace = self.recorder.write_turn(tr)
        return TurnOutcome(turn_id, move, line, results, trace, finished)

    async def _speak(self, move: BoardMove, answer: str, turn_id: str, tr) -> LineResult:
        s = self.deps.settings
        claim = card = None
        if move.move == Move.fact_challenge:
            claim = self._claim(move.target)
            card = self.deps.cards.get(claim.card_id) if claim else None
        topic = (
            move.target
            if move.move == Move.move_on and move.target != CLOSE_TARGET
            else (self.state.topic)
        )
        persona = (
            self.deps.personas.get(move.member) or self.deps.personas[self.deps.strategy.chairman]
        )
        tr.mark("actor_request")
        line = await compose_line(
            self.deps.llm,
            self.deps.kit,
            persona,
            move,
            topic=topic or self.state.topic,
            form=self.form,
            last_question=self.last_question,
            last_answer=answer,
            max_words=s.actor_max_words,
            max_tokens=s.actor_max_tokens,
            turn_id=turn_id,
            claim=claim,
            card=card,
            on_first_token=lambda: tr.mark("actor_first_token"),
        )
        tr.mark("actor_done")
        return line

    def _apply(self, move: BoardMove, analysis: Analysis) -> bool:
        """Deterministic counter updates after a move. Returns True when the session ends."""
        st = self.state
        st.moves.append(move)
        st.turn_index += 1
        if move.move == Move.fact_challenge and move.target:
            st.challenged_claim_ids.append(move.target)
        if move.move == Move.move_on:
            st.strong_streak = 0
            st.turns_in_topic = 0
            st.honest_unknowns_in_topic = 0
            st.pressure_in_topic = 0
            if move.target == CLOSE_TARGET or self._next_topic() is None:
                st.finished = True
            else:
                st.topic_index += 1
        else:
            st.turns_in_topic += 1
            st.strong_streak = st.strong_streak + 1 if analysis.is_strong else 0
            st.honest_unknowns_in_topic += int(analysis.honest_unknown)
            st.pressure_in_topic += int(move.move == Move.pressure)
        return st.finished

    async def finish(self, language: ReportLanguage | str | None = None) -> SessionReport:
        t = time.monotonic()
        cancelled = await self.drain_fact_checks(self.deps.settings.fact_check_drain_timeout_s)
        report = await generate_report(
            self.deps,
            session_id=self.session_id,
            turns=self.state.turns,
            ledger=self.state.claim_ledger,
            moves=self.state.moves,
            language=language,
            callbacks=self.callbacks,
        )
        self.recorder.write_event(
            "report",
            tag=self.session_id,
            report_ms=round((time.monotonic() - t) * 1000, 1),
            is_fallback=report.is_fallback,
            citation_check_passed=report.citation_check_passed,
            judge_revisions=report.judge_revisions,
            report_language=report.report_language.value,
            fact_checks=self._fact_check_trace(),
            fact_checks_cancelled=cancelled,
        )
        await self.analyzer.close_session(self.session_id)
        return report
