r"""LangGraph graphs for the slow brain (PLAN_v2_DELTA §2, orchestrator rulings 1, 9, 10).

Three graphs, all OFF the actor's critical path. The actor is NOT a node here.

1. Fast analysis graph (ruling 9), run once per transcript segment, in segment order::

       START -> listener -> strategist -> END

   * `listener`   one forced-tool Nova call -> typed `Analysis` (claims stamped with ids).
   * `strategist` pure deterministic rules (no LLM). It reads the session's claim ledger
                  as an input snapshot taken when the run starts, so verdicts that the
                  fact-check graph produced for EARLIER turns can trigger a fact challenge.

   This is the only work allowed between end-of-turn and the actor call. One checkpointer
   thread per session (`thread_id = session_id`) accumulates the turn's analyses, so the
   strategist sees every segment of the turn so far. Runs for one session are queued in
   submission order by `TurnAnalyzer` (CTO_NOTES_01 §2: overlapping runs on one thread
   would race on state).

2. Fact-check graph (ruling 9), started in the background by the session for each segment
   that has claims, never awaited by the move::

       START --(Send per claim)--> librarian (xN, parallel) -> gate -> END
             \--(no claims)-----------------------------------------> END

   * `librarian`  one call per claim, fanned out with `Send`. It returns only the model's
                  *vote*, keyed by claim content. It first looks in `deps.vote_cache`
                  (claim hash + card-set version, process-wide) and writes back only
                  real votes, never fallbacks (ruling 10).
   * `gate`       pure code: `librarian.gate_verdict` turns each vote into the final verdict.
                  "contradicted" stands only when the model AND the numeric check agree.

   The run is stateless (no checkpointer): its gated claims go back to the session, which
   upserts them into its ledger. The strategist sees them on the next turn. Fact challenges
   are a turn late by design (R4 with `fact_challenge_min_delay_turns >= 1`, enforced by a
   config validator), so the move never needs this turn's verdicts.

3. Post-session report graph::

       START -> judge -> citation_check --(quotes missing, no revision yet)--> judge
                                        \--------------------------------------> report -> END

   An evaluator-optimizer loop bounded at one revision.

Error handling (verified against langgraph 1.2.17 on Oct 25, 2026):
* If any parallel branch raises, the whole superstep fails. LangGraph's per-node
  `error_handler` recovers a single failing task but did NOT rescue a run with several
  parallel `Send` branches in our probe, so every node catches its own errors.
* Transient errors (throttling, timeouts) are re-raised while attempts remain, so the
  explicit `RetryPolicy(retry_on=is_transient)` retries them; on the final attempt the node
  returns its safe default instead of raising. The node learns its attempt number from
  `runtime.execution_info.node_attempt`.
* A fallback vote must never be cached as if it were a verdict. We do NOT use LangGraph's
  node cache for the librarian: it stores the node's whole return value, fallbacks
  included, and a cache shared across sessions then served one session's throttled
  fallback to every later session (CTO_NOTES_02 M1). `VoteCache.put` refuses fallbacks.
"""

from __future__ import annotations

import asyncio
import inspect
import operator
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, Send
from pydantic import BaseModel

from mockboard.brain import schemas as _schemas
from mockboard.brain import tools as _tools
from mockboard.brain.deps import BrainDeps
from mockboard.brain.judge import (
    assemble_report,
    check_citations,
    fallback_report,
    run_judge,
)
from mockboard.brain.librarian import ask_librarian, claim_cache_key, fallback_vote, gate_verdict
from mockboard.brain.listener import analyse_segment, fallback_analysis
from mockboard.brain.llm import LLMError, is_transient
from mockboard.brain.schemas import (
    Analysis,
    BoardMove,
    CandidateTurn,
    Claim,
    LibrarianVote,
    Move,
    ReportLanguage,
    Segment,
    SessionReport,
    StrategyContext,
    Verdict,
)
from mockboard.brain.strategist import PRESSURE, decide_move
from mockboard.brain.tools import JudgeEvidence, JudgeOutput

