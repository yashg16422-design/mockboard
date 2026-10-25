"""Graph wiring: skip-librarian edge, Send fan-out, per-claim error isolation, retry policy,
shared vote cache (real votes only), session threads and stale-result stamping."""

from __future__ import annotations

import pytest

from conftest import make_claim, make_ctx, seg
from mockboard.brain.fake_responders import listener_responder, make_librarian_responder
from mockboard.brain.graphs import (
    TurnAnalyzer,
    compile_turn_graph,
    route_after_listener,
    upsert_claims,
)
from mockboard.brain.llm import FakeLLMClient, LLMTransientError
from mockboard.brain.schemas import Move, Verdict

CLAIMY = "Tourism is 30 percent of Testland GDP in 2023, so jobs depend on it heavily."
TWO_CLAIMS = (
    "Tourism is 30 percent of Testland GDP in 2023. "
    "Testland received 15 lakh foreign tourist arrivals in 2022."
)
NO_CLAIMS = "I think tourism creates jobs but it can also damage fragile places, so balance it."


def analyzer(deps, llm: FakeLLMClient) -> TurnAnalyzer:
    from dataclasses import replace

    return TurnAnalyzer(replace(deps, llm=llm))


def llm_with(cards, **script) -> FakeLLMClient:
    return FakeLLMClient(
        script=script,
        responders={"listener": listener_responder, "librarian": make_librarian_responder(cards)},
    )


async def run(ta: TurnAnalyzer, text: str, turn_index: int = 0, session: str = "S1", **ctx):
    return await ta.analyse(
        session_id=session,
        turn_id=f"t{turn_index}",
        turn_index=turn_index,
        question="q",
        segment=seg(text),
        ctx=make_ctx(turn_index=turn_index, **ctx),
    )


async def test_skip_librarian_when_no_claims(deps, cards):
    llm = llm_with(cards)
    r = await run(analyzer(deps, llm), NO_CLAIMS)
    assert r.librarian_skipped
    assert llm.calls_named("librarian") == []
    assert r.move.for_turn == "t0"


async def test_skip_librarian_when_garbled(deps, cards):
    llm = llm_with(
        cards,
        listener=[
            {
                "stance": "",
                "reasons": [],
                "balance": "unclear",
                "honest_unknown": False,
                "garbled": True,
                "claims": [
                    {
                        "text": "30 percent",
                        "claim_en": "30 percent",
                        "kind": "numeric",
                        "value": 30,
                        "unit": "percent",
                    }
                ],
            }
        ],
    )
    r = await run(analyzer(deps, llm), "... 30 ... percent ..")
    assert r.librarian_skipped and llm.calls_named("librarian") == []
    assert r.move.move == Move.clarify


def test_route_is_a_pure_function_of_state():
    claim = make_claim()
    sends = route_after_listener(
        {
            "pending_claims": [claim, claim],
            "card_set_version": "v",
            "analyses": [],
        }
    )
    assert [s.node for s in sends] == ["librarian", "librarian"]
    assert route_after_listener({"pending_claims": []}) == "strategist"


async def test_fan_out_one_librarian_call_per_claim(deps, cards):
    llm = llm_with(cards)
    r = await run(analyzer(deps, llm), TWO_CLAIMS)
    assert len(llm.calls_named("librarian")) == 2
    verdicts = {c.card_id: c.verdict for c in r.ledger}
    assert verdicts == {
        "builtin_tourism_gdp_share": Verdict.contradicted,
        "builtin_tourism_arrivals": Verdict.supported,
    }


async def test_per_claim_error_isolation(deps, cards):
    """One claim's librarian raises a non-transient error; the other claim still gets its
    verdict, the failing claim becomes unverifiable, and the run completes."""
    good = make_librarian_responder(cards)

    def librarian(system, user):
        if "arrivals" in user:
            raise ValueError("boom: unexpected bug in one branch")
        return good(system, user)

    llm = FakeLLMClient(responders={"listener": listener_responder, "librarian": librarian})
    r = await run(analyzer(deps, llm), TWO_CLAIMS)
    by_card = {c.claim_en[:20]: c.verdict for c in r.ledger}
    assert Verdict.contradicted in by_card.values()
    assert Verdict.unverifiable in by_card.values()
    assert [f["stage"] for f in r.fallbacks] == ["librarian"]


async def test_transient_errors_are_retried_by_retry_policy(deps, cards):
    llm = llm_with(cards, librarian=[LLMTransientError("ThrottlingException")])
    r = await run(analyzer(deps, llm), CLAIMY)
    assert len(llm.calls_named("librarian")) == 2  # one throttled, one retried
    assert r.ledger[0].verdict == Verdict.contradicted
    assert r.fallbacks == []


async def test_persistent_throttling_falls_back_after_max_attempts(deps, cards):
    attempts = deps.settings.graph_retry_max_attempts
    llm = llm_with(cards, librarian=[LLMTransientError("throttled")] * attempts)
    r = await run(analyzer(deps, llm), CLAIMY)
    assert len(llm.calls_named("librarian")) == attempts
    assert r.ledger[0].verdict == Verdict.unverifiable
    assert r.fallbacks and r.fallbacks[0]["stage"] == "librarian"


