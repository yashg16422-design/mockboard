"""Ruling 14: the actor gets one attempt; a failed or late actor call is answered with a cached
fallback line, and the trace records it.

* Each persona YAML carries two or three short Hinglish fallback lines.
* A failed actor call is never retried: the speaking member's fallback line is said instead.
* An actor call with no first token inside `actor_first_token_budget_s` is abandoned on time,
  with the same fallback (the real Bedrock client and lane threads, over a fake boto3).
* A fact challenge and a topic change keep their own deterministic templates, because the
  session's state changes with them and the candidate must hear it.
* In a whole session the trace line says what happened, and the next turn carries on.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace

import pytest

from conftest import make_claim
from fake_bedrock import FakeBedrockRuntime
from mockboard.brain.actor import (
    FILLER_TEMPLATES,
    compose_line,
    fact_challenge_template,
    persona_fallback_line,
)
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import (
    BedrockNovaClient,
    CallStats,
    FakeLLMClient,
    Lane,
    LaneConfig,
    LLMTransientError,
)
from mockboard.brain.personas import load_personas
from mockboard.brain.schemas import BoardMove, Move
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

NEVER = "THIS SECOND REPLY MUST NEVER BE SPOKEN, BECAUSE THE ACTOR IS NOT RETRIED?"


def probe_move(member: str = "economist") -> BoardMove:
    return BoardMove(move=Move.probe, member=member, reason="R8: test")


async def compose(deps, form, llm, move, *, budget_s=None, claim=None, card=None, turn_id="t3"):
    return await compose_line(
        llm, deps.kit, deps.personas[move.member], move, topic="tourism", form=form,
        last_question="q", last_answer="a", max_words=30, max_tokens=120, turn_id=turn_id,
        claim=claim, card=card, first_token_budget_s=budget_s,
    )


def test_every_persona_has_two_or_three_short_fallback_lines():
    for persona in load_personas().values():
        lines = persona.fallback_lines
        assert 2 <= len(lines) <= 3, persona.id
        assert len(set(lines)) == len(lines), persona.id
        for line in lines:
            assert len(line.split()) <= 15, line  # short: a pause, not a speech
            assert not re.search(r"\d", line), line  # never a figure from nowhere
            assert line[0].isupper() and line[-1] in ".?", line


def test_the_line_is_stable_per_turn_and_varies_across_turns(deps):
    persona = deps.personas["chairman"]
    assert persona_fallback_line(persona, "t4") == persona_fallback_line(persona, "t4")
    seen = {persona_fallback_line(persona, f"t{i}") for i in range(12)}
    assert seen == set(persona.fallback_lines)
    bare = persona.model_copy(update={"fallback_lines": []})
    assert persona_fallback_line(bare, "t1") == FILLER_TEMPLATES[Move.probe]


async def test_a_failed_actor_call_is_not_retried_and_the_persona_line_is_spoken(deps, form):
    llm = FakeLLMClient(script={"actor": [LLMTransientError("throttled"), NEVER]})
    line = await compose(deps, form, llm, probe_move("ips_officer"))
    assert len(llm.calls_named("actor")) == 1  # one attempt, no retry
    assert line.text in deps.personas["ips_officer"].fallback_lines
    assert line.is_fallback and line.guard == "actor_failed"
    assert line.fallback_kind == "persona_line" and "throttled" in line.error


async def test_an_acknowledgement_still_comes_before_the_fallback_line(deps, form):
    move = probe_move().model_copy(update={"preface": "acknowledge"})
    llm = FakeLLMClient(script={"actor": [LLMTransientError("throttled")]})
    line = await compose(deps, form, llm, move)
    head, _, tail = line.text.partition(". ")
    assert head.startswith("Theek hai") and tail in deps.personas["economist"].fallback_lines


async def test_fact_challenge_and_move_on_keep_their_own_templates(deps, form):
    card = deps.cards.get("builtin_tourism_gdp_share")
    challenge = BoardMove(
        move=Move.fact_challenge, target="t0.s0.c0", member="economist", reason="R4: test"
    )
    llm = FakeLLMClient(script={"actor": [LLMTransientError("x"), LLMTransientError("y")]})
    line = await compose(deps, form, llm, challenge, claim=make_claim(), card=card)
    assert line.text == fact_challenge_template(make_claim(), card)
    assert line.fallback_kind == "challenge_template" and line.is_fallback

    move_on = BoardMove(move=Move.move_on, target="internal_security", member="chairman",
                        reason="R6: test")
    line = await compose(deps, form, llm, move_on)
    assert line.text == FILLER_TEMPLATES[Move.move_on] and line.fallback_kind == "move_template"


def bedrock_with_slow_actor(cards, actor_delay_s: float, calls: list[CallStats]):
    stub = FakeBedrockRuntime(
        cards, delay=lambda name, user: actor_delay_s if name == "actor" else 0.0
    )
    llm = BedrockNovaClient(
        region="ap-southeast-2",
        model_id="amazon.nova-pro-v1:0",
        client=stub,
        lanes={lane: LaneConfig(2, 1.0, 1.0) for lane in Lane},
        on_call=calls.append,
    )
    return llm, stub


@pytest.mark.parametrize("actor_delay_s, late", [(0.6, True), (0.0, False)])
async def test_an_actor_call_past_its_budget_is_abandoned_on_time(
    deps, form, actor_delay_s, late
):
    calls: list[CallStats] = []
    llm, stub = bedrock_with_slow_actor(deps.cards, actor_delay_s, calls)
    try:
        t = time.monotonic()
        line = await compose(deps, form, llm, probe_move(), budget_s=0.15)
        elapsed = time.monotonic() - t
    finally:
        llm.close()
    assert len(stub.named("actor")) == 1
    if late:
        assert elapsed < 0.45  # the board did not wait for the 0.6 s call
        assert line.guard == "actor_budget" and line.fallback_kind == "persona_line"
        assert line.text in deps.personas["economist"].fallback_lines
        assert "0.15" in line.error
        assert [c.error for c in calls if c.name == "actor"] == ["cancelled"]
    else:  # the control: a prompt call is spoken normally
        assert not line.is_fallback and not line.error


async def test_the_session_traces_the_actor_fallback_and_carries_on(deps, form, tmp_path):
    from conftest import answer

    llm = make_fake_llm(deps.cards)
    d = replace(deps, llm=llm)
    recorder = TraceRecorder(tmp_path, "r14")
    llm.add_sink(recorder.on_call)
    session = InterviewSession(
        deps=d, form=form, recorder=recorder, analyzer=TurnAnalyzer(d), session_id="r14"
    )
    await session.open()
    llm.push("actor", LLMTransientError("throttled"))  # only the first answer's actor call fails
    failed = await answer(session, "Kanpur mein repair shops bahut hain. Training kam hai.")
    assert failed.move.move != Move.move_on
    persona = d.personas[failed.move.member]
    assert failed.line.text.endswith(tuple(persona.fallback_lines))
    tr = failed.trace
    assert tr["actor_fallback"] and tr["actor_fallback_kind"] == "persona_line"
    assert "throttled" in tr["actor_error"]
    assert [f["spoke"] for f in tr["fallbacks"] if f["stage"] == "actor"] == ["persona_line"]
    assert [c["error"] for c in tr["llm_calls"] if c["name"] == "actor"] == [
        "LLMTransientError"
    ]

    # The interview carries on: the next turn is phrased by the actor as usual.
    after = await answer(session, "Tourism se rozgaar badhta hai, lekin pollution bhi.")
    assert not after.trace["actor_fallback"] and after.trace["actor_error"] is None
    await session.finish()
