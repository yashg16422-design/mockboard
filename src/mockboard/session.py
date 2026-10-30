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
   the trace); its move is never used. An analysis that ends after the move was chosen is
   written to the next trace line under `stale_analyses`, fallbacks included, so no
   fallback goes unrecorded.
   No segments at all means silence: the strategist's rules are run directly (pure, no
   LLM), which reassures.
3. `speak()` has the actor phrase the move (a plain streaming call, not a graph node; guards
   in code), updates the counters deterministically and writes one trace line. The actor
   gets one attempt within `actor_first_token_budget_s` (ruling 14); if it fails, the board
   says a cached fallback line (usually the speaking persona's own, from its YAML file),
   the trace records `actor_fallback`, `actor_fallback_kind`, `actor_error` and a
   `{"stage": "actor"}` entry in `fallbacks`, and the interview simply continues.
   Voice mode (ruling 17): when Pipecat plays a masking filler because the actor's first
   sentence is late, it calls `mark_filler_played()` (or passes `speak(filler_played=True)`
   if the filler was already played before `speak` is called). If the actor then fails, the
   board says the move's template question, never the persona's fallback line straight
   after a filler. Text mode never plays a filler, so its behaviour is unchanged. With
   `actor_fast_throttle_retry` on (off by default), a throttle that comes back within
   `actor_fast_throttle_window_ms` is retried once inside the first-token budget.

Voice mode (V3, ADR-008, CTO_NOTES_04 §7) uses the same three steps, with `speak_stream()`
in place of `speak()`:

* `speak_stream()` yields the line sentence by sentence for TTS. Fact challenges (and moves
  whose guard needs the whole line, such as a closing statement) are buffered: generated in
  full, guarded, then split. Other moves stream: each complete sentence passes the word cap and
  the mode's per-sentence `guard(move, sentence)` (a no-op in UPSC mode) before it is yielded;
  a failing sentence or a failed stream stops the line, and a deterministic question is
  appended if nothing yielded so far asks one.
* `finish_line(spoken, interrupted=...)` then records what the candidate actually heard (only
  the spoken words after a barge-in) and writes the turn's trace line, with the voice fields
  under `voice`. The move still counts toward the pressure limits (round 7).
* `reopen_turn()` (ruling 31): the candidate resumed speaking before the board's first audio,
  so the move is discarded and the same turn takes more segments.
* `turn_params()` gives the voice loop per-turn turn-detection values (longer pauses before a
  debate opening or closing), from the mode's `expects_long_turn` hook.

Text-mode `speak()` is unchanged: the CLI transcripts stay byte-identical.

Fact checks are never awaited by a move. Their gated verdicts are upserted into the ledger
when they end; the strategist sees them through the ledger snapshot of the NEXT turn that
opens after they land (R4 also requires `fact_challenge_min_delay_turns >= 1`). The snapshot
is frozen per turn, so every segment of a turn sees the same ledger and a verdict that lands
mid-turn waits for the next turn: the move never depends on how fast a fact check was.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from mockboard.brain.actor import (
    ACTOR_TEXT_SINK,
    FILLER_TEMPLATES,
    LineResult,
    compose_line,
    generate_opener,
    language_mix,
    preface_text,
)
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
from mockboard.brain.strategist import CLOSE_TARGET, merge_analyses
from mockboard.modes import DEFAULT_MODE, ModeProfile
from mockboard.obs.trace import TraceRecorder, TurnTrace
from mockboard.voice.lines import SentenceSplitter, split_sentences

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


# Moves whose guard needs the whole line, so voice mode never streams them (ADR-008 §1): the
# fact challenge (ask-never-assert, ADR-003) and the closing statement (its concession guard
# may rewrite the start of the line). A mode's hooks may add more with `buffered_moves`.
WHOLE_LINE_MOVES = frozenset({Move.fact_challenge, Move.closing_statement})

# Stages that belong to the end of a turn; `reopen_turn()` moves them aside so the second
# end of the same turn is timed afresh.
_END_STAGES = (
    "vad_end", "commit_sent", "turn_end", "end_of_turn", "analysis_done", "move_ready",
    "actor_request", "actor_first_token", "actor_first_sentence", "actor_done",
    "tts_first_byte", "playback_start",
)


@dataclass(frozen=True)
class SpokenSentence:
    """One sentence of a board line, ready for TTS (voice mode)."""

    text: str
    index: int
    speaker: str  # the display name of the board member speaking
    move: str
    kind: str  # "preface" | "model" | "line" (buffered or no-model text) | "question"


@dataclass(frozen=True)
class TurnParams:
    """Turn-detection values for the candidate's next turn (CTO_NOTES_04 §7). `long_turn` is
    the mode's `expects_long_turn` (a debate opening or closing statement)."""

    long_turn: bool
    smart_turn_stop_secs: float
    extra_pause_s: float
    user_turn_stop_timeout_s: float
    barge_in_min_speech_ms: int
    resume_min_speech_ms: int


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
    # Voice mode (ruling 17): a masking filler has been played this turn.
    filler_played: bool = False
    # Voice mode (V3/V4): the line as generated by the actor, the sentences yielded to TTS,
    # and what happened on the way.
    voice_line: LineResult | None = None
    sent: list[str] = field(default_factory=list)
    line_mode: str = ""
    streaming: bool = False
    guard_stopped: str | None = None
    question_appended: bool = False
    late_finals: list[dict[str, Any]] = field(default_factory=list)
    reopened: int = 0

    @property
    def ended(self) -> bool:
        return self.move is not None


@dataclass
class InterviewSession:
    deps: BrainDeps
    form: CandidateForm | None  # the UPSC application form; None in debate mode
    recorder: TraceRecorder
    analyzer: TurnAnalyzer
    session_id: str = field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:10]}")
    max_turns: int = 9
    callbacks: Sequence[Any] = ()
    fact_checker: FactChecker | None = None
    # ADR-009: the session's mode. The brain takes its rule table, prompts, personas and
    # rubric from the mode's profile; the voice loop never asks which mode it is in.
    mode: str = DEFAULT_MODE
    # Mode-specific setup (debate: a `DebateSetup`), handed to the profile's session hooks.
    mode_options: Any = None
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
    # Fast analyses still running when their turn's move was chosen. They finish later and
    # their moves are ignored, but their fallbacks are still written to the trace.
    _stale: set[asyncio.Task] = field(init=False, default_factory=set)
    _stale_done: list[dict[str, Any]] = field(init=False, default_factory=list)
    # The mode's session hooks (ADR-009), or None for UPSC, whose code paths are below.
    hooks: Any = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.fact_checker is None:
            self.fact_checker = FactChecker(self.deps, callbacks=self.callbacks)
        factory = self.profile.session_hooks
        if factory is not None:
            self.hooks = factory(self, self.mode_options)
        topics = (
            self.hooks.topics()
            if self.hooks is not None
            else [BACKGROUND_TOPIC, *self.deps.settings.topics]
        )
        self.state = SessionState(
            session_id=self.session_id, form=self.form, topics=topics, mode=self.mode
        )

    @property
    def profile(self) -> ModeProfile:
        return self.deps.mode(self.mode).profile

    # -- helpers -------------------------------------------------------------

    def _max_turns_in(self, topic: str) -> int:
        if self.hooks is not None:
            return self.hooks.max_turns_in(topic)
        return 1 if topic == BACKGROUND_TOPIC else self.deps.settings.max_turns_per_topic

    def speaker_name(self, member: str) -> str:
        """The display name of the board member who speaks a move (mode-agnostic)."""
        if self.hooks is not None:
            return self.hooks.speaker_name(member)
        persona = self.deps.personas.get(member)
        return persona.name if persona else member

    def guard_sentence(self, move: BoardMove, sentence: str) -> bool:
        """Mode-agnostic per-sentence guard for the voice loop's `speak_stream` (ruling 22):
        True means the sentence may be spoken. Always True in UPSC mode (a no-op). In debate
        mode, a sentence with a figure that is not on the motion's cards is dropped
        (CTO_NOTES_04 §3); fact challenges keep their own ask-never-assert guard."""
        if self.hooks is None or not hasattr(self.hooks, "guard_sentence"):
            return True
        return self.hooks.guard_sentence(move, sentence)

    @property
    def expects_long_turn(self) -> bool:
        """Mode-agnostic hint for the voice loop's per-session turn detection (ruling 22):
        True when the next candidate turn is a long one (a debate opening or closing
        statement, up to about 90 seconds), so a longer pause should not end it. Always
        False in UPSC mode."""
        if self.hooks is None or not hasattr(self.hooks, "expects_long_turn"):
            return False
        return bool(self.hooks.expects_long_turn())

    def closing_line(self) -> str | None:
        """What the board says while the report is prepared, if anything (debate: "The
        board will confer"). The voice loop speaks it before awaiting `finish()`."""
        if self.hooks is not None and hasattr(self.hooks, "confer_line"):
            return self.hooks.confer_line()
        return None

    def turn_params(self) -> TurnParams:
        """Per-turn turn-detection values for the voice loop (CTO_NOTES_04 §7, ruling 22).
        Long turns (the mode's `expects_long_turn`: a debate opening or closing) tolerate
        longer pauses. Pure configuration: UPSC mode always gets the normal values."""
        s = self.deps.settings
        long_turn = self.expects_long_turn
        return TurnParams(
            long_turn=long_turn,
            smart_turn_stop_secs=(
                s.voice_long_turn_smart_turn_stop_secs if long_turn
                else s.voice_smart_turn_stop_secs
            ),
            extra_pause_s=s.voice_long_turn_extra_pause_s if long_turn else 0.0,
            user_turn_stop_timeout_s=(
                s.voice_long_turn_stop_timeout_s if long_turn
                else s.voice_user_turn_stop_timeout_s
            ),
            barge_in_min_speech_ms=s.voice_barge_in_min_speech_ms,
            resume_min_speech_ms=s.voice_resume_min_speech_ms,
        )

    def mark(self, stage: str, at: float | None = None) -> None:
        """Voice mode: record a stage (e.g. `vad_end`, `commit_sent`, `tts_first_byte`) on
        the open turn's trace, at `at` (time.monotonic) or now. First occurrence wins. Does
        nothing when no turn is open."""
        if self.turn is not None:
            self.turn.trace.mark(stage, at)

    def _next_topic(self) -> str | None:
        i = self.state.topic_index + 1
        return self.state.topics[i] if i < len(self.state.topics) else None

    def _ctx(self, turn_id: str, signals: VoiceSignals) -> StrategyContext:
        ctx = self._base_ctx(turn_id, signals)
        return self.hooks.ctx(ctx) if self.hooks is not None else ctx

    def _base_ctx(self, turn_id: str, signals: VoiceSignals) -> StrategyContext:
        s = self.state
        return StrategyContext(
            mode=self.mode,
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

    def _turn_written(self, turn_id: str) -> bool:
        """True once `turn_id`'s trace line is written (its llm calls must go elsewhere)."""
        return self.turn is None or self.turn.turn_id != turn_id

    def _analysis_done(self, analysis_task: asyncio.Task[AnalysisResult]) -> None:
        """Done-callback of a fast analysis: start the background fact check for its claims.

        The task is kept in `_fact_checks` but no move ever awaits it; its gated verdicts are
        upserted into the ledger when it ends, and the strategist sees them on a later turn.
        An analysis that ended after its turn's move was chosen is recorded as stale.
        """
        stale = analysis_task in self._stale
        self._stale.discard(analysis_task)
        if analysis_task.cancelled():
            return
        if analysis_task.exception() is not None:
            if stale:
                exc = analysis_task.exception()
                self._stale_done.append(
                    {"outcome": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
                )
            return
        result = analysis_task.result()
        if stale:
            entry: dict[str, Any] = {
                "for_turn": result.turn_id,
                "segment_id": result.segment_id,
                "outcome": "finished after the move was chosen; move ignored",
                "fallbacks": result.fallbacks,
            }
            if self._turn_written(result.turn_id):
                entry["llm_calls"] = self.recorder.pop_calls(result.turn_id)
            self._stale_done.append(entry)
        if result.late_analysis is not None:
            late = result.late_analysis
            self._late_listens.add(late)
            late.add_done_callback(functools.partial(self._late_listen_done, result))
        self._fact_check(result.analysis, result.turn_id, result.segment_id)

    def _fact_check(self, analysis: Analysis, turn_id: str, segment_id: str) -> None:
        if not needs_fact_check(analysis):
            return
        if self.hooks is not None and not self.hooks.fact_check_enabled:
            return  # e.g. a debate motion with no fact cards: figures stay unverifiable
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
        if self._turn_written(result.turn_id):
            # The turn's line is already written; keep the late call's stats with this entry.
            entry["llm_calls"] = self.recorder.pop_calls(result.turn_id)
        self._late_done.append(entry)

    def _late_trace(self) -> list[dict[str, Any]]:
        done, self._late_done = self._late_done, []
        return done

    def _stale_trace(self) -> list[dict[str, Any]]:
        done, self._stale_done = self._stale_done, []
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
        while pending := self._fact_checks | self._late_listens | self._stale:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.wait(pending, timeout=remaining)
        return self._fact_checks | self._late_listens | self._stale

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
        (CTO_NOTES_02 M4.4), from the mode's profile. UPSC: nothing said -> reassure
        (chairman), otherwise a neutral probe."""
        rt = self.deps.mode(self.mode)
        spoke = any(s.text.strip() for s in turn.segments)
        return rt.profile.fallback_move(turn.ctx, spoke, reason, rt.strategy)

    # -- public API ----------------------------------------------------------

    async def open(self) -> LineResult:
        if self.hooks is not None:
            return await self._open_with_hooks()
        s = self.deps.settings
        line = await generate_opener(
            self.deps.llm,
            self.deps.kit,
            self.form,
            self.deps.cards,
            max_words=max(s.actor_max_words, 40),
            max_tokens=s.actor_max_tokens,
            turn_id="opener",
            first_token_budget_s=s.actor_first_token_budget_s,
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
            opener_error=line.error or None,
            card_set_version=self.deps.cards.version,
            cards_all_synthetic=self.deps.cards.all_synthetic,
            feature_flags=s.feature_flags(),
            prompt_versions=s.prompt_versions(),
        )
        return line

    async def _open_with_hooks(self) -> LineResult:
        s = self.deps.settings
        line, move, extra = await self.hooks.open()
        self.last_question = line.text
        self.state.board_lines.append(
            BoardLine(
                turn_id="t0", member=move.member, move=move.move, text=line.text,
                is_fallback=line.is_fallback,
            )
        )
        self.recorder.write_event(
            "session_start",
            tag="opener",
            opener_guard=line.guard,
            opener_fallback=line.is_fallback,
            opener_error=line.error or None,
            card_set_version=self.deps.cards.version,
            cards_all_synthetic=self.deps.cards.all_synthetic,
            feature_flags=s.feature_flags(),
            prompt_versions=s.prompt_versions(self.mode),
            **extra,
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
        task.add_done_callback(self._analysis_done)
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
            self._stale.update(pending)

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

        rt = self.deps.mode(self.mode)
        if not turn.tasks:
            # Silence: no segment arrived. The rules are pure and instant, so run them.
            move = rt.profile.decide(turn.ctx, [], turn.ledger, rt.strategy)
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

        if st.turn_index + 1 >= self.max_turns:
            # The hard turn cap: the mode decides how the session closes.
            move = rt.profile.cap_move(turn.ctx, move, self.max_turns, rt.strategy)
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

    def mark_filler_played(self) -> None:
        """Voice mode (ruling 17): Pipecat has played a masking filler for the current turn.

        Safe to call while `speak()` is waiting for the actor (the filler starts when the
        first sentence is late). If the actor then fails, the fallback is the move's template
        question rather than the persona's fallback line, so two pauses never play in a row.
        """
        if self.turn is None:
            raise RuntimeError("no open turn to mark a filler on")
        self.turn.filler_played = True

    async def speak(self, *, filler_played: bool = False) -> TurnOutcome:
        """Phrase the move chosen by `end_of_turn`, update counters, write the trace line.

        `filler_played=True` (voice mode, ruling 17) says a masking filler was already
        played before this call; a filler that starts during the call is reported with
        `mark_filler_played()` instead. Text mode leaves both alone."""
        turn = self.turn
        if turn is None or turn.move is None:
            raise RuntimeError("call end_of_turn() before speak()")
        if filler_played:
            turn.filler_played = True
        line = await self._compose_line(turn)
        return self._finalize(turn, line)

    async def _compose_line(self, turn: OpenTurn) -> LineResult:
        """The actor's line for the turn's move, with every guard (text and voice modes)."""
        move, tr = turn.move, turn.trace
        answer = " ".join(seg.text for seg in turn.segments).strip()
        if self.hooks is not None:
            return await self.hooks.speak(move, answer, turn, tr)
        return await self._speak(move, answer, turn, tr)

    def _finalize(
        self, turn: OpenTurn, line: LineResult, *, voice: dict[str, Any] | None = None
    ) -> TurnOutcome:
        """Counters, board line and trace line after the line is said. `voice` (voice mode
        only) holds what was really heard; then `line.text` is the spoken text, and an empty
        line (nothing heard) leaves `last_question` and `board_lines` as they were."""
        st, s = self.state, self.deps.settings
        move, tr, analysis = turn.move, turn.trace, turn.analysis or Analysis()
        if line.error:  # ruling 14: the actor failed or missed its budget
            turn.fallbacks.append(
                {
                    "stage": "actor",
                    "turn_id": turn.turn_id,
                    "error": line.error,
                    "spoke": line.fallback_kind,
                }
            )
        if self.hooks is not None:
            finished = self.hooks.apply(move, analysis)
            member = self.hooks.persona_id(move.member)
        else:
            finished = self._apply(move, analysis)
            member = move.member
        if voice is None or line.text.strip():
            self.last_question = line.text
            st.board_lines.append(
                BoardLine(
                    turn_id=turn.turn_id,
                    member=member,
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
            actor_fallback_kind=line.fallback_kind or None,
            actor_error=line.error or None,
            actor_fast_retry=line.retried,
            filler_played=turn.filler_played,
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
            stale_analyses=self._stale_trace(),
            fallbacks=turn.fallbacks,
            prompt_versions=s.prompt_versions(self.mode),
        )
        if self.hooks is not None:
            tr.set(**self.hooks.trace_fields(move, line))
        if voice is not None:
            tr.set(voice=voice)
        trace = self.recorder.write_turn(tr)
        self.turn = None
        return TurnOutcome(
            turn.turn_id, move, line, turn.results, trace, finished, turn.fallback_reason
        )

    # -- voice mode (V3/V4) ---------------------------------------------------

    def _buffers_whole_line(self, move: BoardMove) -> bool:
        extra = getattr(self.hooks, "buffered_moves", None) or ()
        return move.move in WHOLE_LINE_MOVES or move.move in extra

    def _preface(self, move: BoardMove) -> str:
        """The code-side acknowledgement said before the model's part (ruling 11)."""
        if move.preface is None:
            return ""
        mix = getattr(self.hooks, "mix", None)
        if mix is None:
            persona = self.deps.personas.get(move.member) or self.deps.personas[
                self.deps.strategy.chairman
            ]
            mix = language_mix(persona, self.form) if self.form is not None else "hi-en"
        return preface_text(move, mix)

    def _template_question(self, move: BoardMove, line: LineResult) -> str | None:
        """The deterministic question appended when a streamed line stopped early and nothing
        sent so far asks one (ADR-008 §2). A mode may provide `template_line(move)`; UPSC uses
        its move templates; otherwise a no-model line that ends in a question is reused."""
        hook = getattr(self.hooks, "template_line", None)
        if hook is not None:
            return hook(move)
        if self.hooks is None:
            return FILLER_TEMPLATES.get(move.move, FILLER_TEMPLATES[Move.probe])
        if line.is_fallback and line.text.strip().endswith("?"):
            return split_sentences(line.text)[-1]
        return None

    async def speak_stream(self, *, filler_played: bool = False) -> AsyncIterator[SpokenSentence]:
        """Voice mode: the line for the move chosen by `end_of_turn`, sentence by sentence.

        Buffered moves (`WHOLE_LINE_MOVES`) are generated and guarded in full first. Other
        moves stream: the acknowledgement preface (code-side) goes first, then each complete
        model sentence that passes `guard_sentence`. A failing sentence stops the line; if the
        actor fails after some sentences, the line stops there too; then, unless something
        sent already asks a question, the move's template question is appended. If nothing
        from the model was sent, the final line (a fallback, ruling 14/17) is said instead.

        Nothing is recorded until `finish_line()`, which the caller must call once playback
        ended or was interrupted. Cancelling the iteration cancels the actor call (its Bedrock
        stream is closed by `brain/llm.py`)."""
        turn = self.turn
        if turn is None or turn.move is None:
            raise RuntimeError("call end_of_turn() before speak_stream()")
        if turn.streaming:
            raise RuntimeError("speak_stream() is already running for this turn")
        if filler_played:
            turn.filler_played = True
        move, tr = turn.move, turn.trace
        speaker = self.speaker_name(move.member)
        buffered = self._buffers_whole_line(move)
        turn.streaming, turn.line_mode = True, ("buffered" if buffered else "streamed")
        turn.sent, turn.voice_line = [], None
        turn.guard_stopped, turn.question_appended = None, False
        chunks: asyncio.Queue[str | None] = asyncio.Queue()

        async def compose() -> LineResult:
            if not buffered:
                ACTOR_TEXT_SINK.set(chunks.put_nowait)  # this task's context only
            return await self._compose_line(turn)

        task = asyncio.create_task(compose(), name=f"actor-{turn.turn_id}")
        task.add_done_callback(lambda _t: chunks.put_nowait(None))
        kinds: list[str] = []

        def emit(text: str, kind: str) -> SpokenSentence:
            if not turn.sent:
                tr.mark("actor_first_sentence")
            turn.sent.append(text)
            kinds.append(kind)
            return SpokenSentence(text, len(turn.sent) - 1, speaker, move.move.value, kind)

        try:
            if buffered:
                line = await task
                for sentence in split_sentences(line.text):
                    if self.guard_sentence(move, sentence):
                        yield emit(sentence, "line")
            else:
                preface = self._preface(move)
                for sentence in split_sentences(preface):
                    yield emit(sentence, "preface")
                splitter, stopped = SentenceSplitter(), False
                while (chunk := await chunks.get()) is not None:
                    if stopped:
                        continue
                    for sentence in splitter.push(chunk):
                        if not self.guard_sentence(move, sentence):
                            stopped, turn.guard_stopped = True, sentence
                            break
                        yield emit(sentence, "model")
                rest = splitter.flush()
                if rest and not stopped:
                    if self.guard_sentence(move, rest):
                        yield emit(rest, "model")
                    else:
                        stopped, turn.guard_stopped = True, rest
                line = task.result()
                if "model" not in kinds:
                    text = line.text
                    if preface and text.startswith(preface):
                        text = text[len(preface):]
                    for sentence in split_sentences(text):
                        if self.guard_sentence(move, sentence):
                            yield emit(sentence, "line")
                elif (stopped or line.is_fallback) and not any(
                    x.rstrip().endswith("?") for x in turn.sent
                ):
                    question = self._template_question(move, line)
                    if question and self.guard_sentence(move, question):
                        turn.question_appended = True
                        yield emit(question, "question")
            turn.voice_line = line
        finally:
            turn.streaming = False
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def finish_line(
        self,
        spoken: str | None = None,
        *,
        interrupted: bool = False,
        spoken_estimated: bool = False,
        details: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        """Voice mode: record the board's line as the candidate heard it and close the turn.

        `spoken` is the heard text (ADR-008 §3; None means every sentence `speak_stream`
        yielded was heard). After a barge-in pass `interrupted=True` and the heard part only;
        `last_question` then becomes that part, or stays the previous question if nothing
        was heard. The move still counts toward the pressure limits. The full generated line
        stays in the trace as `voice.board_line_generated`. `details` (playback timings,
        per-sentence audio) is added to the trace's `voice` block."""
        turn = self.turn
        if turn is None or turn.move is None:
            raise RuntimeError("no turn with a chosen move to finish")
        if turn.streaming:
            raise RuntimeError("stop iterating speak_stream() before finish_line()")
        sent = " ".join(turn.sent).strip()
        generated = turn.voice_line
        cancelled = generated is None
        if generated is None:  # the actor was cancelled (barge-in) before it finished
            generated = LineResult(sent)
        heard = sent if spoken is None else spoken.strip()
        voice: dict[str, Any] = {
            "line_mode": turn.line_mode,
            "board_line_generated": generated.text,
            "board_line_sent": sent,
            "board_line_spoken": heard,
            "interrupted": interrupted,
            "spoken_estimated": spoken_estimated,
            "actor_cancelled": cancelled,
            "guard_stopped": turn.guard_stopped,
            "question_appended": turn.question_appended,
            "sentences_sent": len(turn.sent),
            "late_finals": list(turn.late_finals),
            "turn_reopened": turn.reopened,
        }
        if turn.move.move == Move.fact_challenge:
            # ADR-008 §3: the challenge was heard only if its line played to the end.
            voice["challenge_heard"] = bool(heard) and heard == sent and not interrupted
        voice.update(details or {})
        return self._finalize(turn, replace(generated, text=heard), voice=voice)

    def reopen_turn(self) -> None:
        """Ruling 31: the candidate resumed speaking before the board's first audio, so the
        turn had not really ended. The chosen move is dropped (its actor call must already be
        cancelled), the segments heard so far stay, and the same turn takes more segments
        until `end_of_turn` is called again. The first attempt's stage times are kept in the
        trace under `reopened`."""
        turn = self.turn
        if turn is None or turn.move is None:
            raise RuntimeError("no ended turn to reopen")
        if turn.streaming:
            raise RuntimeError("stop iterating speak_stream() before reopen_turn()")
        st = self.state
        if st.turns and st.turns[-1].turn_id == turn.turn_id:
            st.turns.pop()
        stages = turn.trace.stages
        turn.trace.fields.setdefault("reopened", []).append(
            {"move": turn.move.move.value, "stages_ms": {k: stages[k] for k in _END_STAGES
                                                          if k in stages}}
        )
        for k in _END_STAGES:
            stages.pop(k, None)
        turn.reopened += 1
        turn.move, turn.results, turn.fallbacks = None, [], []
        turn.fallback_reason, turn.deadline_s, turn.analysis = None, None, None
        turn.voice_line, turn.sent = None, []
        turn.guard_stopped, turn.question_appended = None, False

    def note_late_final(self, text: str, *, commit_id: int | None = None) -> None:
        """Ruling 26 safety net: a transcript that belongs to a turn whose move was already
        chosen (its commit wait timed out). It never starts a turn or changes the move; its
        words join that turn's transcript (so the report sees them) and the trace."""
        text = text.strip()
        if not text or not self.state.turns:
            return
        cand = self.state.turns[-1]
        index = (cand.segments[-1].segment_index + 1) if cand.segments else 0
        cand.segments.append(
            Segment(segment_id=segment_id_for(index), segment_index=index, text=text)
        )
        entry = {"turn_id": cand.turn_id, "segment_index": index, "commit_id": commit_id,
                 "words": len(text.split())}
        if self.turn is not None and self.turn.turn_id == cand.turn_id:
            self.turn.late_finals.append(entry)
        else:
            self.recorder.write_event("late_final", tag=cand.turn_id, **entry)

    async def _speak(self, move: BoardMove, answer: str, turn: OpenTurn, tr) -> LineResult:
        s = self.deps.settings
        turn_id = turn.turn_id
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
            first_token_budget_s=s.actor_first_token_budget_s,
            filler_played=lambda: turn.filler_played,
            fast_throttle_retry_window_s=(
                s.actor_fast_throttle_window_ms / 1000 if s.actor_fast_throttle_retry else None
            ),
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
        if self.hooks is not None:
            report = await self.hooks.report(language)
        else:
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
            stale_analyses=self._stale_trace(),
        )
        await self.analyzer.close_session(self.session_id)
        return report
