"""Debate listener (DB2): optional debate fields, UPSC parsing unchanged."""

from __future__ import annotations

import pytest

from conftest import make_analysis, seg
from mockboard.brain.listener import analyse_segment, build_listener_user, to_analysis
from mockboard.brain.prompts import build_prompt_kit, load_prompt
from mockboard.brain.schemas import (
    DebateArgument,
    DebateContext,
    OpponentArg,
    PointStrength,
    StrategyContext,
)
from mockboard.brain.strategist import merge_analyses
from mockboard.brain.tools import (
    DEBATE_LISTENER_TOOL,
    LISTENER_TOOL,
    DebateListenerOutput,
    ListenerOutput,
)
from mockboard.modes.debate.listener import debate_listener_context
from mockboard.modes.debate.motions import Side, load_curated_motions

UPSC_LISTENER_FIELDS = {
    "stance", "reasons", "claims", "balance", "honest_unknown", "concedes_error", "garbled",
}


def debate_ctx(motion_id: str = "smartphones_in_schools", side: Side = Side.for_, **kw):
    motion = load_curated_motions()[motion_id]
    return StrategyContext(
        mode="debate",
        turn_id="t1",
        turn_index=1,
        topic="clash",
        debate=DebateContext(
            motion=motion.text,
            user_side=side.value,
            opponent_args=[
                OpponentArg(id=a.id, claim=a.claim, keywords=a.keywords)
                for a in motion.opponent_case(side)
            ],
            **kw,
        ),
    )


@pytest.fixture
def debate_kit(settings, deps, cards):
    return build_prompt_kit(
        deps.personas, cards, settings.prompt_versions("debate"), shared_id="shared_debate"
    )


def test_upsc_listener_schema_and_message_are_unchanged():
    assert set(LISTENER_TOOL.input_schema["properties"]) == UPSC_LISTENER_FIELDS
    assert set(LISTENER_TOOL.input_schema["required"]) == UPSC_LISTENER_FIELDS
    assert set(ListenerOutput.model_fields) == UPSC_LISTENER_FIELDS
    msg = build_listener_user(seg("Hello there."), "Q?")
    assert msg == (
        "Analyse segment 0 now and call the tool.\n<question>\nQ?\n</question>\n"
        "<candidate_speech>\nHello there.\n</candidate_speech>"
    )


def test_debate_tool_is_the_upsc_tool_plus_optional_fields():
    props = set(DEBATE_LISTENER_TOOL.input_schema["properties"])
    assert props > UPSC_LISTENER_FIELDS
    assert props - UPSC_LISTENER_FIELDS == {
        "arguments", "rebuts", "question_to_opponent", "concedes_point", "point_strength",
        "off_limits",
    }
    # Optional in code: a payload with only the UPSC fields still parses.
    out = DebateListenerOutput.model_validate({"stance": "x", "point_strength": ""})
    assert out.point_strength is None and out.rebuts == []


async def test_upsc_analysis_has_neutral_debate_defaults(deps):
    a = await analyse_segment(
        deps.llm, deps.kit, segment=seg("Tourism se rozgaar milta hai, lekin pollution bhi."),
        question="Q?", turn_id="t1", turn_index=1,
    )
    assert (a.arguments, a.rebuts, a.question_to_opponent) == ([], [], "")
    assert not a.concedes_point and a.point_strength is None and not a.off_limits
    call = deps.llm.calls_named("listener")[-1]
    assert "<debate_context>" not in call.user


def test_prompts_load():
    for pid in ("shared_debate", "listener_debate"):
        assert load_prompt(pid, 1).body


async def test_debate_listener_fills_the_debate_fields(deps, debate_kit):
    ctx = debate_ctx()
    text = (
        "Lekin phones learning tools bhi hain, kyunki educational apps aur videos padhai mein "
        "madad karte hain jahan library nahi hai. Fair point ki distraction hoti hai. "
        "Aap batayiye, parents emergency mein bachche se kaise baat karenge?"
    )
    a = await analyse_segment(
        deps.llm, debate_kit, segment=seg(text), question="Opponent line", turn_id="t1",
        turn_index=1, prompt_id="listener_debate", tool=DEBATE_LISTENER_TOOL,
        output_model=DebateListenerOutput, context=debate_listener_context(ctx),
    )
    # The user (For the ban) is answering the Opponent's (Against) case.
    assert "A1" in a.rebuts
    assert a.question_to_opponent.endswith("?")
    assert a.concedes_point
    assert any(arg.warrant for arg in a.arguments)
    assert a.point_strength == PointStrength.strong
    assert not a.off_limits
    call = deps.llm.calls_named("listener")[-1]
    assert "<debate_context>" in call.user and '"A1"' in call.user


async def test_off_limits_and_weak_points(deps, debate_kit):
    ctx = debate_ctx()
    common = dict(
        question="Q", turn_id="t1", turn_index=1, prompt_id="listener_debate",
        tool=DEBATE_LISTENER_TOOL, output_model=DebateListenerOutput,
        context=debate_listener_context(ctx),
    )
    a = await analyse_segment(
        deps.llm, debate_kit,
        segment=seg("Yeh sab BJP aur Congress ki politics hai, election ke liye."), **common,
    )
    assert a.off_limits
    b = await analyse_segment(deps.llm, debate_kit, segment=seg("Phones bure hain."), **common)
    assert b.point_strength == PointStrength.weak and not b.off_limits


def test_to_analysis_copies_debate_fields():
    out = DebateListenerOutput.model_validate(
        {
            "arguments": [{"claim": "Phones distract", "warrant": "notifications"}],
            "rebuts": ["A1", " "],
            "question_to_opponent": " Why? ",
            "concedes_point": True,
            "point_strength": "adequate",
            "off_limits": False,
        }
    )
    a = to_analysis(out, turn_id="t1", turn_index=1, segment=seg("x y"))
    assert a.arguments == [DebateArgument(claim="Phones distract", warrant="notifications")]
    assert a.rebuts == ["A1"] and a.question_to_opponent == "Why?"
    assert a.point_strength == PointStrength.adequate


def test_merge_analyses_combines_debate_fields():
    one = make_analysis(rebuts=["A1"], point_strength=PointStrength.weak)
    two = make_analysis(
        rebuts=["A1", "A2"], point_strength=PointStrength.strong, question_to_opponent="Q?",
        off_limits=True, arguments=[DebateArgument(claim="c")],
    )
    m = merge_analyses([one, two])
    assert m.rebuts == ["A1", "A2"] and m.point_strength == PointStrength.strong
    assert m.question_to_opponent == "Q?" and m.off_limits and len(m.arguments) == 1
    plain = merge_analyses([make_analysis(), make_analysis()])
    assert plain.point_strength is None and plain.rebuts == [] and not plain.off_limits
