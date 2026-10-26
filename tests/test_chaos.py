"""Bedrock chaos test (orchestrator ruling 4 workflow item; CTO_NOTES_02 M5).

24 seeded whole interviews run on the REAL `BedrockNovaClient` (lanes, thread pools, botocore
error mapping, stream reader) over a fake boto3 client. On all three lanes it injects, at
random but reproducibly per seed, `ThrottlingException`, read timeouts and mid-stream
throttling, and it makes some listener calls hang past `fast_listener_timeout_s`. Half the
seeds also use an end-of-turn deadline shorter than that timeout, so the deadline fallback
fires too. All sessions share one vote cache, as sessions in one process do.

For every session:

1. it completes, with a report;
2. no fallback librarian vote is ever cached;
3. no fact challenge is ever issued from a fallback verdict (checked against the fake's own
   record of which librarian calls really answered, not against our code's flags);
4. every fallback is recorded in the trace, and no model call is missing from the trace.

The last test checks that the chaos really reached every lane and every fallback path, so
the invariants above are not passing vacuously.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field, replace

from conftest import answer
from fake_bedrock import MIDSTREAM, FakeBedrockRuntime, Fault, read_timeout, throttling
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.librarian import VoteCache, build_librarian_user
from mockboard.brain.llm import BedrockNovaClient, Lane, LaneConfig, lane_for
from mockboard.brain.schemas import Move, SessionReport, Verdict
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession
from test_board_invariants import ANSWERS, random_answers

SEEDS = range(24)
# Share of Bedrock calls that fail, per lane. The background lane is hit harder because the
# shared vote cache makes its calls rare, and a librarian or judge fallback needs every one
# of its graph retries to fail.
FAULT_RATE = {Lane.actor: 0.25, Lane.fast: 0.25, Lane.background: 0.6}
HANG_RATE = 0.12  # share of (otherwise healthy) listener calls that hang
LISTENER_TIMEOUT_S = 0.05
HANG_S = 0.15  # > LISTENER_TIMEOUT_S: the fast graph must abandon these calls
BACKGROUND = (
    "Kanpur mein repair shops bahut hain, lekin skilled training kam hai. ITI ke saath "
    "tie-up karke skilling badhaunga."
)


@dataclass
class Chaos:
    """A seeded fault plan. Each decision depends only on (seed, call name, prompt, attempt
    number for that prompt), never on thread timing, so a seed always injects the same
    faults and a retry of the same request can succeed."""

    seed: int
    attempts: Counter = field(default_factory=Counter)

    def _rng(self, *parts: object) -> random.Random:
        digest = hashlib.sha256("|".join(map(str, (self.seed, *parts))).encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    def fault(self, name: str, user: str) -> Fault:  # called under the fake's lock
        n = self.attempts[(name, user)]
        self.attempts[(name, user)] += 1
        rng = self._rng("fault", name, n, user)
        if rng.random() >= FAULT_RATE[lane_for(name)]:
            return None
        kinds = ["throttle", "throttle", "timeout"]
        if name in ("actor", "opener"):
            kinds.append(MIDSTREAM)
        kind = rng.choice(kinds)
        if kind == "throttle":
            return throttling("ConverseStream" if name in ("actor", "opener") else "Converse")
        return read_timeout() if kind == "timeout" else MIDSTREAM

    def delay(self, name: str, user: str) -> float:
        n = self.attempts[(name, user)] - 1  # fault() already counted this call
        if name == "listener" and self._rng("hang", n, user).random() < HANG_RATE:
            return HANG_S
        return 0.0


@dataclass
class Run:
    seed: int
    session: InterviewSession
    report: SessionReport
    stub: FakeBedrockRuntime
    lines: list[dict]
    fallback_analyses: int  # from the fast graph's own state, not from the trace
    leftover_calls: dict


async def run_session(seed: int, deps, form, tmp_path, cache: VoteCache) -> Run:
    chaos = Chaos(seed)
    stub = FakeBedrockRuntime(deps.cards, fault=chaos.fault, delay=chaos.delay)
    llm = BedrockNovaClient(
        region="ap-southeast-2",
        model_id="amazon.nova-pro-v1:0",
        client=stub,
        lanes={lane: LaneConfig(2, 1.0, 1.0) for lane in Lane},
    )
    sid = f"chaos-{seed}"
    recorder = TraceRecorder(tmp_path, sid)
    llm.add_sink(recorder.on_call)
    d = replace(
        deps,
        llm=llm,
        vote_cache=cache,
        settings=deps.settings.model_copy(update={"fast_listener_timeout_s": LISTENER_TIMEOUT_S}),
    )
    analyzer = TurnAnalyzer(d)
    session = InterviewSession(
        deps=d, form=form, recorder=recorder, analyzer=analyzer, session_id=sid
    )
    # Even seeds: a deadline below the listener timeout, so hung segments hit the deadline.
    deadline_s = 0.03 if seed % 2 == 0 else 1.0
    try:
        await session.open()
        # The shared planted claim is answered from the vote cache after its first real vote;
        # a per-seed variant makes every session ask the librarian at least once.
        variant = ANSWERS["planted"][0].replace("30 percent", f"{20 + seed} percent")
        answers = [BACKGROUND, ANSWERS["planted"][0], variant, *random_answers(seed, 6)]
        for text in answers:
            if session.state.finished:
                break
            await answer(session, text, deadline_s=deadline_s, think_s=2.0)
        assert await session.settle_fact_checks(5.0), f"seed {seed}: background work hung"
        cfg = {"configurable": {"thread_id": sid}}
        analyses = (await analyzer.graph.aget_state(cfg)).values.get("analyses", [])
        report = await session.finish()
    finally:
        llm.close()
    lines = [json.loads(x) for x in recorder.path.read_text(encoding="utf-8").splitlines()]
    return Run(
        seed=seed,
        session=session,
        report=report,
        stub=stub,
        lines=lines,
        fallback_analyses=sum(a.is_fallback for a in analyses),
        leftover_calls=dict(recorder._calls),
    )


def all_llm_calls(lines: list[dict]) -> list[dict]:
    calls = []
    for line in lines:
        calls += line.get("llm_calls", [])
        for key in ("fact_checks", "late_listener", "stale_analyses"):
            for entry in line.get(key) or []:
                calls += entry.get("llm_calls", [])
    return calls


def trace_fallbacks(lines: list[dict]) -> list[dict]:
    """Every fallback entry in the file: turn fallbacks, stale analyses, fact checks."""
    out = []
    for line in lines:
        out += line.get("fallbacks") or []
        for entry in line.get("stale_analyses") or []:
            out += entry.get("fallbacks") or []
        for entry in line.get("fact_checks") or []:
            out += [{**f, "segment_id": entry["segment_id"]} for f in entry["fallbacks"]]
    return out


def violations(run: Run, answered_librarian_prompts: set[str], cache: VoteCache) -> list[str]:
    bad: list[str] = []
    st, lines = run.session.state, run.lines
    turns = [x for x in lines if "turn_id" in x and "event" not in x]
    events = {x["event"]: x for x in lines if "event" in x}

    # 1. Completed, with a report.
    if not st.finished:
        bad.append("session did not finish")
    if not isinstance(run.report, SessionReport) or "report" not in events:
        bad.append("no report")
    if len(turns) != len(st.moves):
        bad.append(f"{len(turns)} trace lines for {len(st.moves)} moves")

    # 2. No fallback vote cached.
    if any(v.is_fallback for v in cache._data.values()):
        bad.append("a fallback vote is in the vote cache")

    # 3. No fact challenge from a fallback verdict.
    ledger = {c.id: c for c in st.claim_ledger}
    for m in st.moves:
        if m.move != Move.fact_challenge:
            continue
        c = ledger.get(m.target)
        if c is None or c.verdict != Verdict.contradicted or c.model_verdict is None:
            bad.append(f"fact challenge on {m.target} without a real contradicted verdict")
        elif build_librarian_user(c) not in answered_librarian_prompts:
            bad.append(f"fact challenge on {m.target}: no librarian call ever answered it")

    # 4a. No model call missing from the trace, and every failed call visible as an error.
    calls = all_llm_calls(lines)
    if len(calls) != len(run.stub.calls):
        bad.append(f"{len(calls)} llm calls in the trace for {len(run.stub.calls)} made")
    faulted = Counter(c.name for c in run.stub.calls if c.fault)
    errored = Counter(c["name"] for c in calls if c["error"])
    if faulted != errored:
        bad.append(f"faults {dict(faulted)} != errors in trace {dict(errored)}")
    if any(c["lane"] != lane_for(c["name"]).value for c in calls):
        bad.append("a call ran on the wrong lane")
    if run.leftover_calls:
        bad.append(f"calls never written to the trace: {list(run.leftover_calls)}")

    # 4b. Actor and opener: a failed stream is always a flagged fallback line.
    actor_calls = run.stub.named("actor")
    if len(actor_calls) != len(turns):
        bad.append(f"{len(actor_calls)} actor calls for {len(turns)} turns")
    for call, line, board in zip(actor_calls, turns, st.board_lines[1:], strict=False):
        if call.fault and not line["actor_fallback"]:
            bad.append(f"{line['turn_id']}: actor failed ({call.fault}) but no actor_fallback")
        if line["actor_fallback"] != board.is_fallback:
            bad.append(f"{line['turn_id']}: actor_fallback disagrees with the board line")
    opener = run.stub.named("opener")[0]
    if opener.fault and not events["session_start"]["opener_fallback"]:
        bad.append("opener failed but session_start has no opener_fallback")

    # 4c. Listener: one trace entry per fallback analysis in the fast graph's state.
    fbs = trace_fallbacks(lines)
    listener_fbs = [f for f in fbs if f["stage"] == "listener"]
    if len(listener_fbs) != run.fallback_analyses:
        bad.append(
            f"{run.fallback_analyses} fallback analyses but {len(listener_fbs)} in the trace"
        )

    # 4d. Librarian: every claim gated from a fallback vote has a trace entry for its segment.
    lib_fbs = {(f["turn_id"], f["segment_id"]) for f in fbs if f["stage"] == "librarian"}
    for c in st.claim_ledger:
        if c.verdict_note.startswith("librarian unavailable") and (
            (c.turn_id, c.segment_id) not in lib_fbs
        ):
            bad.append(f"claim {c.id}: librarian fallback not in the trace")

    # 4e. Deadline fallback moves and the report.
    for m, line in zip(st.moves, turns, strict=False):
        if m.rule_id == "FALLBACK" and not line["fallback_reason"]:
            bad.append(f"{line['turn_id']}: fallback move without fallback_reason")
    if events.get("report", {}).get("is_fallback") != run.report.is_fallback:
        bad.append("report fallback not recorded in the report event")
    return [f"seed {run.seed}: {b}" for b in bad]


async def test_chaos_sessions_complete_safely_and_record_every_fallback(
    deps, form, tmp_path, capsys
):
    cache = VoteCache()
    answered: set[str] = set()
    problems: list[str] = []
    seen = Counter()
    for seed in SEEDS:
        run = await run_session(seed, deps, form, tmp_path, cache)
        answered |= {c.user for c in run.stub.named("librarian") if c.fault is None}
        problems += violations(run, answered, cache)
        # What the chaos reached, for the coverage test below.
        seen.update(f"fault:{lane_for(c.name).value}" for c in run.stub.calls if c.fault)
        seen.update(f"fallback:{f['stage']}" for f in trace_fallbacks(run.lines))
        seen["abandoned"] += sum(
            f["error"].startswith("abandoned") for f in trace_fallbacks(run.lines)
        )
        st = run.session.state
        seen["deadline_fallback_move"] += sum(m.rule_id == "FALLBACK" for m in st.moves)
        seen["actor_fallback"] += sum(b.is_fallback for b in st.board_lines[1:])
        seen["fact_challenge"] += sum(m.move == Move.fact_challenge for m in st.moves)
        seen["report_fallback"] += run.report.is_fallback
    assert problems == []
    with capsys.disabled():
        print(f"\nchaos coverage over {len(SEEDS)} sessions: {dict(sorted(seen.items()))}")
    # The chaos reached every lane and every fallback path (no vacuous pass).
    for key in (
        "fault:actor",
        "fault:fast",
        "fault:background",
        "fallback:listener",
        "fallback:librarian",
        "abandoned",
        "deadline_fallback_move",
        "actor_fallback",
        "fact_challenge",
        "report_fallback",
    ):
        assert seen[key] > 0, f"chaos never produced {key}: {dict(seen)}"
