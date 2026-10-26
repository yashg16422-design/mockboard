"""Ruling 17 (refines ruling 14 for voice mode, CTO_NOTES_03 §5).

* No double filler: if a masking filler already played this turn, a failed actor call falls
  back to the move's template question, never the persona's fallback line (itself a pause).
  The flag is read when the call fails, because Pipecat starts the filler while the actor
  call is still pending. Text mode never sets it, so its fallback is unchanged.
* Fast-throttle retry (`actor_fast_throttle_retry`, off by default): one retry, only for a
  ThrottlingException that comes back within `actor_fast_throttle_window_ms`, before any
  token, inside what is left of the first-token budget.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from botocore.exceptions import ClientError

from conftest import make_claim
from fake_bedrock import MIDSTREAM, FakeBedrockRuntime, throttling
from mockboard.brain.actor import FILLER_TEMPLATES, compose_line, fact_challenge_template
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import (
    BedrockNovaClient,
    FakeLLMClient,
    Lane,
    LaneConfig,
    LLMThrottledError,
    LLMTransientError,
    _map_boto_error,
    is_transient,
)
from mockboard.brain.schemas import BoardMove, Move
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

LINE = "Achha, iska ek thos example dijiye, kaise hoga?"


def move_of(kind: Move, member: str = "economist", **kw) -> BoardMove:
    return BoardMove(move=kind, member=member, reason="TEST: ruling 17", **kw)


async def compose(deps, form, llm, move, **kw):
    kw.setdefault("turn_id", "t3")
    return await compose_line(
        llm, deps.kit, deps.personas[move.member], move, topic="tourism", form=form,
        last_question="q", last_answer="a", max_words=30, max_tokens=120, **kw,
    )


# ---------------------------------------------------------------------------
# 1. No persona fallback line straight after a masking filler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [Move.probe, Move.counter, Move.reassure, Move.pressure])
async def test_after_a_filler_a_failed_actor_says_the_move_template(deps, form, kind):
    llm = FakeLLMClient(script={"actor": [LLMTransientError("throttled")]})
    line = await compose(deps, form, llm, move_of(kind), filler_played=lambda: True)
    assert line.text == FILLER_TEMPLATES[kind]
    assert line.text not in deps.personas["economist"].fallback_lines
    assert line.is_fallback and line.fallback_kind == "move_template"
    assert line.guard == "actor_failed" and "throttled" in line.error


async def test_without_a_filler_the_persona_line_is_unchanged(deps, form):
    for flag in (None, lambda: False):
        llm = FakeLLMClient(script={"actor": [LLMTransientError("throttled")]})
        line = await compose(deps, form, llm, move_of(Move.probe), filler_played=flag)
        assert line.text in deps.personas["economist"].fallback_lines
        assert line.fallback_kind == "persona_line"


async def test_the_filler_flag_is_read_when_the_call_fails(deps, form):
    """Pipecat starts the filler while the actor is still pending; the flag set during the
    call must count."""
    played = {"filler": False}

    def slow_then_fail(system: str, user: str):
        played["filler"] = True  # the filler started while we waited
        raise LLMTransientError("throttled")

    llm = FakeLLMClient(script={"actor": [slow_then_fail]})
    line = await compose(deps, form, llm, move_of(Move.counter),
                         filler_played=lambda: played["filler"])
    assert line.text == FILLER_TEMPLATES[Move.counter]


async def test_a_missed_budget_after_a_filler_also_says_the_template(deps, form):
    stub = FakeBedrockRuntime(deps.cards, delay=lambda n, u: 0.5 if n == "actor" else 0.0)
    llm = BedrockNovaClient(region="ap-southeast-2", model_id="amazon.nova-pro-v1:0",
                            client=stub, lanes={lane: LaneConfig(2, 1.0, 1.0) for lane in Lane})
    try:
        line = await compose(deps, form, llm, move_of(Move.probe), first_token_budget_s=0.1,
                             filler_played=lambda: True)
    finally:
        llm.close()
    assert line.guard == "actor_budget" and line.text == FILLER_TEMPLATES[Move.probe]


async def test_fact_challenge_and_move_on_keep_their_templates_after_a_filler(deps, form):
    card = deps.cards.get("builtin_tourism_gdp_share")
    llm = FakeLLMClient(script={"actor": [LLMTransientError("x"), LLMTransientError("y")]})
    challenge = move_of(Move.fact_challenge, target="t0.s0.c0")
    line = await compose(deps, form, llm, challenge, claim=make_claim(), card=card,
                         filler_played=lambda: True)
    assert line.text == fact_challenge_template(make_claim(), card)
    assert line.fallback_kind == "challenge_template"
    move_on = move_of(Move.move_on, member="chairman", target="internal_security")
    line = await compose(deps, form, llm, move_on, filler_played=lambda: True)
    assert line.text == FILLER_TEMPLATES[Move.move_on]


def new_session(deps, form, tmp_path, llm, sid: str, **settings_update) -> InterviewSession:
    d = replace(deps, llm=llm, settings=deps.settings.model_copy(update=settings_update))
    recorder = TraceRecorder(tmp_path, sid)
    llm.add_sink(recorder.on_call)
    return InterviewSession(deps=d, form=form, recorder=recorder, analyzer=TurnAnalyzer(d),
                            session_id=sid)


async def voice_turn(session, text: str, **speak_kw):
    session.submit_segment(text, segment_index=0)
    await session.end_of_turn(30.0)
    return await session.speak(**speak_kw)


async def test_session_speak_filler_played_gives_the_template_and_traces_it(deps, form, tmp_path):
    llm = make_fake_llm(deps.cards)
    session = new_session(deps, form, tmp_path, llm, "r17a")
    await session.open()
    await voice_turn(session, "Kanpur mein repair shops bahut hain. Training kam hai.")
    llm.push("actor", LLMTransientError("throttled"))
    out = await voice_turn(session, "Tourism se rozgaar badhta hai, lekin pollution bhi.",
                           filler_played=True)
    assert out.move.move not in (Move.move_on, Move.fact_challenge)
    assert out.line.text.endswith(FILLER_TEMPLATES[out.move.move])
    persona = session.deps.personas[out.move.member]
    assert not out.line.text.endswith(tuple(persona.fallback_lines))
    assert out.trace["filler_played"] is True
    assert out.trace["actor_fallback_kind"] == "move_template"
    # The flag belongs to one turn: the next turn starts without it.
    after = await voice_turn(session, "Homestays se gaon ki aamdani badh sakti hai.")
    assert after.trace["filler_played"] is False and not after.trace["actor_fallback"]
    await session.finish()


async def test_mark_filler_played_during_speak_is_honoured(deps, form, tmp_path):
    llm = make_fake_llm(deps.cards)
    session = new_session(deps, form, tmp_path, llm, "r17b")
    await session.open()
    await voice_turn(session, "Kanpur mein repair shops bahut hain. Training kam hai.")

    def filler_then_fail(system: str, user: str):
        session.mark_filler_played()  # Pipecat's masking filler starts mid-call
        raise LLMTransientError("throttled")

    llm.push("actor", filler_then_fail)
    out = await voice_turn(session, "Tourism se rozgaar badhta hai, lekin pollution bhi.")
    assert out.trace["filler_played"] is True
    assert out.line.text.endswith(FILLER_TEMPLATES[out.move.move])
    await session.finish()


async def test_mark_filler_played_needs_an_open_turn(deps, form, tmp_path):
    session = new_session(deps, form, tmp_path, make_fake_llm(deps.cards), "r17c")
    with pytest.raises(RuntimeError):
        session.mark_filler_played()


# ---------------------------------------------------------------------------
# 2. Optional fast-throttle retry
# ---------------------------------------------------------------------------


def test_settings_default_the_fast_throttle_retry_off(settings):
    assert settings.actor_fast_throttle_retry is False
    assert settings.actor_fast_throttle_window_ms == 300


def test_a_bedrock_throttle_maps_to_a_typed_transient_error():
    err = _map_boto_error(throttling("ConverseStream"))
    assert isinstance(err, LLMThrottledError) and is_transient(err)
    other = _map_boto_error(
        ClientError({"Error": {"Code": "ServiceUnavailableException", "Message": "x"}}, "C")
    )
    assert is_transient(other) and not isinstance(other, LLMThrottledError)


async def test_off_by_default_a_fast_throttle_is_not_retried(deps, form):
    llm = FakeLLMClient(script={"actor": [LLMThrottledError("fast"), LINE]})
    line = await compose(deps, form, llm, move_of(Move.probe))
    assert len(llm.calls_named("actor")) == 1
    assert line.fallback_kind == "persona_line" and not line.retried


async def test_on_a_fast_throttle_is_retried_once_and_spoken(deps, form):
    llm = FakeLLMClient(script={"actor": [LLMThrottledError("fast"), LINE]})
    line = await compose(deps, form, llm, move_of(Move.probe), first_token_budget_s=2.5,
                         fast_throttle_retry_window_s=0.3)
    assert len(llm.calls_named("actor")) == 2
    assert line.text == LINE and line.retried and not line.is_fallback


async def test_on_only_one_retry_then_the_fallback(deps, form):
    llm = FakeLLMClient(script={"actor": [LLMThrottledError("a"), LLMThrottledError("b"), LINE]})
    line = await compose(deps, form, llm, move_of(Move.probe), fast_throttle_retry_window_s=0.3)
    assert len(llm.calls_named("actor")) == 2
    assert line.is_fallback and line.retried and "b" in line.error


async def test_on_other_transient_errors_are_not_retried(deps, form):
    llm = FakeLLMClient(script={"actor": [LLMTransientError("timeout/connection"), LINE]})
    line = await compose(deps, form, llm, move_of(Move.probe), fast_throttle_retry_window_s=0.3)
    assert len(llm.calls_named("actor")) == 1 and line.is_fallback and not line.retried


def bedrock(deps, *, delay_s: float, faults: list):
    """Real BedrockNovaClient over the fake boto3; the n-th actor call takes faults[n]."""
    seen = {"n": 0}

    def fault(name, user):
        if name != "actor":
            return None
        i, seen["n"] = seen["n"], seen["n"] + 1
        return faults[i] if i < len(faults) else None

    stub = FakeBedrockRuntime(deps.cards, delay=lambda n, u: delay_s if n == "actor" else 0.0,
                              fault=fault)
    llm = BedrockNovaClient(region="ap-southeast-2", model_id="amazon.nova-pro-v1:0",
                            client=stub, lanes={lane: LaneConfig(2, 1.0, 1.0) for lane in Lane})
    return llm, stub


@pytest.mark.parametrize("delay_s, window_s, retried", [(0.0, 0.3, True), (0.15, 0.05, False)])
async def test_only_a_throttle_inside_the_window_is_retried(deps, form, delay_s, window_s,
                                                            retried):
    """End to end on the real client: botocore's ThrottlingException becomes
    LLMThrottledError, and only a fast one is retried."""
    llm, stub = bedrock(deps, delay_s=delay_s, faults=[throttling("ConverseStream")])
    try:
        line = await compose(deps, form, llm, move_of(Move.probe), first_token_budget_s=2.0,
                             fast_throttle_retry_window_s=window_s)
    finally:
        llm.close()
    assert len(stub.named("actor")) == (2 if retried else 1)
    assert line.retried is retried and line.is_fallback is (not retried)


async def test_a_throttle_after_the_first_token_is_not_retried(deps, form):
    llm, stub = bedrock(deps, delay_s=0.0, faults=[MIDSTREAM])
    try:
        line = await compose(deps, form, llm, move_of(Move.probe),
                             fast_throttle_retry_window_s=0.3)
    finally:
        llm.close()
    assert len(stub.named("actor")) == 1 and line.is_fallback and not line.retried


async def test_session_wires_the_setting_and_traces_the_retry(deps, form, tmp_path):
    llm = make_fake_llm(deps.cards)
    session = new_session(deps, form, tmp_path, llm, "r17d", actor_fast_throttle_retry=True)
    await session.open()
    await voice_turn(session, "Kanpur mein repair shops bahut hain. Training kam hai.")
    llm.push("actor", LLMThrottledError("fast"))  # then the responder phrases the line
    out = await voice_turn(session, "Tourism se rozgaar badhta hai, lekin pollution bhi.")
    assert out.trace["actor_fast_retry"] is True and not out.trace["actor_fallback"]
    assert [c["error"] for c in out.trace["llm_calls"] if c["name"] == "actor"] == [
        "LLMThrottledError", None
    ]
    await session.finish()
