"""The smarter board over MCP (owner, Nov 9): the client model proposes moves, Mock Board rules on
each; the sitting opens with an introduction; members' moods follow the answers. Free: no model."""

from __future__ import annotations

import pytest

from mockboard.mcp.service import BoardService
from mockboard.mcp.tools import call_tool

FORM = {"home_state": "Uttar Pradesh", "district": "Lucknow", "degree_subject": "Physics",
        "hobbies": ["Chess"], "work_experience": "One year as a school science teacher",
        "interview_language": "english"}
GOOD = {"stance": "start with those affected", "reasons": ["they know the problem", "cost"],
        "balance": "balanced", "honest_unknown": False, "concedes_error": False,
        "garbled": False, "candidate_request": "none"}
INTRO = ("I am from Lucknow, I studied physics, and teaching for a year showed me how much a "
         "system can change, because one rule reaches thousands of children.")
LONG = ("I would start with the people most affected, because they know the problem best, and "
        "then weigh the cost against the benefit before deciding.")


@pytest.fixture
async def svc():
    s = BoardService()
    yield s
    await s.aclose()


async def start(svc) -> str:
    out, err = await call_tool(svc, "start_interview", {"form": FORM})
    assert not err
    return out["session_id"]


async def turn(svc, sid, said=LONG, analysis=GOOD, proposal=None):
    args = {"session_id": sid, "candidate_said": said, "analysis": analysis}
    if proposal is not None:
        args["proposal"] = proposal
    out, err = await call_tool(svc, "next_turn", args)
    assert not err, out
    return out


async def test_the_sitting_opens_with_an_introduction(svc):
    out, _ = await call_tool(svc, "start_interview", {"form": FORM})
    assert out["say"][-1]["question"].startswith("Please introduce yourself")


async def test_after_the_introduction_the_board_is_told_what_the_form_holds(svc):
    sid = await start(svc)
    out = await turn(svc, sid, INTRO)
    own = [line for line in out["say"] if line["how"] == "own_words"]
    assert own and "introduction left one of these out" in own[-1]["guidance"]
    assert "hobbies: Chess" in own[-1]["guidance"]


async def test_a_dig_is_allowed_and_said_as_the_models_move(svc):
    sid = await start(svc)
    await turn(svc, sid, INTRO)
    out = await turn(svc, sid, proposal={"action": "dig", "reason": "the cost point is vague"})
    if out["board"]["rule"] == "D-HANDOFF":  # the Chairperson's budget may already be spent
        out = await turn(svc, sid, proposal={"action": "dig", "reason": "the mechanism"})
    assert out["board"]["rule"] == "M-DIG"
    assert out["board"]["your_proposal"]["accepted"] is True
    own = [line for line in out["say"] if line["how"] == "own_words"]
    assert "dig deeper was allowed" in own[-1]["guidance"]


async def test_a_confrontation_must_quote_the_candidate(svc):
    sid = await start(svc)
    await turn(svc, sid, INTRO)
    await turn(svc, sid)  # into Doctor Iyer's segment
    made_up = await turn(svc, sid, proposal={"action": "confront", "quote": "I hate rules",
                                             "reason": "contradiction"})
    assert made_up["board"]["your_proposal"]["accepted"] is False
    assert "exact words" in made_up["board"]["your_proposal"]["why"]
    real = await turn(svc, sid, proposal={"action": "confront",
                                          "quote": "one rule reaches thousands of children",
                                          "reason": "now says rules are not enough"})
    if real["board"]["your_proposal"]["accepted"]:
        assert real["board"]["rule"] == "M-CONFRONT"
        own = [line for line in real["say"] if line["how"] == "own_words"]
        assert "one rule reaches thousands of children" in own[-1]["guidance"]
    else:  # the member's budget ran out first: refused with that reason, never silently
        assert "used up" in real["board"]["your_proposal"]["why"]


async def test_an_honest_i_dont_know_is_never_pressed(svc):
    sid = await start(svc)
    await turn(svc, sid, INTRO)
    honest = {**GOOD, "honest_unknown": True, "reasons": []}
    out = await turn(svc, sid, "I honestly do not know, sir.", honest,
                     proposal={"action": "elaborate", "reason": "thin"})
    assert out["board"]["your_proposal"]["accepted"] is False
    assert "never pressed" in out["board"]["your_proposal"]["why"]
    assert out["board"]["mood"] == "patient"


async def test_a_pivot_hands_over_and_digging_has_a_limit(svc):
    sid = await start(svc)
    await turn(svc, sid, INTRO)
    out = await turn(svc, sid, proposal={"action": "pivot", "reason": "heard enough"})
    assert out["board"]["rule"] == "D-HANDOFF" and out["board"]["your_proposal"]["accepted"]
    rules = []
    for _ in range(3):
        o = await turn(svc, sid, proposal={"action": "dig", "reason": "go deeper"})
        rules.append((o["board"]["rule"], o["board"]["your_proposal"]["accepted"]))
    assert rules[0] == ("M-DIG", True) and rules[1] == ("M-DIG", True)
    assert rules[2][1] is False  # the third dig on one point is refused: back to the plan


async def test_the_case_study_comes_before_a_pivot(svc):
    sid = await start(svc)
    out = None
    for _ in range(12):  # reach Shri Rathore's case study
        out = await turn(svc, sid, LONG)
        if out["board"]["situation"]:
            break
    assert out["board"]["situation"]
    twist = await turn(svc, sid, LONG)
    assert twist["board"]["rule"] == "D-TWIST"
    piv = await turn(svc, sid, proposal={"action": "pivot", "reason": "move on"})
    assert piv["board"]["your_proposal"]["accepted"] is True  # the case was asked: allowed


async def test_a_hedged_answer_makes_the_member_sceptical(svc):
    sid = await start(svc)
    await turn(svc, sid, INTRO)
    out = await turn(svc, sid, "Maybe, I think so.", {**GOOD, "reasons": [], "balance": "unclear"})
    assert out["board"]["mood"] in ("sceptical", "pressing")


async def test_an_unknown_action_is_an_error_the_model_can_fix(svc):
    sid = await start(svc)
    out, err = await call_tool(svc, "next_turn", {"session_id": sid, "candidate_said": LONG,
                                                  "proposal": {"action": "shout"}})
    assert err and "proposal.action" in out["error"]