MAX_JUDGE_REVISIONS = 1


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def merge_dict(left: dict | None, right: dict | None) -> dict:
    """Right wins on key clashes. Used for votes coming back from parallel `Send` branches."""
    return {**(left or {}), **(right or {})}


def upsert_claims(left: list[Claim] | None, right: list[Claim] | None) -> list[Claim]:
    """The session claim ledger: append new claims, replace a claim with the same id."""
    out = list(left or [])
    index = {c.id: i for i, c in enumerate(out)}
    for claim in right or []:
        if claim.id in index:
            out[index[claim.id]] = claim
        else:
            index[claim.id] = len(out)
            out.append(claim)
    return out


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class FastState(TypedDict, total=False):
    """State of the fast analysis graph (one checkpointer thread per session).

    Inputs for each run: `session_id`, `turn_id`, `turn_index`, `question`, `segment`, `ctx`,
    and `ledger`, the snapshot of the session's claim ledger taken when the run starts.
    `ledger` has no reducer on purpose: every run overwrites it with the latest snapshot,
    so the strategist always sees the verdicts the fact-check graph has produced so far.
    Fields with reducers accumulate across runs of the same session thread.
    """

    session_id: str
    turn_id: str
    turn_index: int
    question: str
    segment: Segment
    ctx: StrategyContext
    ledger: list[Claim]  # input snapshot, overwritten by every run
    # Accumulating, session-long.
    analyses: Annotated[list[Analysis], operator.add]
    moves: Annotated[list[BoardMove], operator.add]
    fallbacks: Annotated[list[dict[str, str]], operator.add]
    timings: Annotated[list[dict[str, Any]], operator.add]
    # Written by the strategist on every run.
    move: BoardMove


class FactCheckState(TypedDict, total=False):
    """State of one fact-check run (stateless: no checkpointer, one run per segment).

    Inputs: `turn_id`, `segment_id`, `claims`, `card_set_version`, `trace_tag`.
    Output: `gated`, the claims with their final verdicts.
    """

    turn_id: str
    segment_id: str
    claims: list[Claim]
    card_set_version: str  # part of every librarian vote-cache key
    trace_tag: str  # groups the librarian calls in the per-turn trace
    votes: Annotated[dict[str, LibrarianVote], merge_dict]
    lib_failures: Annotated[list[str], operator.add]  # audit only; never read by routing
    fallbacks: Annotated[list[dict[str, str]], operator.add]
    timings: Annotated[list[dict[str, Any]], operator.add]
    gated: list[Claim]


class LibrarianTask(TypedDict):
    """The payload each `Send` branch receives (one claim)."""

    claim: Claim
    claim_key: str
    turn_id: str
    segment_id: str
    trace_tag: str


class ReportState(TypedDict, total=False):
    session_id: str
    language: ReportLanguage
    turns: list[CandidateTurn]
    ledger: list[Claim]
    moves: list[BoardMove]
    draft: JudgeOutput | None
    failed: list[JudgeEvidence]
    revisions: int
    error: str
    report: SessionReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamp(state: dict, stage: str) -> dict[str, Any]:
    """One stage timestamp (monotonic seconds), stamped with turn and segment ids."""
    seg = state.get("segment")
    return {
        "turn_id": state.get("turn_id", ""),
        "segment_id": seg.segment_id if isinstance(seg, Segment) else state.get("segment_id", ""),
        "stage": stage,
        "t": time.monotonic(),
    }


def _attempt(runtime: Runtime) -> int:
    info = getattr(runtime, "execution_info", None)
    return getattr(info, "node_attempt", 1) or 1


def _should_reraise(exc: BaseException, runtime: Runtime, max_attempts: int) -> bool:
    """Re-raise transient errors while retries remain, so RetryPolicy handles them."""
    return is_transient(exc) and _attempt(runtime) < max_attempts


