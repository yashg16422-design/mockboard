"""Ruling 13: short timeouts on the hot lanes, and a hung listener never blocks the session.

* The actor and fast lanes get their own short botocore timeouts and capped retries from
  settings; the background lane keeps the long ones.
* The fast graph retries with its own, smaller cap.
* A listener call that hangs on segment 1 is abandoned after `fast_listener_timeout_s`:
  segment 2 is still analysed, the next turn's move is on time, and when the hung call
  finally returns its claims are still fact-checked.
* The end-of-turn deadline abandons a turn's unfinished analyses at once, so the next turn
  never queues behind them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace

from conftest import seg
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import BedrockNovaClient, FakeLLMClient, Lane, LLMTransientError
from mockboard.brain.schemas import Move, StrategyContext
from mockboard.session import segment_id_for
from test_latency_budget import CLAIMY, MORE, NO_CLAIMS, SlowFake, board, contradicted, slow_fake


def hang_listener_when(fake: SlowFake, hangs: Callable[[str], bool]) -> asyncio.Event:
    """Listener calls whose prompt matches `hangs` block until the returned event is set."""
    gate = asyncio.Event()
    inner = fake.llm._tool_call

    async def tool_call(system, user, tool, max_tokens, stats):
        if stats.name == "listener" and hangs(user):
            await gate.wait()
        return await inner(system, user, tool, max_tokens, stats)

    fake.llm._tool_call = tool_call
    fake.gates["_hung"] = gate  # released by Board.close
    return gate


def with_settings(deps, **values):
    return replace(deps, settings=deps.settings.model_copy(update=values))


def test_hot_lanes_get_short_timeouts_and_capped_retries_from_settings(settings):
    s = settings
    llm = BedrockNovaClient.from_settings(s)
    cfg = {lane: llm.client_for(lane).meta.config for lane in Lane}
    assert (cfg[Lane.actor].connect_timeout, cfg[Lane.actor].read_timeout) == (
        s.bedrock_actor_connect_timeout_s,
        s.bedrock_actor_read_timeout_s,
    )
    assert (cfg[Lane.fast].connect_timeout, cfg[Lane.fast].read_timeout) == (
        s.bedrock_fast_connect_timeout_s,
        s.bedrock_fast_read_timeout_s,
    )
    assert cfg[Lane.background].read_timeout == s.bedrock_read_timeout_s
    # The property the ruling asks for: hot lanes give up long before the background lane.
    for lane in (Lane.actor, Lane.fast):
        assert cfg[lane].read_timeout < cfg[Lane.background].read_timeout
    # The actor has no graph RetryPolicy, so botocore's capped retry is its only one; the
    # graph lanes keep botocore at one attempt so retries never multiply.
    assert cfg[Lane.actor].retries["total_max_attempts"] == s.bedrock_actor_max_attempts
    assert cfg[Lane.fast].retries["total_max_attempts"] == 1
    assert cfg[Lane.background].retries["total_max_attempts"] == 1
    llm.close()


async def test_fast_graph_retries_are_capped_by_their_own_setting(deps):
    llm = FakeLLMClient(script={"listener": [LLMTransientError("throttled")] * 5})
    d = with_settings(replace(deps, llm=llm), fast_graph_max_attempts=2, graph_retry_max_attempts=4)
    analyzer = TurnAnalyzer(d)
    ctx = StrategyContext(turn_id="t1", turn_index=1, topic="tourism")
    result = await analyzer.analyse(
        session_id="cap", turn_id="t1", turn_index=1, question="q", segment=seg(NO_CLAIMS), ctx=ctx
    )
    assert len(llm.calls_named("listener")) == 2  # not graph_retry_max_attempts (4)
    assert result.analysis.is_fallback and result.fallbacks[0]["stage"] == "listener"
    await analyzer.close_session("cap")


async def test_a_hung_listener_on_segment_1_does_not_stop_segment_2_or_the_next_turn(
    deps, form, tmp_path, cards
):
    fake = slow_fake(cards)
    gate = hang_listener_when(fake, lambda user: "segment 1" in user and "30 percent" in user)
    b = await board(with_settings(deps, fast_listener_timeout_s=0.15), form, tmp_path, fake)
    fake.llm.add_sink(b.session.recorder.on_call)  # record llm calls, as the CLI does
    try:
        tasks = [
            b.session.submit_segment(text, segment_index=i)
            for i, text in enumerate([NO_CLAIMS, CLAIMY, MORE])
        ]
        t = time.monotonic()
        move = await b.session.end_of_turn(2.0)
        assert time.monotonic() - t < 1.0  # gave up on segment 1 after 0.15 s, not 2 s
        s2 = [c for c in fake.named("listener") if "segment 2" in c.user]
        assert s2 and s2[0].end is not None  # segment 2 was analysed after the hang
        assert move == tasks[2].result().move and move.rule_id != "FALLBACK"
        out = await b.session.speak()
        assert out.fallback_reason is None
        abandoned = [f for f in out.trace["fallbacks"] if f["error"].startswith("abandoned")]
        assert [(f["stage"], f["segment_id"]) for f in abandoned] == [
            ("listener", segment_id_for(1))
        ]
        assert b.session.pending_late_listens == 1  # the hung call is parked, not cancelled

        # The next turn's move is on time, while segment 1's call is still hung.
        move1, elapsed, _ = await b.turn(NO_CLAIMS, deadline_s=0.3)
        assert elapsed < 0.3 and move1.rule_id != "FALLBACK"

        # The hung call finally returns: its claim is still fact-checked...
        gate.set()
        assert await b.session.settle_fact_checks(2.0)
        assert contradicted(b.session) == ["t0.s1.c0"]
        # ...recorded in the trace, and challenged on a later turn.
        move2, _, out2 = await b.turn(NO_CLAIMS)
        late = out2.trace["late_listener"]
        assert late and late[0]["segment_id"] == "s1" and late[0]["fact_checked"]
        assert late[0]["llm_calls"][0]["name"] == "listener"
        assert (move2.move, move2.target) == (Move.fact_challenge, "t0.s1.c0")
    finally:
        await b.close()


async def test_the_deadline_abandons_unfinished_analyses_so_the_next_turn_never_waits(
    deps, form, tmp_path, cards
):
    """With a long listener timeout, only the end-of-turn deadline can free the queue."""
    fake = slow_fake(cards)
    gate = hang_listener_when(fake, lambda user: "30 percent" in user)
    b = await board(with_settings(deps, fast_listener_timeout_s=30.0), form, tmp_path, fake)
    try:
        move0, _, out0 = await b.turn(CLAIMY, deadline_s=0.2)
        assert move0.rule_id == "FALLBACK" and "deadline" in out0.trace["fallback_reason"]
        move1, elapsed, _ = await b.turn(NO_CLAIMS, deadline_s=0.3)
        assert elapsed < 0.3 and move1.rule_id != "FALLBACK"  # not queued behind the hang
        gate.set()
        assert await b.session.settle_fact_checks(2.0)
        assert contradicted(b.session) == ["t0.s0.c0"]  # the late result still counted
    finally:
        await b.close()
