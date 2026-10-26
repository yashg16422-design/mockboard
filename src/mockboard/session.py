"""Session controller: one interview from the opener to the report (text mode now; the
Pipecat voice path calls the same three methods).

One candidate turn (CTO_NOTES_02 M4, orchestrator ruling 9):

1. `submit_segment(text, segment_index=k)` for each final transcript segment, as it arrives
   (Prisma sends one per server-VAD segment; the CLI sends one per typed answer). The first
   segment opens the turn: the session freezes a `StrategyContext` and a snapshot of its
   claim ledger for this turn, then queues the segment's fast analysis (listener ->
   strategist) on the session's thread. Runs are queued in segment order. When a run ends
   with claims, a fact check (librarian fan-out -> gate) starts in the background.
2. `end_of_turn(deadline_s)` waits for this turn's queued fast analyses, at most
   `deadline_s`, and returns the board's move:
   * all analyses done: the strategist's move after the last segment;
   * deadline hit, some segments done: the move computed after the latest finished one;
   * deadline hit, none done: a deterministic fallback (silence -> reassure, else probe).
   Every deadline fallback is recorded as `fallback_reason` in the turn's trace line.
   At the deadline the turn's unfinished analyses are abandoned (ruling 13): each waiting
   listener node gives up at once and parks its Bedrock call, so it cannot hold up the next
   turn's segments; a listener call that hangs also gives up on its own after
   `fast_listener_timeout_s`, so it cannot hold up the same turn's later segments either.
   A parked call that returns later still has its claims fact-checked (`late_listener` in
   the trace); its move is never used.
   No segments at all means silence: the strategist's rules are run directly (pure, no
   LLM), which reassures.
3. `speak()` has the actor phrase the move (a plain streaming call, not a graph node; guards
   in code), updates the counters deterministically and writes one trace line.

Fact checks are never awaited by a move. Their gated verdicts are upserted into the ledger
when they end; the strategist sees them through the ledger snapshot of the NEXT turn that
opens after they land (R4 also requires `fact_challenge_min_delay_turns >= 1`). The snapshot
is frozen per turn, so every segment of a turn sees the same ledger and a verdict that lands
mid-turn waits for the next turn: the move never depends on how fast a fact check was.
"""

from __future__ import annotations

import asyncio
import functools
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
from mockboard.brain.strategist import CLOSE_TARGET, PRESSURE, decide_move, merge_analyses
from mockboard.obs.trace import TraceRecorder, TurnTrace

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


def segment_id_for(segment_index: int) -> str:
    return f"s{segment_index}"


@dataclass
class TurnOutcome:
    turn_id: str
    move: BoardMove
    line: LineResult
    results: list[AnalysisResult]
    trace: dict[str, Any]
    finished: bool
    fallback_reason: str | None = None


