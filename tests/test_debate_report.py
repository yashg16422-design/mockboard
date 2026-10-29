"""Debate report (DB6): rubric with citation-checked quotes and one revision, the scoreboard
computed by code from the debate record, the best line, the templated verdict, en/hi output,
and the CLI. The UPSC report is locked separately (tests/golden, test_judge.py)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from conftest import answer
from mockboard import cli
from mockboard.brain.llm import LLMThrottledError
from mockboard.brain.schemas import (
    INSUFFICIENT_EVIDENCE,
    DebateDimension,
    DebateDimensionScore,
    DebateReport,
    DebateScoreboard,
    Evidence,
    ReportLanguage,
    VerdictLabel,
)
from mockboard.brain.tools import ALL_TOOLS, DEBATE_JUDGE_TOOL
from mockboard.config import REPO_ROOT
from mockboard.modes import get_profile
from mockboard.modes.debate.judge import build_debate_judge_user, code_best_line
from mockboard.modes.debate.report_format import format_debate_report
from mockboard.modes.debate.scoreboard import compute_scoreboard, rows_from_trace
from mockboard.modes.debate.verdict import build_verdict, choose_label, takes_sides
from test_debate_session import CLASH_PLAIN, CLASH_STRONG, CLOSING, OPENING, make_session

ANSWERS = [OPENING, CLASH_STRONG, CLASH_PLAIN, CLOSING]
BEST = (
    "Lekin phones learning tools tabhi hain jab sahi use ho, aur bachche khud ko rok nahi "
    "paate kyunki unka self-control abhi ban raha hai."
)
DISCLAIMER = "not which side of the motion is right"


def payload(quote: str = "Phones class mein dhyan bhatkate hain", *, best: str = BEST,
            best_turn: str = "t1", score: str = "4") -> dict:
    return {
        "scores": [
            {"dimension": d.value,
             "score": "insufficient_evidence" if d == DebateDimension.responsiveness else score,
             "rationale": f"{d.value} rationale"}
            for d in DebateDimension
        ],
        "evidence": [
            {"dimension": d.value, "turn_id": "t0", "quote": quote}
            for d in DebateDimension if d != DebateDimension.responsiveness
        ],
        "best_line_turn_id": best_turn,
        "best_line_quote": best,
        "best_line_reason": "It answers the opponent's point and gives a reason.",
    }


async def debate(deps, tmp_path, answers=ANSWERS, *, language="en", trace=False, sid="rep",
                 **kw):
    s = make_session(deps, tmp_path, trace=trace, sid=sid, **kw)
    await s.open()
    for text in answers:
        if s.state.finished:
            break
        await answer(s, text)
    return s, await s.finish(language)


# ---------------------------------------------------------------------------
# The rubric and the report graph
# ---------------------------------------------------------------------------


def test_rubric_is_the_profile_rubric_and_the_tool_enum():
    rubric = get_profile("debate").rubric
    assert rubric == tuple(d.value for d in DebateDimension) == (
        "clarity_structure", "evidence_use", "rebuttal_quality", "responsiveness",
        "fairness_to_other_side", "composure",
    )
    props = DEBATE_JUDGE_TOOL.input_schema["properties"]
    assert props["scores"]["items"]["properties"]["dimension"]["enum"] == list(rubric)
    assert ALL_TOOLS["judge_debate"][0] is DEBATE_JUDGE_TOOL
    for banned in ("accent", "grammar", "fluency"):
        assert not any(banned in d for d in rubric)


async def test_fake_debate_report_is_complete_and_citation_checked(deps, tmp_path):
    s, rep = await debate(deps, tmp_path)
    assert isinstance(rep, DebateReport) and not rep.is_fallback
    assert {x.dimension for x in rep.scores} == set(DebateDimension)
    assert rep.citation_check_passed and rep.judge_revisions == 0
    by_id = {t.turn_id: t.transcript for t in s.state.turns}
    for x in rep.scores:
        assert isinstance(x.score, int) == bool(x.evidence)
        for e in x.evidence:
            assert e.quote in by_id[e.turn_id]  # quotes are the user's own words
    assert rep.prompt_versions["judge_debate"] == 1
    assert rep.motion == s.hooks.setup.motion.text and rep.user_side == "for"
    judge_call = deps.llm.calls_named("judge_debate")[0]
    assert "judge_debate" in judge_call.system or "DEBATE JUDGE" in judge_call.system
    assert deps.llm.calls_named("judge") == []  # the UPSC judge is never called


async def test_revision_loop_runs_once_then_drops_unverified_quotes(deps, tmp_path):
    deps.llm.push("judge_debate", payload("invented words"), payload("still invented"))
    _, rep = await debate(deps, tmp_path)
    calls = deps.llm.calls_named("judge_debate")
    assert len(calls) == 2 and "revision_request" in calls[1].user
    assert rep.judge_revisions == 1 and not rep.citation_check_passed
    assert all(x.score == INSUFFICIENT_EVIDENCE for x in rep.scores)
    assert rep.verdict.label == VerdictLabel.no_verdict  # nothing verified, nothing judged
    assert DISCLAIMER in rep.verdict.text


async def test_revision_can_fix_the_quotes(deps, tmp_path):
    deps.llm.push("judge_debate", payload("invented words"), payload())
    _, rep = await debate(deps, tmp_path)
    assert rep.citation_check_passed and rep.judge_revisions == 1
    assert rep.score_for(DebateDimension.rebuttal_quality).score == 4
    assert rep.verdict.label == VerdictLabel.carried  # mean 4, and the user answered A1


async def test_best_line_is_citation_checked_and_revised(deps, tmp_path):
    deps.llm.push("judge_debate", payload(best="A line the user never said at all."),
                  payload(best="Still not something the user said."))
    _, rep = await debate(deps, tmp_path)
    first = deps.llm.calls_named("judge_debate")[1].user
    assert "best_line / t1" in first  # the revision request lists the best line too
    assert rep.best_line is not None and rep.best_line.chosen_by == "code"
    assert any("best line chosen by rule" in n for n in rep.notes)


async def test_judge_best_line_kept_when_it_checks_out(deps, tmp_path):
    deps.llm.push("judge_debate", payload())
    s, rep = await debate(deps, tmp_path)
    b = rep.best_line
    assert b is not None and b.chosen_by == "judge" and b.turn_id == "t1"
    assert b.quote == BEST and b.quote in s.state.turns[1].transcript
    assert b.reason


async def test_best_line_with_an_uncarded_figure_is_replaced(deps, tmp_path):
    figure = "Studies show 40 percent of bachche phone se distract hote hain kyunki apps hain."
    deps.llm.push("judge_debate", payload(best=figure, best_turn="t2"))
    _, rep = await debate(deps, tmp_path, [OPENING, CLASH_STRONG, figure, CLOSING])
    assert rep.best_line.chosen_by == "code" and "40" not in rep.best_line.quote
    assert any("figure" in n for n in rep.notes)


async def test_best_line_never_comes_from_an_off_limits_turn(deps, tmp_path):
    off = "Yeh sab politicians ki galti hai kyunki election ke time sab vaade karte hain."
    deps.llm.push("judge_debate", payload(best=off, best_turn="t1"))
    s, rep = await debate(deps, tmp_path, [OPENING, off, CLASH_STRONG, CLASH_PLAIN, CLOSING])
    assert s.state.moves[1].move.value == "redirect"
    assert rep.best_line.turn_id != "t1" and rep.best_line.chosen_by == "code"


async def test_judge_unavailable_gives_a_fallback_without_scores(deps, tmp_path):
    deps.llm.push("judge_debate", *[LLMThrottledError("throttled")] * 5)
    s, rep = await debate(deps, tmp_path)
    assert rep.is_fallback and rep.scores == []
    assert rep.verdict.label == VerdictLabel.no_verdict
    assert rep.verdict.text.startswith("The board could not score this debate")
    assert rep.scoreboard.rounds  # the scoreboard never needs the judge
    assert rep.best_line is not None and rep.best_line.chosen_by == "code"
    assert rep.best_line.quote in {t.turn_id: t.transcript for t in s.state.turns}[
        rep.best_line.turn_id]


def test_fallback_report_cannot_carry_scores():
    ev = [Evidence(turn_id="t0", quote="x")]
    with pytest.raises(ValidationError):
        DebateReport(
            session_id="s", motion="m", user_side="for", is_fallback=True,
            scores=[DebateDimensionScore(dimension=DebateDimension.composure, score=3,
                                         rationale="r", evidence=ev)],
            scoreboard=compute_scoreboard([], []),
            verdict=build_verdict([], compute_scoreboard([], []), "en", judged=False),
        )


# ---------------------------------------------------------------------------
# The scoreboard: code, from the debate record
# ---------------------------------------------------------------------------


async def test_scoreboard_is_computed_from_the_session_record(deps, tmp_path):
    _, rep = await debate(deps, tmp_path)
    sb = rep.scoreboard
    assert [r.round for r in sb.rounds] == ["opening", "clash", "closing"]
    assert [r.turns for r in sb.rounds] == [1, 2, 1]
    clash = sb.rounds[1]
    assert clash.concessions_won == 1 and sb.concessions_won == 1  # D8 conceded A1
    assert sb.opponent_arguments == 4 and sb.standing_at_end == 3
    assert sb.figures_questioned == 0  # motion 1 has no cards: nothing is challenged


async def test_scoreboard_from_trace_equals_scoreboard_from_session(deps, tmp_path):
    s, rep = await debate(deps, tmp_path, trace=True, sid="sb-trace")
    rows = rows_from_trace(tmp_path / "sb-trace.jsonl")
    assert rows == s.hooks.rows
    case = [a.id for a in s.hooks.setup.opponent_case]
    assert compute_scoreboard(rows, case) == rep.scoreboard


async def test_scoreboard_counts_figures_questioned_on_a_motion_with_cards(deps, tmp_path):
    from mockboard.modes.debate.motions import Side
    from mockboard.modes.debate.session import Length

    planted = ("Testland mein tourism share of GDP 30 percent hai in 2023, isliye tourism "
               "itna important nahi hai kyunki baaki sectors bade hain.")
    _, rep = await debate(deps, tmp_path, [OPENING, planted, CLASH_PLAIN, CLASH_PLAIN,
                                           CLASH_PLAIN, CLOSING],
                          motion_id="tourism_hill_towns", side=Side.against,
                          length=Length.standard)
    assert rep.scoreboard.figures_questioned == 1
    assert any(c.verdict.value == "contradicted" for c in rep.claims)


def test_scoreboard_counts_first_answers_and_new_concessions_only():
    rows = [
        {"round": "opening", "arguments_made": 2, "answered_ids": ["A1"], "conceded_ids": [],
         "standing_ids": ["A1", "A2", "A3"], "move": "opening_statement"},
        {"round": "clash", "arguments_made": 1, "answered_ids": ["A1", "A2"],
         "conceded_ids": ["A1"], "standing_ids": ["A2", "A3"], "move": "concede_point"},
        {"round": "clash", "arguments_made": 1, "answered_ids": ["A1", "A2"],
         "conceded_ids": ["A1"], "standing_ids": ["A2", "A3"], "move": "fact_challenge"},
    ]
    sb = compute_scoreboard(rows, ["A1", "A2", "A3"])
    assert [(r.answered, r.concessions_won, r.figures_questioned) for r in sb.rounds] == [
        (1, 0, 0), (1, 1, 1)
    ]
    assert (sb.answered, sb.concessions_won, sb.standing_at_end) == (2, 1, 2)


# ---------------------------------------------------------------------------
# The verdict: template only, traceable to the rubric, never takes a side
# ---------------------------------------------------------------------------


def _scores(values: dict[DebateDimension, int | str]) -> list[DebateDimensionScore]:
    out = []
    for d in DebateDimension:
        v = values.get(d, INSUFFICIENT_EVIDENCE)
        ev = [Evidence(turn_id="t0", quote="q")] if isinstance(v, int) else []
        out.append(DebateDimensionScore(dimension=d, score=v, rationale=f"{d.value} because",
                                        evidence=ev))
    return out


def _board(answered: int = 1, conceded: int = 0) -> DebateScoreboard:
    return DebateScoreboard(rounds=[], opponent_arguments=4, arguments_made=3,
                            answered=answered, standing_at_end=4 - conceded,
                            concessions_won=conceded, figures_questioned=0)


@pytest.mark.parametrize(("values", "answered", "label"), [
    ({d: 4 for d in DebateDimension}, 1, VerdictLabel.carried),
    ({d: 4 for d in DebateDimension}, 0, VerdictLabel.close),
    ({d: 3 for d in DebateDimension}, 2, VerdictLabel.close),
    ({d: 3 for d in DebateDimension}, 0, VerdictLabel.opponent_held),
    ({d: 2 for d in DebateDimension}, 3, VerdictLabel.opponent_held),
    ({DebateDimension.composure: 5, DebateDimension.clarity_structure: 5}, 2,
     VerdictLabel.no_verdict),
])
def test_verdict_label_table(values, answered, label):
    assert choose_label(_scores(values), _board(answered)) == label


def test_verdict_reasons_map_to_rubric_scores_and_the_scoreboard():
    scores = _scores({DebateDimension.rebuttal_quality: 5, DebateDimension.composure: 4,
                      DebateDimension.clarity_structure: 4, DebateDimension.evidence_use: 2})
    v = build_verdict(scores, _board(2, 1), "en")
    by_dim = {s.dimension: s for s in scores}
    rubric = [r for r in v.reasons if r.kind != "scoreboard"]
    assert [(r.kind, r.dimension) for r in rubric] == [
        ("strength", DebateDimension.rebuttal_quality),
        ("strength", DebateDimension.clarity_structure),  # ties break in rubric order
        ("work_on", DebateDimension.evidence_use),
    ]
    for r in rubric:
        assert r.score == by_dim[r.dimension].score
        assert by_dim[r.dimension].rationale in r.text
    assert [r.kind for r in v.reasons][-1] == "scoreboard"
    assert "answered 2 of the opponent's 4 arguments" in v.reasons[-1].text
    assert v.text.endswith(f"{DISCLAIMER}.")


def test_verdict_never_quotes_a_rationale_that_takes_a_side():
    scores = _scores({d: 4 for d in DebateDimension})
    scores[2] = scores[2].model_copy(update={"rationale": "The motion is clearly right here."})
    v = build_verdict(scores, _board(), "en")
    assert "clearly right" not in v.text and "motion is" not in v.text.split(DISCLAIMER)[0]
    assert takes_sides("The motion is clearly right here.")
    assert takes_sides("आपका पक्ष सही है")
    assert not takes_sides("The user restated the other side fairly.")


def test_verdict_adds_no_praise_of_its_own():
    scores = _scores({d: 4 for d in DebateDimension})
    v = build_verdict(scores, _board(), "en")
    for word in ("excellent", "brilliant", "great", "amazing", "superb", "won the debate"):
        assert word not in v.text.lower()


def test_verdict_in_hindi():
    v = build_verdict(_scores({d: 4 for d in DebateDimension}), _board(), ReportLanguage.hi)
    assert v.headline == "तर्क के बल पर बहस आपके नाम रही"
    assert "कौन सा पक्ष सही है" in v.text  # the disclaimer, in Hindi


# ---------------------------------------------------------------------------
# Language, CLI, prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "hi"])
async def test_report_language(deps, tmp_path, lang):
    _, rep = await debate(deps, tmp_path, language=lang)
    assert rep.report_language == ReportLanguage(lang)
    call = deps.llm.calls_named("judge_debate")[0]
    assert f"<report_language>\n{lang}\n</report_language>" in call.user
    text = format_debate_report(rep, deps.cards)
    devanagari = any("ऀ" <= ch <= "ॿ" for ch in rep.verdict.text + text)
    assert devanagari == (lang == "hi")


async def test_judge_sees_board_lines_but_quotes_only_user_turns(deps, tmp_path):
    s, _ = await debate(deps, tmp_path)
    user = build_debate_judge_user(
        motion=s.hooks.setup.motion.text, user_side="for", case=s.hooks.setup.opponent_case,
        turns=s.state.turns, ledger=[], board_lines=s.state.board_lines,
        language=ReportLanguage.en,
    )
    assert "board_reply" in user and "concede_point" in user
    block = user.split("<debate_session>\n", 1)[1].split("\n</debate_session>", 1)[0]
    rows = json.loads(block)
    assert rows[0]["board_said_before"] == s.state.board_lines[0].text  # the intro
    assert rows[0]["board_reply"]["move"] == "opening_statement"  # not the intro again
    assert code_best_line(s.state.turns, s.hooks.setup.opponent_case, s.hooks.allowed,
                          ReportLanguage.en).quote in s.state.turns[1].transcript


def test_cli_prints_the_debate_report(tmp_path, capsys):
    rc = cli.main([
        "--mode", "debate", "--fake", "--script", str(REPO_ROOT / "examples" / "fake_debate.txt"),
        "--session-id", "pytest-debate-report", "--trace-dir", str(tmp_path),
        "--report-json", str(tmp_path / "rep.json"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[Moderator Rao] The board's verdict:" in out
    for heading in ("DEBATE REPORT", "Moderator's verdict:", "Scoreboard (computed by code",
                    "Best line [t1]:", "Debate rubric"):
        assert heading in out
    rep = json.loads((tmp_path / "rep.json").read_text())
    assert rep["mode"] == "debate" and len(rep["scores"]) == 6
    event = json.loads((tmp_path / "pytest-debate-report.jsonl").read_text().splitlines()[-1])
    assert event["event"] == "report" and event["citation_check_passed"] is True


def test_judge_prompt_is_versioned_and_logged(deps):
    body = deps.mode("debate").kit.prompt("judge_debate").body
    for d in DebateDimension:
        assert d.value in body
    assert "never which side of the motion is right" in body
    assert "NO score for accent" in body and "best_line_quote" in body
    log = (REPO_ROOT / "src" / "mockboard" / "prompts" / "CHANGELOG.md").read_text()
    assert "| judge_debate | 1 |" in log
