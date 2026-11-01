"""Latency budget and turn timing of the session API (CTO_NOTES_02 M4, orchestrator ruling 9).

Fake LLM clients inject delays (or block on an event until the test releases them), so these
tests check the STRUCTURE of the hot path, not Nova's speed:

* `end_of_turn` returns within its deadline even while fact checks are slow;
* a slow fact check never delays a later move;
* a slow listener past the deadline yields the deterministic fallback move, recorded as
  `fallback_reason` in the trace (or the move after the latest finished segment);
* verdicts produced during turn N influence the move on turn N+1, never turn N;
* segments of one session are analysed in segment order, one at a time.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import pytest

from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.schemas import Move, Verdict
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

CLAIMY = "Tourism is 30 percent of Testland GDP in 2023, so jobs depend on it heavily."
NO_CLAIMS = "I think tourism creates jobs but it can also damage fragile places, so balance it."
MORE = "Local guides need training too, and waste rules must be enforced, otherwise it fails."

DEADLINE_S = 0.3
# Generous slack for a loaded CI machine; the fake calls themselves take milliseconds.
SLACK_S = 0.25


@dataclass
class Call:
    name: str
    user: str
    start: float
    end: float | None = None


@dataclass
class SlowFake:
    """Wraps a rule-based FakeLLMClient: per call name, an optional delay (a number, or a
    function of the user prompt) and an optional gate (an event the call waits for)."""

    llm: FakeLLMClient
    delays: dict[str, float | Callable[[str], float]] = field(default_factory=dict)
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        inner = self.llm._tool_call

        async def _tool_call(system, user, tool, max_tokens, stats):
            call = Call(stats.name, user, time.monotonic())
            self.calls.append(call)
            gate = self.gates.get(stats.name)
            if gate is not None:
                await gate.wait()
            delay = self.delays.get(stats.name, 0)
            delay = delay(user) if callable(delay) else delay
            if delay:
                await asyncio.sleep(delay)
            try:
                return await inner(system, user, tool, max_tokens, stats)
            finally:
                call.end = time.monotonic()

        self.llm._tool_call = _tool_call

    def named(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    def release(self) -> None:
        for gate in self.gates.values():
            gate.set()


def slow_fake(cards, **kw) -> SlowFake:
    return SlowFake(make_fake_llm(cards), **kw)


@dataclass
class Board:
    session: InterviewSession
    fake: SlowFake
    analyzer: TurnAnalyzer

    async def turn(self, *segments: str, deadline_s: float = DEADLINE_S):
        """One candidate turn through the public API; returns (move, seconds, outcome)."""
        for i, text in enumerate(segments):
            self.session.submit_segment(text, segment_index=i)
        t = time.monotonic()
        move = await self.session.end_of_turn(deadline_s)
        elapsed = time.monotonic() - t
        outcome = await self.session.speak()
        return move, elapsed, outcome

    async def close(self) -> None:
        self.fake.release()
        await self.session.drain_fact_checks(5.0)
        await self.analyzer.close_session(self.session.session_id)


async def board(deps, form, tmp_path, fake: SlowFake, sid: str = "lat") -> Board:
    d = replace(deps, llm=fake.llm)
    analyzer = TurnAnalyzer(d)
    session = InterviewSession(
        deps=d,
        form=form,
        recorder=TraceRecorder(tmp_path, sid),
        analyzer=analyzer,
        session_id=sid,
    )
    await session.open()
    return Board(session, fake, analyzer)


def contradicted(session: InterviewSession) -> list[str]:
    return [c.id for c in session.state.claim_ledger if c.verdict == Verdict.contradicted]


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


async def test_end_of_turn_meets_the_deadline_while_fact_checks_are_slow(
    deps, form, tmp_path, cards
):
    fake = slow_fake(cards, gates={"librarian": asyncio.Event()})  # blocked until released
    b = await board(deps, form, tmp_path, fake)
    try:
        b.session.submit_segment(CLAIMY, segment_index=0)
        t = time.monotonic()
        move = await b.session.end_of_turn(DEADLINE_S)
        elapsed = time.monotonic() - t
        assert elapsed < DEADLINE_S  # the listener is fast; nothing waits for the librarian
        assert move.rule_id != "FALLBACK"
        assert not any(c.end for c in fake.named("librarian"))  # no verdict yet
        assert b.session.pending_fact_checks == 1
        out = await b.session.speak()
        assert out.trace["fallback_reason"] is None
        assert [c["verdict"] for c in out.trace["claims"]] == ["pending"]
        assert out.trace["fact_checks_pending"] == 1
        # Releasing the librarian later still lands the verdict in the ledger.
        fake.release()
        assert await b.session.settle_fact_checks(2.0)
        assert contradicted(b.session) == ["t0.s0.c0"]
    finally:
        await b.close()


async def test_a_slow_fact_check_never_delays_a_later_move(deps, form, tmp_path, cards):
    fake = slow_fake(cards, gates={"librarian": asyncio.Event()})
    b = await board(deps, form, tmp_path, fake)
    try:
        await b.turn(CLAIMY)  # its fact check stays blocked for the whole test
        move, elapsed, out = await b.turn(NO_CLAIMS)
        assert elapsed < DEADLINE_S and move.rule_id != "FALLBACK"
        assert move.move != Move.fact_challenge  # no verdict yet, so nothing to challenge
        assert b.session.pending_fact_checks == 1
        assert out.trace["move_wait_ms"] < DEADLINE_S * 1000
        assert out.trace["fallback_reason"] is None
    finally:
        await b.close()


async def test_a_slow_listener_past_the_deadline_yields_the_fallback_move(
    deps, form, tmp_path, cards
):
    fake = slow_fake(cards, gates={"listener": asyncio.Event()})
    b = await board(deps, form, tmp_path, fake)
    try:
        b.session.submit_segment(CLAIMY, segment_index=0)
        t = time.monotonic()
        move = await b.session.end_of_turn(DEADLINE_S)
        elapsed = time.monotonic() - t
        assert DEADLINE_S <= elapsed < DEADLINE_S + SLACK_S
        assert (move.move, move.rule_id) == (Move.probe, "FALLBACK")  # a neutral probe
        assert move.for_turn == "t0"
        out = await b.session.speak()
        assert out.fallback_reason and "deadline" in out.trace["fallback_reason"]
        assert out.trace["deadline_s"] == DEADLINE_S
        assert out.line.text.strip()  # the board still says something
        # The late analysis still finishes and its claim is still fact-checked, but it
        # never replaces the move that was already spoken.
        fake.release()
        await asyncio.sleep(0.05)
        assert await b.session.settle_fact_checks(2.0)
        assert contradicted(b.session) == ["t0.s0.c0"]
        assert b.session.state.moves[-1].rule_id == "FALLBACK"
    finally:
        await b.close()


async def test_silent_turn_past_the_deadline_falls_back_to_reassure(deps, form, tmp_path, cards):
    fake = slow_fake(cards, gates={"listener": asyncio.Event()})
    b = await board(deps, form, tmp_path, fake)
    try:
        b.session.submit_segment("   ", segment_index=0)  # a segment with no words
        move = await b.session.end_of_turn(DEADLINE_S)
        assert (move.move, move.rule_id) == (Move.reassure, "FALLBACK")
    finally:
        await b.close()


async def test_deadline_uses_the_move_after_the_latest_finished_segment(
    deps, form, tmp_path, cards
):
    gate = asyncio.Event()
    fake = slow_fake(cards)
    inner = fake.llm._tool_call

    async def listener_blocks_on_second_segment(system, user, tool, max_tokens, stats):
        if stats.name == "listener" and "segment 1" in user:
            await gate.wait()
        return await inner(system, user, tool, max_tokens, stats)

    fake.llm._tool_call = listener_blocks_on_second_segment
    fake.gates["_s1"] = gate  # released by Board.close
    b = await board(deps, form, tmp_path, fake)
    try:
        b.session.submit_segment(NO_CLAIMS, segment_index=0)
        b.session.submit_segment(MORE, segment_index=1)
        move = await b.session.end_of_turn(DEADLINE_S)
        assert move.rule_id != "FALLBACK"  # the strategist's real move after s0
        reason = b.session.turn.fallback_reason
        assert reason and "1 of 2" in reason and "after s0" in reason
        out = await b.session.speak()
        assert set(out.trace["fast_graph_ms"]) == {"s0"}
    finally:
        await b.close()


async def test_no_segment_is_silence_and_needs_no_llm(deps, form, tmp_path, cards):
    fake = slow_fake(cards)
    b = await board(deps, form, tmp_path, fake)
    try:
        move, elapsed, out = await b.turn()
        assert move.move == Move.reassure and move.rule_id == "R3"
        assert elapsed < DEADLINE_S  # nothing to wait for
        assert out.fallback_reason is None and fake.named("listener") == []
    finally:
        await b.close()


# ---------------------------------------------------------------------------
# Turn N's verdicts reach turn N+1, never turn N
# ---------------------------------------------------------------------------


async def test_verdicts_of_turn_n_influence_turn_n_plus_1_and_never_turn_n(
    deps, form, tmp_path, cards
):
    fake = slow_fake(cards)
    b = await board(deps, form, tmp_path, fake)
    try:
        # The claim is made on turn 1 (the Chairperson's last question), so turn 2 is the first
        # turn a challenge can come on: a spent budget hands off before any strategist rule.
        await b.turn(NO_CLAIMS)
        b.session.submit_segment(CLAIMY, segment_index=0)
        # Let the fact check finish BEFORE turn 1 ends: the verdict exists during turn 1.
        await asyncio.sleep(0.05)
        assert await b.session.settle_fact_checks(2.0)
        assert contradicted(b.session) == ["t1.s0.c0"]
        move1 = await b.session.end_of_turn(DEADLINE_S)
        await b.session.speak()
        assert move1.move != Move.fact_challenge

        move2, _, _ = await b.turn(NO_CLAIMS)
        assert (move2.move, move2.target) == (Move.fact_challenge, "t1.s0.c0")
    finally:
        await b.close()


async def test_a_verdict_landing_mid_turn_waits_for_the_next_turn(deps, form, tmp_path, cards):
    """The ledger snapshot is frozen when a turn opens, so a verdict that lands while turn N
    is in progress (even for an earlier turn's claim) first counts on turn N+1. The move
    never depends on how fast a fact check happened to be."""
    fake = slow_fake(cards, gates={"librarian": asyncio.Event()})
    b = await board(deps, form, tmp_path, fake)
    try:
        await b.turn(CLAIMY)  # turn 0: fact check blocked
        b.session.submit_segment(NO_CLAIMS, segment_index=0)  # turn 1 opens: no verdict yet
        fake.release()
        assert await b.session.settle_fact_checks(2.0)
        assert contradicted(b.session) == ["t0.s0.c0"]  # the verdict landed mid-turn 1
        b.session.submit_segment(MORE, segment_index=1)
        move1 = await b.session.end_of_turn(DEADLINE_S)
        await b.session.speak()
        assert move1.move != Move.fact_challenge

        move2, _, _ = await b.turn(NO_CLAIMS)
        assert (move2.move, move2.target) == (Move.fact_challenge, "t0.s0.c0")
    finally:
        await b.close()


# ---------------------------------------------------------------------------
# Segment ordering
# ---------------------------------------------------------------------------


async def test_segments_are_analysed_in_order_one_at_a_time(deps, form, tmp_path, cards):
    """Segment 0's listener is slow and segment 1's is instant: segment 1 still waits."""
    fake = slow_fake(cards, delays={"listener": lambda user: 0.15 if "segment 0" in user else 0})
    b = await board(deps, form, tmp_path, fake)
    try:
        t0 = b.session.submit_segment(NO_CLAIMS, segment_index=0)
        t1 = b.session.submit_segment(MORE, segment_index=1)
        move = await b.session.end_of_turn(5.0)
        first, second = fake.named("listener")
        assert "segment 0" in first.user and "segment 1" in second.user
        assert second.start >= first.end  # never overlapping on the session's thread
        r0, r1 = t0.result(), t1.result()
        assert (r0.segment_index, r1.segment_index) == (0, 1)
        assert r1.queued_ms >= 100  # it queued behind segment 0
        assert move == r1.move  # the move after the LAST segment
        cfg = {"configurable": {"thread_id": b.session.session_id}}
        state = (await b.analyzer.graph.aget_state(cfg)).values
        assert [a.segment_id for a in state["analyses"]] == ["s0", "s1"]
        await b.session.speak()
    finally:
        await b.close()


async def test_sessions_do_not_queue_behind_each_other(deps, form, tmp_path, cards):
    slow = slow_fake(cards, gates={"listener": asyncio.Event()})
    a = await board(deps, form, tmp_path, slow, sid="A")
    fast = slow_fake(cards)
    b = await board(deps, form, tmp_path, fast, sid="B")
    try:
        a.session.submit_segment(NO_CLAIMS, segment_index=0)  # blocked
        move, elapsed, _ = await b.turn(NO_CLAIMS)
        assert elapsed < DEADLINE_S and move.rule_id != "FALLBACK"
    finally:
        await a.close()
        await b.close()


@pytest.mark.parametrize("index", [0, 1])
async def test_out_of_order_or_duplicate_segment_is_refused(deps, form, tmp_path, cards, index):
    b = await board(deps, form, tmp_path, slow_fake(cards))
    try:
        b.session.submit_segment(NO_CLAIMS, segment_index=1)
        with pytest.raises(ValueError, match="increasing order"):
            b.session.submit_segment(MORE, segment_index=index)
    finally:
        await b.close()


async def test_turn_protocol_is_enforced(deps, form, tmp_path, cards):
    b = await board(deps, form, tmp_path, slow_fake(cards))
    try:
        with pytest.raises(RuntimeError, match="end_of_turn"):
            await b.session.speak()
        b.session.submit_segment(NO_CLAIMS, segment_index=0)
        move = await b.session.end_of_turn(DEADLINE_S)
        assert await b.session.end_of_turn(DEADLINE_S) == move  # idempotent until speak()
        with pytest.raises(RuntimeError, match="speak"):
            b.session.submit_segment(MORE, segment_index=1)
        await b.session.speak()
        assert b.session.turn is None
    finally:
        await b.close()