@dataclass
class OpenTurn:
    """The candidate turn in progress: from its first segment until `speak()` ends it."""

    turn_id: str
    trace: TurnTrace
    signals: VoiceSignals
    ctx: StrategyContext
    ledger: list[Claim]  # snapshot frozen when the turn opened
    segments: list[Segment] = field(default_factory=list)
    tasks: list[asyncio.Task[AnalysisResult]] = field(default_factory=list)
    # Set by end_of_turn.
    move: BoardMove | None = None
    results: list[AnalysisResult] = field(default_factory=list)
    fallbacks: list[dict[str, str]] = field(default_factory=list)
    fallback_reason: str | None = None
    deadline_s: float | None = None
    move_wait_ms: float = 0.0
    analysis: Analysis | None = None

    @property
    def ended(self) -> bool:
        return self.move is not None


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
    turn: OpenTurn | None = field(init=False, default=None)
    # Background fact checks (never awaited by a move) and the ones finished since the last
    # trace line was written.
    _fact_checks: set[asyncio.Task] = field(init=False, default_factory=set)
    _fact_checks_done: list[FactCheckResult] = field(init=False, default_factory=list)
    # Listener calls abandoned for the move but still running (ruling 13), and what became
    # of the ones that ended since the last trace line.
    _late_listens: set[asyncio.Task] = field(init=False, default_factory=set)
    _late_done: list[dict[str, Any]] = field(init=False, default_factory=list)

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

    def _open_turn(self, signals: VoiceSignals | None) -> OpenTurn:
        st = self.state
        if st.finished:
            raise RuntimeError("the interview has finished")
        turn_id = f"t{st.turn_index}"
        tr = self.recorder.start_turn(turn_id, st.turn_index, st.topic)
        tr.mark("question_shown", tr.t0)
        sig = signals or VoiceSignals()
        self.turn = OpenTurn(
            turn_id=turn_id,
            trace=tr,
            signals=sig,
            ctx=self._ctx(turn_id, sig),
            ledger=list(st.claim_ledger),
        )
        return self.turn

    def _start_fact_check(self, analysis_task: asyncio.Task[AnalysisResult]) -> None:
        """Done-callback of a fast analysis: start the background fact check for its claims.

        The task is kept in `_fact_checks` but no move ever awaits it; its gated verdicts are
        upserted into the ledger when it ends, and the strategist sees them on a later turn.
        """
        if analysis_task.cancelled() or analysis_task.exception() is not None:
            return
        result = analysis_task.result()
        if result.late_analysis is not None:
            late = result.late_analysis
            self._late_listens.add(late)
            late.add_done_callback(functools.partial(self._late_listen_done, result))
        self._fact_check(result.analysis, result.turn_id, result.segment_id)

    def _fact_check(self, analysis: Analysis, turn_id: str, segment_id: str) -> None:
        if not needs_fact_check(analysis):
            return
        task = asyncio.create_task(
            self.fact_checker.check(analysis.claims, turn_id=turn_id, segment_id=segment_id)
        )
        self._fact_checks.add(task)
        task.add_done_callback(self._fact_check_done)

    def _late_listen_done(self, result: AnalysisResult, task: asyncio.Task[Analysis]) -> None:
        """An abandoned listener call ended: fact-check its claims if it returned any."""
        self._late_listens.discard(task)
        entry: dict[str, Any] = {"for_turn": result.turn_id, "segment_id": result.segment_id}
        if task.cancelled():
            entry["outcome"] = "cancelled"
        elif task.exception() is not None:
            exc = task.exception()
            entry["outcome"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        else:
            analysis = task.result()
            entry["outcome"] = "returned"
            entry["claims"] = len(analysis.claims)
            entry["fact_checked"] = needs_fact_check(analysis)
            self._fact_check(analysis, result.turn_id, result.segment_id)
        if self.turn is None or self.turn.turn_id != result.turn_id:
            # The turn's line is already written; keep the late call's stats with this entry.
            entry["llm_calls"] = self.recorder.pop_calls(result.turn_id)
        self._late_done.append(entry)

    def _late_trace(self) -> list[dict[str, Any]]:
        done, self._late_done = self._late_done, []
        return done

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

    @property
    def pending_fact_checks(self) -> int:
        return len(self._fact_checks)

    @property
    def pending_late_listens(self) -> int:
        return len(self._late_listens)

    async def _wait_background(self, timeout_s: float) -> set[asyncio.Task]:
        """Wait (bounded) for fact checks and late listener calls, including fact checks that
        late calls start while we wait. Returns whatever is still running."""
        deadline = time.monotonic() + timeout_s
        while pending := self._fact_checks | self._late_listens:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.wait(pending, timeout=remaining)
        return self._fact_checks | self._late_listens

    async def settle_fact_checks(self, timeout_s: float) -> bool:
        """Wait (bounded) for background fact checks without cancelling any. Returns True
        when none is left. Only for scripted text runs, where the "candidate" answers with zero
        thinking time; never called between end of turn and the actor."""
        return not await self._wait_background(timeout_s)

    async def drain_fact_checks(self, timeout_s: float) -> int:
        """Wait (bounded) for background fact checks; cancel the rest. Returns how many were
        cancelled. Used before the report, never on the move's path."""
        still = await self._wait_background(timeout_s)
        for task in still:
            task.cancel()
        return len(still)

    def _claim(self, claim_id: str | None) -> Claim | None:
        return next((c for c in self.state.claim_ledger if c.id == claim_id), None)

    def _fallback_move(self, turn: OpenTurn, reason: str) -> BoardMove:
        """Deterministic fast rule when no analysis of this turn finished in time
        (CTO_NOTES_02 M4.4): nothing said -> reassure (chairman), otherwise a neutral probe."""
        cfg = self.deps.strategy
        if not any(s.text.strip() for s in turn.segments):
            move, member = Move.reassure, cfg.chairman
        else:
            move, member = Move.probe, cfg.member_for(turn.ctx.topic)
        return BoardMove(
            move=move,
            member=member,
            pressure=PRESSURE[move],
            reason=f"FALLBACK: {reason}",
            rule_id="FALLBACK",
            for_turn=turn.turn_id,
        )

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

    def submit_segment(
        self,
        text: str,
        *,
        segment_index: int,
        signals: VoiceSignals | None = None,
        audio_duration_ms: int | None = None,
    ) -> asyncio.Task[AnalysisResult]:
        """Queue one final transcript segment of the current turn. Never blocks.

        The first segment opens the turn. `segment_index` must increase strictly within a
        turn (Prisma's order); a duplicate or out-of-order index raises `ValueError`.
        `signals`, when given, replace the turn's voice signals for this and later segments.
        Returns the queued fast-analysis task (callers normally ignore it).
        """
        turn = self.turn or self._open_turn(signals)
        if turn.ended:
            raise RuntimeError(f"turn {turn.turn_id} already ended; call speak() first")
        if turn.segments and segment_index <= turn.segments[-1].segment_index:
            raise ValueError(
                f"segment_index {segment_index} after {turn.segments[-1].segment_index}: "
                "segments must arrive in increasing order"
            )
        if signals is not None and signals != turn.signals:
            turn.signals = signals
            turn.ctx = turn.ctx.model_copy(update={"voice_signals": signals})
        segment = Segment(
            segment_id=segment_id_for(segment_index),
            segment_index=segment_index,
            text=text,
            audio_duration_ms=audio_duration_ms,
        )
        task = self.analyzer.submit(
            session_id=self.session_id,
            turn_id=turn.turn_id,
            turn_index=turn.ctx.turn_index,
            question=self.last_question,
            segment=segment,
            ctx=turn.ctx,
            ledger=turn.ledger,
        )
        task.add_done_callback(self._start_fact_check)
        turn.segments.append(segment)
        turn.tasks.append(task)
        turn.trace.mark(f"segment_{segment_index}_submitted")
        return task

    async def end_of_turn(self, deadline_s: float | None = None) -> BoardMove:
        """The candidate stopped speaking: return the board's move within `deadline_s`.

        `deadline_s` defaults to `MOVE_DEADLINE_MS`. Only this turn's fast analyses are
        waited for, never fact checks. Calling it again before `speak()` returns the same
        move.
        """
        turn = self.turn or self._open_turn(None)
        if turn.move is not None:
            return turn.move
        st = self.state
        if deadline_s is None:
            deadline_s = self.deps.settings.move_deadline_ms / 1000
        tr = turn.trace
        tr.mark("end_of_turn")
        turn.deadline_s = deadline_s
        waited_from = time.monotonic()
        pending: set[asyncio.Task] = set()
        if turn.tasks:
            _, pending = await asyncio.wait(turn.tasks, timeout=deadline_s)
        turn.move_wait_ms = round((time.monotonic() - waited_from) * 1000, 2)
        tr.mark("analysis_done")
        if pending:
            # Ruling 13: unfinished analyses are abandoned for the move, so they cannot hold
            # up the next turn's segments. Their listener calls still run; claims still count.
            self.analyzer.abandon(self.session_id, turn.turn_id)

        results: list[AnalysisResult] = []
        for task in turn.tasks:
            if task in pending or task.cancelled():
                continue
            if task.exception() is not None:
                exc = task.exception()
                turn.fallbacks.append(
                    {
                        "stage": "analysis",
                        "turn_id": turn.turn_id,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue
            results.append(task.result())
        results.sort(key=lambda r: r.segment_index)
        for r in results:
            turn.fallbacks.extend(r.fallbacks)
            for t in r.timings:
                tr.mark(f"{r.segment_id}.{t['stage']}", t["t"])
        # The strategist's latest move for THIS turn (stale results are ignored).
        fresh = [r for r in results if r.move.for_turn == turn.turn_id]
        turn.results = fresh

        if not turn.tasks:
            # Silence: no segment arrived. The rules are pure and instant, so run them.
            move = decide_move(turn.ctx, [], turn.ledger, self.deps.strategy)
        elif pending and fresh:
            move = fresh[-1].move
            turn.fallback_reason = (
                f"deadline {deadline_s:g}s: {len(pending)} of {len(turn.tasks)} segment "
                f"analyses unfinished; used the move after {fresh[-1].segment_id}"
            )
        elif pending:
            turn.fallback_reason = (
                f"deadline {deadline_s:g}s: no segment analysis finished "
                f"({len(turn.tasks)} queued)"
            )
            move = self._fallback_move(turn, turn.fallback_reason)
        elif fresh:
            move = fresh[-1].move
        else:
            turn.fallback_reason = "no segment analysis succeeded"
            move = self._fallback_move(turn, turn.fallback_reason)

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
                for_turn=turn.turn_id,
                preface=move.preface,  # a conceded error is still acknowledged at the close
            )
        tr.mark("move_ready")

        if fresh:
            turn.analysis = merge_analyses([r.analysis for r in fresh])
        else:  # silence is a real (empty) answer; a missing analysis is a fallback
            turn.analysis = Analysis(turn_id=turn.turn_id, is_fallback=bool(turn.tasks))
        st.turns.append(
            CandidateTurn(
                turn_id=turn.turn_id,
                turn_index=turn.ctx.turn_index,
                topic=turn.ctx.topic,
                question=self.last_question,
                segments=list(turn.segments),
                voice_signals=turn.signals,
                analysis=turn.analysis,
            )
        )
        turn.move = move
        return move

    async def speak(self) -> TurnOutcome:
        """Phrase the move chosen by `end_of_turn`, update counters, write the trace line."""
        turn = self.turn
        if turn is None or turn.move is None:
            raise RuntimeError("call end_of_turn() before speak()")
        st, s = self.state, self.deps.settings
        move, tr, analysis = turn.move, turn.trace, turn.analysis or Analysis()
        answer = " ".join(seg.text for seg in turn.segments).strip()
        line = await self._speak(move, answer, turn.turn_id, tr)
        finished = self._apply(move, analysis)
        self.last_question = line.text
        st.board_lines.append(
            BoardLine(
                turn_id=turn.turn_id,
                member=move.member,
                move=move.move,
                text=line.text,
                is_fallback=line.is_fallback,
            )
        )
        checked = [
            c for r in turn.results if needs_fact_check(r.analysis) for c in r.analysis.claims
        ]
        tr.set(
            move=move.move.value,
            rule_id=move.rule_id,
            move_reason=move.reason,
            member=move.member,
            target=move.target,
            board_line=line.text,
            actor_guard=line.guard,
            actor_fallback=line.is_fallback,
            librarian_skipped=not checked,
            segments=len(turn.segments),
            # Deadline and timing of the move (ruling 9): the fast graph is the only work
            # between end of turn and the actor; fact checks are reported separately.
            deadline_s=turn.deadline_s,
            move_wait_ms=turn.move_wait_ms,
            fallback_reason=turn.fallback_reason,
            fast_graph_ms={r.segment_id: r.fast_graph_ms for r in turn.results},
            fast_graph_queued_ms={r.segment_id: r.queued_ms for r in turn.results},
            # This turn's claims; "pending" while their background fact check runs.
            claims=[
                {
                    "id": c.id,
                    "verdict": c.verdict.value,
                    "card_id": c.card_id,
                    "numeric_check": c.numeric_check,
                }
                for c in (self._claim(x.id) or x for x in checked)
            ],
            fact_checks=self._fact_check_trace(),
            fact_checks_pending=self.pending_fact_checks,
            late_listener=self._late_trace(),
            fallbacks=turn.fallbacks,
            prompt_versions=s.prompt_versions(),
        )
        trace = self.recorder.write_turn(tr)
        self.turn = None
        return TurnOutcome(
            turn.turn_id, move, line, turn.results, trace, finished, turn.fallback_reason
        )

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
            late_listener=self._late_trace(),
        )
        await self.analyzer.close_session(self.session_id)
        return report