async def test_listener_failure_gives_safe_default(deps, cards):
    llm = llm_with(cards, listener=[{"balance": "sideways"}, {"balance": "sideways"}])
    r = await run(analyzer(deps, llm), CLAIMY)
    assert r.analysis.is_fallback and r.analysis.claims == []
    assert r.librarian_skipped
    assert r.fallbacks[0]["stage"] == "listener"


async def test_real_verdict_is_cached_by_claim_and_card_version(deps, cards):
    llm = llm_with(cards)
    ta = analyzer(deps, llm)
    first = await run(ta, CLAIMY, 0, session="A")
    await run(ta, CLAIMY, 1, session="A")
    second = await run(ta, CLAIMY, 0, session="B")  # the vote cache is process-wide
    assert len(llm.calls_named("librarian")) == 1
    assert first.ledger[0].verdict == second.ledger[0].verdict == Verdict.contradicted
    assert deps.vote_cache.hits == 2
    assert second.fallbacks == []


async def test_card_set_change_misses_the_cache(deps, cards):
    from dataclasses import replace

    llm = llm_with(cards)
    await run(analyzer(deps, llm), CLAIMY, 0, session="A")
    edited = replace(cards, version=cards.version + "-edited")
    await run(analyzer(replace(deps, cards=edited), llm), CLAIMY, 0, session="B")
    assert len(llm.calls_named("librarian")) == 2


async def test_throttled_fallback_in_one_session_is_never_served_to_another(deps, cards):
    """CTO_NOTES_02 M1 regression: session A is throttled on every attempt and falls back to
    unverifiable. Session B, analysing the same claim, must call the librarian itself and
    get the real verdict, and its trace must not report a fallback that never happened."""
    attempts = deps.settings.graph_retry_max_attempts
    llm = llm_with(cards, librarian=[LLMTransientError("ThrottlingException")] * attempts)
    ta = analyzer(deps, llm)

    a = await run(ta, CLAIMY, 0, session="A")
    assert a.ledger[0].verdict == Verdict.unverifiable
    assert [f["stage"] for f in a.fallbacks] == ["librarian"]
    assert len(deps.vote_cache) == 0  # the fallback was not cached

    b = await run(ta, CLAIMY, 0, session="B")
    assert len(llm.calls_named("librarian")) == attempts + 1  # B made its own call
    assert b.ledger[0].verdict == Verdict.contradicted
    assert b.fallbacks == []
    assert len(deps.vote_cache) == 1  # B's real vote is now cached


async def test_same_session_retries_after_its_own_fallback(deps, cards):
    attempts = deps.settings.graph_retry_max_attempts
    llm = llm_with(cards, librarian=[LLMTransientError("x")] * attempts)
    ta = analyzer(deps, llm)
    first = await run(ta, CLAIMY, 0)
    second = await run(ta, CLAIMY, 1)
    assert first.ledger[0].verdict == Verdict.unverifiable
    assert second.ledger[-1].verdict == Verdict.contradicted


def test_librarian_node_has_no_langgraph_cache_policy():
    builder = compile_turn_graph().builder
    assert builder.nodes["librarian"].cache_policy is None


async def test_session_state_accumulates_and_enables_delayed_challenge(deps, cards):
    ta = analyzer(deps, llm_with(cards))
    r0 = await run(ta, CLAIMY, 0)
    assert r0.move.move != Move.fact_challenge  # never in the same turn
    r1 = await run(ta, NO_CLAIMS, 1)
    assert r1.move.move == Move.fact_challenge and r1.move.target == r0.ledger[0].id
    assert r1.move.for_turn == "t1"
    # Separate sessions do not share state.
    other = await run(ta, NO_CLAIMS, 1, session="OTHER")
    assert other.move.move != Move.fact_challenge


async def test_close_session_drops_thread(deps, cards):
    ta = analyzer(deps, llm_with(cards))
    await run(ta, CLAIMY, 0)
    await ta.close_session("S1")
    snap = await ta.graph.aget_state({"configurable": {"thread_id": "S1"}})
    assert not snap.values


async def test_results_carry_ids_and_stage_timings(deps, cards):
    r = await run(analyzer(deps, llm_with(cards)), CLAIMY, 0)
    assert (r.session_id, r.turn_id, r.segment_id) == ("S1", "t0", "s0")
    stages = [t["stage"] for t in r.timings]
    assert stages == ["listener_start", "listener_end", "librarian_end", "strategist_end"]
    assert all(t["turn_id"] == "t0" for t in r.timings)


def test_upsert_claims_replaces_by_id():
    a, b = make_claim("t0.s0.c0"), make_claim("t0.s0.c1")
    a2 = a.model_copy(update={"verdict": Verdict.supported})
    assert [c.verdict for c in upsert_claims([a, b], [a2])] == [Verdict.supported, b.verdict]


def test_actor_is_not_a_graph_node():
    nodes = set(compile_turn_graph().get_graph().nodes)
    assert "actor" not in nodes
    assert {"listener", "librarian", "gate", "strategist"} <= nodes


@pytest.mark.parametrize("bad", [ValueError("x"), KeyError("y"), RuntimeError("z")])
async def test_non_transient_listener_errors_are_not_retried(deps, cards, bad):
    llm = llm_with(cards, listener=[bad])
    r = await run(analyzer(deps, llm), CLAIMY)
    assert len(llm.calls_named("listener")) == 1 and r.analysis.is_fallback