def _fallback(stage: str, turn_id: str, exc: BaseException) -> dict[str, str]:
    return {"stage": stage, "turn_id": turn_id, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _ms(start: float, end: float) -> float:
    return round((end - start) * 1000, 2)


def _serde_types() -> list[type]:
    """Every project model/enum that may sit in graph state: an explicit msgpack allow-list,
    so checkpoints never deserialise arbitrary types (and no warnings in tests)."""
    found: list[type] = []
    for module in (_schemas, _tools):
        for obj in vars(module).values():
            if (
                inspect.isclass(obj)
                and obj.__module__ == module.__name__
                and issubclass(obj, BaseModel | Enum)
            ):
                found.append(obj)
    return found


def make_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_serde_types())


def retry_policy(max_attempts: int, initial_interval_s: float) -> RetryPolicy:
    """Explicit retry on our transient errors only (throttling, timeouts, 5xx).

    botocore errors are mapped to `LLMTransientError` in `llm.py`, so the default LangGraph
    retry behaviour toward botocore exceptions (unverified) never matters.
    """
    return RetryPolicy(
        max_attempts=max_attempts,
        initial_interval=initial_interval_s,
        backoff_factor=2.0,
        jitter=True,
        retry_on=is_transient,
    )


def needs_fact_check(analysis: Analysis) -> bool:
    """A segment goes to the fact-check graph only when it has claims and is not garbled
    (a garbled transcript gets `clarify`; its "claims" are not worth a librarian call)."""
    return bool(analysis.claims) and not analysis.garbled


# ---------------------------------------------------------------------------
# Fast analysis graph: nodes
# ---------------------------------------------------------------------------


