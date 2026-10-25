"""Judge rubric (ruling 6): seven UPSC qualities + insufficient_evidence + two practice
indicators + report_language, citation check, and the bounded revision loop."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import seg
from mockboard.brain.fake_responders import judge_responder
from mockboard.brain.graphs import generate_report
from mockboard.brain.llm import FakeLLMClient
from mockboard.brain.schemas import (
    INSUFFICIENT_EVIDENCE,
    PRACTICE_INDICATORS,
    UPSC_QUALITIES,
    CandidateTurn,
    Dimension,
    ReportLanguage,
    SessionReport,
)
from mockboard.brain.tools import JUDGE_TOOL, JudgeOutput

QUOTE = "tourism creates jobs for local youth"
TURNS = [
    CandidateTurn(
        turn_id="t0",
        turn_index=0,
        topic="tourism",
        question="q",
        segments=[seg(f"I think {QUOTE}, but it also strains water.")],
    )
]


def payload(quote: str = QUOTE, score: str = "4") -> dict:
    return {
        "scores": [
            {
                "dimension": d.value,
                "score": score
                if d != Dimension.social_cohesion_leadership
                else "insufficient_evidence",
                "rationale": "r",
            }
            for d in Dimension
        ],
        "evidence": [
            {"dimension": d.value, "turn_id": "t0", "quote": quote}
            for d in Dimension
            if d != Dimension.social_cohesion_leadership
        ],
        "composure_moments": [{"turn_id": "t0", "note": "steady"}],
        "model_answer": "A balanced answer.",
    }


def test_rubric_is_exactly_upsc_seven_plus_two_practice_indicators():
    assert [d.value for d in UPSC_QUALITIES] == [
        "mental_alertness",
        "critical_assimilation",
        "clear_logical_exposition",
        "balance_of_judgement",
        "variety_depth_of_interest",
        "social_cohesion_leadership",
        "intellectual_moral_integrity",
    ]
    assert [d.value for d in PRACTICE_INDICATORS] == ["factual_accuracy", "composure"]
    assert len(Dimension) == 9
    banned = ("accent", "dialect", "grammar", "fluency", "pronunciation")
    assert not any(b in d.value for d in Dimension for b in banned)


def test_judge_tool_offers_insufficient_evidence():
    enum = JUDGE_TOOL.input_schema["properties"]["scores"]["items"]["properties"]["score"]["enum"]
    assert enum == ["1", "2", "3", "4", "5", INSUFFICIENT_EVIDENCE]


def test_judge_output_requires_every_dimension_once():
    p = payload()
    p["scores"] = p["scores"][:-1]
    with pytest.raises(ValidationError):
        JudgeOutput.model_validate(p)
    p = payload()
    p["scores"].append(p["scores"][0])
    with pytest.raises(ValidationError):
        JudgeOutput.model_validate(p)


@pytest.mark.parametrize("bad", ["0", "6", "great"])
def test_judge_scores_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        JudgeOutput.model_validate(payload(score=bad))


def test_numeric_score_needs_evidence_in_report():
    from mockboard.brain.schemas import DimensionScore

    with pytest.raises(ValidationError):
        DimensionScore(dimension=Dimension.composure, score=3, rationale="r")
    DimensionScore(dimension=Dimension.composure, score=INSUFFICIENT_EVIDENCE, rationale="r")


async def test_report_graph_verified_quotes(deps):
    llm = FakeLLMClient(script={"judge": [payload()]})
    rep = await generate_report(
        _with(deps, llm), session_id="S", turns=TURNS, ledger=[], moves=[], language="en"
    )
    assert isinstance(rep, SessionReport) and not rep.is_fallback
    assert rep.citation_check_passed and rep.judge_revisions == 0
    assert rep.score_for(Dimension.social_cohesion_leadership).score == INSUFFICIENT_EVIDENCE
    assert rep.score_for(Dimension.composure).score == 4
    assert len(llm.calls_named("judge")) == 1


async def test_report_graph_revises_once_then_drops_bad_quotes(deps):
    llm = FakeLLMClient(script={"judge": [payload("invented words"), payload("still invented")]})
    rep = await generate_report(
        _with(deps, llm), session_id="S", turns=TURNS, ledger=[], moves=[], language="en"
    )
    assert len(llm.calls_named("judge")) == 2  # one revision, never more
    assert "revision_request" in llm.calls_named("judge")[1].user
    assert rep.judge_revisions == 1 and not rep.citation_check_passed
    assert all(s.score == INSUFFICIENT_EVIDENCE for s in rep.scores)


async def test_report_graph_revision_fixes_quotes(deps):
    llm = FakeLLMClient(script={"judge": [payload("invented words"), payload()]})
    rep = await generate_report(
        _with(deps, llm), session_id="S", turns=TURNS, ledger=[], moves=[], language="en"
    )
    assert rep.citation_check_passed and rep.judge_revisions == 1


async def test_judge_unavailable_gives_fallback_without_scores(deps):
    llm = FakeLLMClient(script={"judge": [{"scores": []}, {"scores": []}]})
    rep = await generate_report(
        _with(deps, llm), session_id="S", turns=TURNS, ledger=[], moves=[], language="en"
    )
    assert rep.is_fallback and rep.scores == []


@pytest.mark.parametrize("lang", ["en", "hi"])
async def test_report_language_is_passed_and_recorded(deps, lang):
    llm = FakeLLMClient(responders={"judge": judge_responder})
    rep = await generate_report(
        _with(deps, llm), session_id="S", turns=TURNS, ledger=[], moves=[], language=lang
    )
    assert rep.report_language == ReportLanguage(lang)
    assert f"<report_language>\n{lang}\n</report_language>" in llm.calls[0].user
    if lang == "hi":
        assert any("ऀ" <= ch <= "ॿ" for ch in rep.model_answer)


async def test_report_language_defaults_to_settings(deps):
    llm = FakeLLMClient(responders={"judge": judge_responder})
    rep = await generate_report(_with(deps, llm), session_id="S", turns=TURNS, ledger=[], moves=[])
    assert rep.report_language == ReportLanguage(deps.settings.report_language)


def test_judge_prompt_states_rubric_and_language_rule(deps):
    body = deps.kit.prompt("judge").body
    for d in Dimension:
        assert d.value in body
    assert "insufficient_evidence" in body and "report_language" in body
    assert "NO score for accent" in body


def _with(deps, llm):
    from dataclasses import replace

    return replace(deps, llm=llm)
