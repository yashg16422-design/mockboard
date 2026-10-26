"""Execution lanes for Bedrock calls (CTO_NOTES_02 M5).

The real `BedrockNovaClient` runs on a fake boto3 client whose calls BLOCK their thread (as a
slow Bedrock call does), so these tests exercise the real thread pools:

* every call runs on its own lane's threads;
* a saturated background lane (slow fact checks) does not delay the listener or the actor;
* the same load on ONE shared pool does delay them (the control that proves the test bites);
* lane pools, timeouts and connection pools come from settings.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from pydantic import ValidationError

from conftest import make_claim, seg
from fake_bedrock import FakeBedrockRuntime
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.librarian import ask_librarian, claim_cache_key
from mockboard.brain.listener import analyse_segment
from mockboard.brain.llm import (
    BedrockNovaClient,
    CallStats,
    Lane,
    LaneConfig,
    lane_for,
)
from mockboard.config import Settings
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

SLOW_S = 0.4
FAST_ENOUGH_S = 0.2
CLAIMY = "Tourism is 30 percent of Testland GDP in 2023, so jobs depend on it heavily."


def nova(stub, workers: int = 2) -> BedrockNovaClient:
    lanes = {lane: LaneConfig(workers, 1.0, 1.0) for lane in Lane}
    return BedrockNovaClient(
        region="ap-southeast-2", model_id="amazon.nova-pro-v1:0", client=stub, lanes=lanes
    )


def slow_librarian(cards) -> FakeBedrockRuntime:
    return FakeBedrockRuntime(
        cards, delay=lambda name, user: SLOW_S if name == "librarian" else 0.0
    )


def fact_checks(llm, deps, n: int) -> list[asyncio.Task]:
    claims = [make_claim(f"t0.s0.c{i}", value=30 + i) for i in range(n)]
    return [
        asyncio.create_task(
            ask_librarian(llm, deps.kit, c, claim_key=claim_cache_key(c, deps.cards.version))
        )
        for c in claims
    ]


async def listener_and_actor(llm, deps) -> tuple[float, float]:
    """Seconds taken by one listener call and one streamed actor line."""
    t = time.monotonic()
    await analyse_segment(
        llm, deps.kit, segment=seg("Tourism helps jobs."), question="q", turn_id="t1", turn_index=1
    )
    listener_s = time.monotonic() - t
    t = time.monotonic()
    chunks = [
        c
        async for c in llm.stream_text(
            name="actor", system=deps.kit.system("actor", max_words="30"), user="x", max_tokens=50
        )
    ]
    assert chunks
    return listener_s, time.monotonic() - t


def test_calls_are_assigned_to_lanes_by_name():
    assert lane_for("actor") == lane_for("opener") == Lane.actor
    assert lane_for("listener") == Lane.fast
    assert lane_for("librarian") == lane_for("judge") == Lane.background
    assert lane_for("something_new") == Lane.background  # never the critical path by accident
    assert CallStats(name="listener", model_id="m").as_dict()["lane"] == "fast"


async def test_each_lane_runs_on_its_own_threads(deps, cards):
    stub = FakeBedrockRuntime(cards)
    llm = nova(stub)
    await asyncio.gather(*fact_checks(llm, deps, 1))
    await listener_and_actor(llm, deps)
    threads = {c.name: c.thread for c in stub.calls}
    assert threads["librarian"].startswith("bedrock-background")
    assert threads["listener"].startswith("bedrock-fast")
    assert threads["actor"].startswith("bedrock-actor")


async def test_a_saturated_background_lane_does_not_delay_the_listener_or_the_actor(deps, cards):
    stub = slow_librarian(cards)
    llm = nova(stub, workers=2)
    started = time.monotonic()
    background = fact_checks(llm, deps, 6)  # 6 slow calls on 2 threads: 3 waves
    await asyncio.sleep(0.05)  # let them take both background threads
    listener_s, actor_s = await listener_and_actor(llm, deps)
    assert listener_s < FAST_ENOUGH_S
    assert actor_s < FAST_ENOUGH_S
    await asyncio.gather(*background)
    # The background lane really was saturated: its last call waited for two earlier waves.
    assert max(c.end for c in stub.named("librarian")) - started >= 3 * SLOW_S - 0.05


async def test_control_one_shared_pool_does_delay_them(deps, cards):
    """Same load, but every lane on one 2-thread pool: the hot path queues behind fact checks.
    This is the priority inversion the lanes remove, and proves the test above can fail."""
    llm = nova(slow_librarian(cards), workers=2)
    shared = ThreadPoolExecutor(max_workers=2)
    llm._pools = dict.fromkeys(Lane, shared)
    background = fact_checks(llm, deps, 6)
    await asyncio.sleep(0.05)
    listener_s, _ = await listener_and_actor(llm, deps)
    assert listener_s >= SLOW_S
    await asyncio.gather(*background)
    shared.shutdown()


async def test_session_moves_and_speaks_on_time_while_fact_checks_saturate(
    deps, form, cards, tmp_path
):
    llm = nova(slow_librarian(cards), workers=2)
    d = replace(deps, llm=llm)
    analyzer = TurnAnalyzer(d)
    session = InterviewSession(
        deps=d, form=form, recorder=TraceRecorder(tmp_path, "lanes"), analyzer=analyzer,
        session_id="lanes",
    )
    await session.open()
    background = fact_checks(llm, d, 6)
    await asyncio.sleep(0.05)
    session.submit_segment(CLAIMY, segment_index=0)
    t = time.monotonic()
    move = await session.end_of_turn(0.3)
    move_s = time.monotonic() - t
    t = time.monotonic()
    out = await session.speak()
    speak_s = time.monotonic() - t
    assert move.rule_id != "FALLBACK" and move_s < FAST_ENOUGH_S
    assert not out.line.is_fallback and speak_s < FAST_ENOUGH_S
    assert out.trace["fact_checks_pending"] == 1  # its librarian call is queued behind
    await asyncio.gather(*background)
    await session.drain_fact_checks(5.0)
    await analyzer.close_session(session.session_id)


def test_lane_clients_take_pools_and_timeouts_from_settings(settings):
    s = settings.model_copy(
        update={
            "bedrock_actor_workers": 3,
            "bedrock_fast_workers": 5,
            "bedrock_background_workers": 7,
            "bedrock_max_pool_connections": 12,
        }
    )
    llm = BedrockNovaClient.from_settings(s)
    workers = {lane: llm.pool_for(lane)._max_workers for lane in Lane}
    assert workers == {Lane.actor: 3, Lane.fast: 5, Lane.background: 7}
    clients = {lane: llm.client_for(lane) for lane in Lane}
    assert len({id(c) for c in clients.values()}) == 3  # one boto3 client per lane
    for client in clients.values():
        assert client.meta.config.max_pool_connections == 12
    bg = clients[Lane.background].meta.config
    assert (bg.connect_timeout, bg.read_timeout) == (
        s.bedrock_connect_timeout_s,
        s.bedrock_read_timeout_s,
    )
    assert bg.retries["total_max_attempts"] == 1  # the graph RetryPolicy retries, not botocore
    llm.close()


def test_pool_connections_must_cover_the_largest_lane():
    with pytest.raises(ValidationError, match="max_pool_connections"):
        Settings(_env_file=None, bedrock_background_workers=20, bedrock_max_pool_connections=16)