def _make_listener(max_attempts: int) -> Callable:
    async def listener(state: FastState, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
        deps = runtime.context
        seg = state["segment"]
        start = _stamp(state, "listener_start")
        fallbacks: list[dict[str, str]] = []
        try:
            analysis = await analyse_segment(
                deps.llm,
                deps.kit,
                segment=seg,
                question=state.get("question", ""),
                turn_id=state["turn_id"],
                turn_index=state.get("turn_index", 0),
            )
        except Exception as exc:
            if _should_reraise(exc, runtime, max_attempts):
                raise
            analysis = fallback_analysis(
                turn_id=state["turn_id"],
                segment=seg,
                note=f"listener fallback ({type(exc).__name__})",
            )
            fallbacks.append(_fallback("listener", state["turn_id"], exc))
        return {
            "analyses": [analysis],
            "fallbacks": fallbacks,
            "timings": [start, _stamp(state, "listener_end")],
        }

    return listener


async def strategist(state: FastState, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
    """Pure rules over this turn's segments so far and the ledger snapshot."""
    deps = runtime.context
    ctx = state["ctx"]
    analyses = [a for a in state.get("analyses") or [] if a.turn_id == ctx.turn_id]
    try:
        move = decide_move(ctx, analyses, state.get("ledger") or [], deps.strategy)
        fallbacks: list[dict[str, str]] = []
    except Exception as exc:  # the rules are pure, but a bug must not stall the board
        move = BoardMove(
            move=Move.probe,
            member=deps.strategy.member_for(ctx.topic),
            pressure=PRESSURE[Move.probe],
            reason="FALLBACK: strategist error",
            rule_id="FALLBACK",
            for_turn=ctx.turn_id,
        )
        fallbacks = [_fallback("strategist", ctx.turn_id, exc)]
    return {
        "move": move,
        "moves": [move],
        "fallbacks": fallbacks,
        "timings": [_stamp(state, "strategist_end")],
    }


def build_fast_graph(*, max_attempts: int = 3, initial_interval_s: float = 0.5) -> StateGraph:
    g = StateGraph(FastState, context_schema=BrainDeps)
    g.add_node(
        "listener",
        _make_listener(max_attempts),
        retry_policy=retry_policy(max_attempts, initial_interval_s),
    )
    g.add_node("strategist", strategist)
    g.add_edge(START, "listener")
    g.add_edge("listener", "strategist")
    g.add_edge("strategist", END)
    return g


def compile_fast_graph(
    *,
    max_attempts: int = 3,
    initial_interval_s: float = 0.5,
    checkpointer: Any = None,
):
    """Compile once per process. In-memory checkpointer (thread per session) with an
    explicit type allow-list."""
    serde = make_serde()
    builder = build_fast_graph(max_attempts=max_attempts, initial_interval_s=initial_interval_s)
    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(serde=serde),
        name="fast_analysis",
    )


# ---------------------------------------------------------------------------
# Fact-check graph: nodes
# ---------------------------------------------------------------------------


def route_claims(state: FactCheckState) -> list[Send] | str:
    """Pure function of state: one `Send` per claim, or straight to END with no claims."""
    claims = state.get("claims") or []
    if not claims:
        return END
    card_version = state.get("card_set_version", "")
    return [
        Send(
            "librarian",
            LibrarianTask(
                claim=claim,
                claim_key=claim_cache_key(claim, card_version),
                turn_id=state.get("turn_id", ""),
                segment_id=claim.segment_id,
                trace_tag=state.get("trace_tag") or state.get("turn_id", ""),
            ),
        )
        for claim in claims
    ]


def _make_librarian(max_attempts: int) -> Callable:
    async def librarian(task: LibrarianTask, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
        deps = runtime.context
        key = task["claim_key"]
        cached = deps.vote_cache.get(key)
        if cached is not None:  # only real votes are ever in the cache
            return {"votes": {key: cached}}
        try:
            vote = await ask_librarian(
                deps.llm, deps.kit, task["claim"], claim_key=key, trace_tag=task["trace_tag"]
            )
            deps.vote_cache.put(vote)  # refuses fallbacks by construction
            return {"votes": {key: vote}}
        except Exception as exc:  # per-claim isolation: never fail the superstep
            if _should_reraise(exc, runtime, max_attempts):
                raise
            return {
                "votes": {key: fallback_vote(key, f"{type(exc).__name__}")},
                "lib_failures": [key],
                "fallbacks": [_fallback("librarian", task["turn_id"], exc)],
            }

    return librarian


async def gate(state: FactCheckState, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
    """Deterministic verdicts for this segment's claims (ruling 1)."""
    deps = runtime.context
    votes = state.get("votes") or {}
    gated: list[Claim] = []
    fallbacks: list[dict[str, str]] = []
    for claim in state.get("claims") or []:
        try:
            vote = votes.get(claim_cache_key(claim, state.get("card_set_version", "")))
            gated.append(gate_verdict(claim, vote, deps.cards, deps.tolerance))
        except Exception as exc:
            gated.append(
                claim.model_copy(
                    update={"verdict": Verdict.unverifiable, "verdict_note": "gate error"}
                )
            )
            fallbacks.append(_fallback("gate", state.get("turn_id", ""), exc))
    return {"gated": gated, "fallbacks": fallbacks, "timings": [_stamp(state, "gate_end")]}


def build_factcheck_graph(
    *, max_attempts: int = 3, initial_interval_s: float = 0.5
) -> StateGraph:
    g = StateGraph(FactCheckState, context_schema=BrainDeps)
    # No cache_policy here on purpose: see the module docstring and ruling 10.
    g.add_node(
        "librarian",
        _make_librarian(max_attempts),
        retry_policy=retry_policy(max_attempts, initial_interval_s),
    )
    g.add_node("gate", gate)
    g.add_conditional_edges(START, route_claims, ["librarian", END])
    g.add_edge("librarian", "gate")
    g.add_edge("gate", END)
    return g


def compile_factcheck_graph(*, max_attempts: int = 3, initial_interval_s: float = 0.5):
    """Stateless: no checkpointer. Each run checks one segment's claims and returns them."""
    builder = build_factcheck_graph(
        max_attempts=max_attempts, initial_interval_s=initial_interval_s
    )
    return builder.compile(name="fact_check")


# ---------------------------------------------------------------------------
# Running the fast graph: per-session queue in submission order
# ---------------------------------------------------------------------------

LedgerSource = Sequence[Claim] | Callable[[], Sequence[Claim]]


@dataclass
class AnalysisResult:
    """What the session controller gets back for one segment. Stamped with ids, so the
    controller can ignore stale results (a move computed for an earlier turn)."""

    session_id: str
    turn_id: str
    segment_id: str
    segment_index: int
    analysis: Analysis
    move: BoardMove
    fallbacks: list[dict[str, str]]
    timings: list[dict[str, Any]]
    queued_ms: float = 0.0  # waiting behind this session's earlier segments
    fast_graph_ms: float = 0.0  # the graph run itself (listener + strategist)
    ledger_size: int = 0  # claims in the snapshot the strategist saw


@dataclass
class TurnAnalyzer:
    """Runs the fast analysis graph, one run at a time per session, in submission order.

    `submit` chains each new run behind the session's previous one (not just behind a lock),
    so segment k+1 can never overtake segment k, even if the event loop schedules the tasks
    differently. Different sessions run concurrently.
    """

    deps: BrainDeps
    graph: Any = None
    callbacks: Sequence[Any] = ()
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _tasks: dict[str, set[asyncio.Task]] = field(default_factory=dict)
    _tails: dict[str, asyncio.Task] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.graph is None:
            s = self.deps.settings
            self.graph = compile_fast_graph(
                max_attempts=s.graph_retry_max_attempts,
                initial_interval_s=s.graph_retry_initial_interval_s,
            )

    def _config(self, session_id: str) -> dict[str, Any]:
        cfg: dict[str, Any] = {"configurable": {"thread_id": session_id}}
        if self.callbacks:
            cfg["callbacks"] = list(self.callbacks)
        return cfg

    async def analyse(
        self,
        *,
        session_id: str,
        turn_id: str,
        turn_index: int,
        question: str,
        segment: Segment,
        ctx: StrategyContext,
        ledger: LedgerSource = (),
        submitted_at: float | None = None,
    ) -> AnalysisResult:
        """Run the fast graph for one segment. `ledger` may be a callable: it is then read
        when the run actually starts (after any queueing), so the snapshot is fresh."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            started = time.monotonic()
            snapshot = list(ledger() if callable(ledger) else ledger)
            # Each run only adds the timing/fallback entries of this run; remember where
            # this run's entries start so the result reports only them.
            before = await self._counts(session_id)
            state = await self.graph.ainvoke(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "turn_index": turn_index,
                    "question": question,
                    "segment": segment,
                    "ctx": ctx,
                    "ledger": snapshot,
                },
                self._config(session_id),
                context=self.deps,
            )
            ended = time.monotonic()
        return AnalysisResult(
            session_id=session_id,
            turn_id=turn_id,
            segment_id=segment.segment_id,
            segment_index=segment.segment_index,
            analysis=state["analyses"][-1],
            move=state["move"],
            fallbacks=state.get("fallbacks", [])[before["fallbacks"] :],
            timings=state.get("timings", [])[before["timings"] :],
            queued_ms=_ms(submitted_at, started) if submitted_at is not None else 0.0,
            fast_graph_ms=_ms(started, ended),
            ledger_size=len(snapshot),
        )

    async def _counts(self, session_id: str) -> dict[str, int]:
        snap = await self.graph.aget_state(self._config(session_id))
        values = snap.values if snap else {}
        return {k: len(values.get(k, []) or []) for k in ("timings", "fallbacks")}

    def submit(self, **kwargs: Any) -> asyncio.Task[AnalysisResult]:
        """Queue an analysis behind the session's previous one and return its task."""
        sid = kwargs["session_id"]
        prev = self._tails.get(sid)
        task = asyncio.create_task(self._after(prev, time.monotonic(), kwargs))
        self._tails[sid] = task
        tasks = self._tasks.setdefault(sid, set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _after(
        self, prev: asyncio.Task | None, submitted_at: float, kwargs: dict[str, Any]
    ) -> AnalysisResult:
        if prev is not None and not prev.done():
            await asyncio.wait([prev])  # never raises; the previous run's errors are its own
        return await self.analyse(submitted_at=submitted_at, **kwargs)

    async def close_session(self, session_id: str) -> None:
        """Cancel outstanding work and drop the session's checkpoint thread."""
        for task in list(self._tasks.pop(session_id, set())):
            task.cancel()
        self._tails.pop(session_id, None)
        checkpointer = getattr(self.graph, "checkpointer", None)
        if checkpointer is not None and hasattr(checkpointer, "adelete_thread"):
            await checkpointer.adelete_thread(session_id)
        self._locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# Running the fact-check graph
# ---------------------------------------------------------------------------


@dataclass
class FactCheckResult:
    """Gated claims for one segment, for the session to upsert into its ledger."""

    turn_id: str
    segment_id: str
    claims: list[Claim]
    fallbacks: list[dict[str, str]]
    timings: list[dict[str, Any]]
    fact_check_ms: float
    trace_tag: str


def fact_check_tag(turn_id: str, segment_id: str) -> str:
    """Trace tag for one fact-check run's librarian calls. Separate from the turn's own tag,
    because the run usually ends after the turn's trace line has been written."""
    return f"fc:{turn_id}.{segment_id}"


@dataclass
class FactChecker:
    """Runs the stateless fact-check graph. Safe to call concurrently for any sessions."""

    deps: BrainDeps
    graph: Any = None
    callbacks: Sequence[Any] = ()

    def __post_init__(self) -> None:
        if self.graph is None:
            s = self.deps.settings
            self.graph = compile_factcheck_graph(
                max_attempts=s.graph_retry_max_attempts,
                initial_interval_s=s.graph_retry_initial_interval_s,
            )

    async def check(
        self, claims: Sequence[Claim], *, turn_id: str, segment_id: str
    ) -> FactCheckResult:
        """Never raises (except cancellation): a failure of the whole run makes every claim
        unverifiable, which can never be challenged."""
        tag = fact_check_tag(turn_id, segment_id)
        start = time.monotonic()
        cfg: dict[str, Any] = {"callbacks": list(self.callbacks)} if self.callbacks else {}
        try:
            out = await self.graph.ainvoke(
                {
                    "turn_id": turn_id,
                    "segment_id": segment_id,
                    "claims": list(claims),
                    "card_set_version": self.deps.cards.version,
                    "trace_tag": tag,
                },
                cfg,
                context=self.deps,
            )
            gated = list(out.get("gated") or [])
            fallbacks = list(out.get("fallbacks") or [])
            timings = list(out.get("timings") or [])
        except Exception as exc:
            gated = [
                c.model_copy(
                    update={"verdict": Verdict.unverifiable, "verdict_note": "fact-check error"}
                )
                for c in claims
            ]
            fallbacks, timings = [_fallback("fact_check", turn_id, exc)], []
        return FactCheckResult(
            turn_id=turn_id,
            segment_id=segment_id,
            claims=gated,
            fallbacks=fallbacks,
            timings=timings,
            fact_check_ms=_ms(start, time.monotonic()),
            trace_tag=tag,
        )


# ---------------------------------------------------------------------------
# Report graph
# ---------------------------------------------------------------------------


def _make_judge(max_attempts: int) -> Callable:
    async def judge(state: ReportState, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
        deps = runtime.context
        failed = state.get("failed") or []
        revisions = state.get("revisions", 0) + (1 if failed else 0)
        try:
            draft = await run_judge(
                deps.llm,
                deps.kit,
                state.get("turns", []),
                state.get("ledger", []),
                state.get("language", ReportLanguage.en),
                session_id=state["session_id"],
                failed=failed,
                max_tokens=deps.settings.judge_max_tokens,
            )
            return {"draft": draft, "revisions": revisions}
        except Exception as exc:
            if _should_reraise(exc, runtime, max_attempts):
                raise
            # A failed revision keeps the earlier draft; a failed first draft has none.
            return {
                "error": f"{type(exc).__name__}",
                "revisions": revisions,
                "failed": [] if state.get("draft") else failed,
            }

    return judge


async def citation_check(state: ReportState) -> dict[str, Any]:
    draft = state.get("draft")
    if draft is None:
        return {"failed": []}
    return {"failed": check_citations(draft.evidence, state.get("turns", []))}


def route_after_check(state: ReportState) -> str:
    if state.get("draft") is None or state.get("error"):
        return "report"
    if state.get("failed") and state.get("revisions", 0) < MAX_JUDGE_REVISIONS:
        return "judge"
    return "report"


async def report(state: ReportState, runtime: Runtime[BrainDeps]) -> dict[str, Any]:
    deps = runtime.context
    s = deps.settings
    language = state.get("language", ReportLanguage.en)
    common = dict(
        session_id=state["session_id"],
        ledger=state.get("ledger", []),
        moves=state.get("moves", []),
        language=language,
        feature_flags=s.feature_flags(),
        prompt_versions=s.prompt_versions(),
    )
    draft = state.get("draft")
    if draft is None:
        return {"report": fallback_report(reason=state.get("error") or "no draft", **common)}
    try:
        rep = assemble_report(
            draft,
            turns=state.get("turns", []),
            cards=deps.cards,
            revisions=state.get("revisions", 0),
            **common,
        )
    except (ValueError, LLMError) as exc:
        rep = fallback_report(reason=f"assembly failed: {type(exc).__name__}", **common)
    return {"report": rep}


def build_report_graph(*, max_attempts: int = 3, initial_interval_s: float = 0.5) -> StateGraph:
    g = StateGraph(ReportState, context_schema=BrainDeps)
    g.add_node(
        "judge",
        _make_judge(max_attempts),
        retry_policy=retry_policy(max_attempts, initial_interval_s),
    )
    g.add_node("citation_check", citation_check)
    g.add_node("report", report)
    g.add_edge(START, "judge")
    g.add_edge("judge", "citation_check")
    g.add_conditional_edges("citation_check", route_after_check, ["judge", "report"])
    g.add_edge("report", END)
    return g


def compile_report_graph(
    *, max_attempts: int = 3, initial_interval_s: float = 0.5, checkpointer: Any = None
):
    # The CTO suggests a durable SQLite checkpointer so a crashed report can resume; that
    # needs the separate `langgraph-checkpoint-sqlite` package, which is not installed yet.
    return build_report_graph(
        max_attempts=max_attempts, initial_interval_s=initial_interval_s
    ).compile(checkpointer=checkpointer, name="session_report")


async def generate_report(
    deps: BrainDeps,
    *,
    session_id: str,
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    moves: Sequence[BoardMove],
    language: ReportLanguage | str | None = None,
    graph: Any = None,
    callbacks: Sequence[Any] = (),
) -> SessionReport:
    """Run the post-session graph and return the validated report."""
    s = deps.settings
    graph = graph or compile_report_graph(
        max_attempts=s.graph_retry_max_attempts, initial_interval_s=s.graph_retry_initial_interval_s
    )
    lang = ReportLanguage(language or s.report_language)
    cfg: dict[str, Any] = {"callbacks": list(callbacks)} if callbacks else {}
    out = await graph.ainvoke(
        {
            "session_id": session_id,
            "language": lang,
            "turns": list(turns),
            "ledger": list(ledger),
            "moves": list(moves),
            "revisions": 0,
            "failed": [],
        },
        cfg,
        context=deps,
    )
    return out["report"]
