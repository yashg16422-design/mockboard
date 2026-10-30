"""The debate judge and report (CTO_NOTES_04 §4, DB6).

The report graph has the same shape as the UPSC one and is wired by the same function
(`graphs.wire_report_graph`): judge -> citation check -> at most one revision -> report. Only
three pieces are debate-specific:

1. **The judge call** (`judge_debate.v1.md`, forced tool `record_debate_report`): six debate
   dimensions, each 1-5 or "insufficient_evidence" with verbatim quotes and turn ids, plus one
   "best line" from the user's own speech with a reason.
2. **The citation check** verifies every evidence quote AND the best line against the cited
   turn's transcript, with the UPSC check (`brain.judge.check_citations`). Quotes that fail
   go back to the judge once; a score still without verified evidence becomes
   "insufficient_evidence".
3. **The report** adds what code computes: the round-by-round scoreboard (`scoreboard.py`,
   from the debate record), the Moderator's verdict (`verdict.py`, by template), and a code
   fallback for the best line when the judge's pick fails a check.

Board lines are shown to the judge for context, but only the user's own transcripts can be
quoted: the citation check looks only at the user's turns.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, TypedDict

from langgraph.runtime import Runtime

from mockboard.brain.deps import BrainDeps
from mockboard.brain.graphs import wire_report_graph
from mockboard.brain.judge import check_citations, quote_in_turn
from mockboard.brain.llm import LLMClient, LLMError
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    INSUFFICIENT_EVIDENCE,
    BestLine,
    BoardLine,
    BoardMove,
    CandidateTurn,
    Claim,
    DebateDimension,
    DebateDimensionScore,
    DebateReport,
    DebateScoreboard,
    Evidence,
    PointStrength,
    ReportLanguage,
)
from mockboard.brain.tools import DEBATE_JUDGE_TOOL, DebateJudgeOutput
from mockboard.modes.debate.guards import FigurePolicy, sentence_allowed, split_sentences
from mockboard.modes.debate.motions import CaseArgument
from mockboard.modes.debate.scoreboard import compute_scoreboard
from mockboard.modes.debate.verdict import build_verdict, takes_sides

MODE = "debate"


class DebateReportState(TypedDict, total=False):
    session_id: str
    language: ReportLanguage
    turns: list[CandidateTurn]
    ledger: list[Claim]
    moves: list[BoardMove]
    board_lines: list[BoardLine]
    rows: list[dict]
    motion: str
    user_side: str
    case: list[CaseArgument]
    allowed: FigurePolicy
    draft: DebateJudgeOutput | None
    failed: list
    revisions: int
    error: str
    report: DebateReport


# ---------------------------------------------------------------------------
# The judge call
# ---------------------------------------------------------------------------


def _replies(board_lines: Sequence[BoardLine]) -> dict[str, BoardLine]:
    """The board's reply to each user turn. The first board line is the Moderator's intro
    (also stamped t0), so it is skipped."""
    return {b.turn_id: b for b in list(board_lines)[1:]}


def build_debate_session_block(
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    board_lines: Sequence[BoardLine],
) -> str:
    claims_by_turn: dict[str, list[dict]] = {}
    for c in ledger:
        claims_by_turn.setdefault(c.turn_id, []).append(
            {"claim": c.claim_en, "verdict": c.verdict.value, "card_id": c.card_id}
        )
    replies = _replies(board_lines)
    rows = []
    for t in turns:
        a = t.analysis
        reply = replies.get(t.turn_id)
        rows.append(
            {
                "turn_id": t.turn_id,
                "round": t.topic,
                "board_said_before": t.question,
                "user_speech": t.transcript,
                "board_reply": (
                    {"move": reply.move.value, "text": reply.text} if reply else None
                ),
                "opponent_arguments_answered": list(a.rebuts) if a else [],
                "question_to_opponent": a.question_to_opponent if a else "",
                "user_conceded_a_point": bool(a and a.concedes_point),
                "honest_unknown": bool(a and a.honest_unknown),
                "claims": claims_by_turn.get(t.turn_id, []),
            }
        )
    return tag("debate_session", json.dumps(rows, ensure_ascii=False, indent=1))


def build_debate_judge_user(
    *,
    motion: str,
    user_side: str,
    case: Sequence[CaseArgument],
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    board_lines: Sequence[BoardLine],
    language: ReportLanguage,
    failed: Sequence[Any] = (),
) -> str:
    debate = {
        "motion": motion,
        "user_side": user_side,
        "opponent_case": [{"id": a.id, "claim": a.claim} for a in case],
    }
    parts = [
        tag("report_language", language.value),
        tag("debate", json.dumps(debate, ensure_ascii=False, indent=1)),
        build_debate_session_block(turns, ledger, board_lines),
    ]
    if failed:
        listing = "\n".join(f"- {e.dimension} / {e.turn_id}: {e.quote!r}" for e in failed)
        parts.append(
            tag(
                "revision_request",
                "These quotes were not found word for word in the cited turn's user_speech. "
                "Replace each with an exact quote from that turn, or mark the dimension "
                "insufficient_evidence (for best_line, pick another exact sentence):\n"
                + listing,
            )
        )
    parts.append("Call the tool now.")
    return "\n".join(parts)


async def run_debate_judge(
    llm: LLMClient,
    kit: PromptKit,
    state: DebateReportState,
    failed: Sequence[Any] = (),
    *,
    max_tokens: int = 2500,
) -> DebateJudgeOutput:
    return await llm.structured(
        name="judge_debate",
        system=kit.system("judge_debate"),
        user=build_debate_judge_user(
            motion=state.get("motion", ""),
            user_side=state.get("user_side", ""),
            case=state.get("case", []),
            turns=state.get("turns", []),
            ledger=state.get("ledger", []),
            board_lines=state.get("board_lines", []),
            language=state.get("language", ReportLanguage.en),
            failed=failed,
        ),
        tool=DEBATE_JUDGE_TOOL,
        output_model=DebateJudgeOutput,
        tag=state["session_id"],
        max_tokens=max_tokens,
    )


# ---------------------------------------------------------------------------
# The best line
# ---------------------------------------------------------------------------

_WARRANT = re.compile(
    r"\b(?:kyunki|kyonki|because|since|isliye|so that|which means|jisse|taaki|therefore|"
    r"for example|jaise ki)\b|क्योंकि|इसलिए",
    re.IGNORECASE,
)
_STRENGTH = {PointStrength.strong: 3, PointStrength.adequate: 2, PointStrength.weak: 1}

_BEST_REASON = {
    "en": {
        "rebut_warrant": "It answers the opponent's point (\"{claim}\") and gives a reason, "
                         "not just a claim.",
        "rebut": "It answers the opponent's point (\"{claim}\") directly.",
        "warrant": "It makes a claim and gives the reason for it in one sentence.",
        "plain": "It states your position in one sentence.",
    },
    "hi": {
        "rebut_warrant": "यह विपक्ष के तर्क (\"{claim}\") का जवाब देती है और केवल दावा नहीं, "
                         "कारण भी बताती है।",
        "rebut": "यह विपक्ष के तर्क (\"{claim}\") का सीधा जवाब देती है।",
        "warrant": "यह एक ही वाक्य में दावा और उसका कारण, दोनों बताती है।",
        "plain": "यह एक वाक्य में आपका पक्ष रखती है।",
    },
}


def _eligible(turn: CandidateTurn) -> bool:
    a = turn.analysis
    return bool(turn.transcript) and not (a and (a.off_limits or a.garbled))


def code_best_line(
    turns: Sequence[CandidateTurn],
    case: Sequence[CaseArgument],
    allowed: FigurePolicy,
    language: ReportLanguage,
) -> BestLine | None:
    """A deterministic best line when the judge's pick is missing or fails a check: the
    user's most substantive turn (point strength, answered an opponent argument, gave a
    reason), then its sentence that gives a reason, without any figure that is not on the
    motion's cards. Never from an off-limits or garbled turn."""
    claims = {a.id: a.claim for a in case}
    best: tuple[tuple[int, int], CandidateTurn, str] | None = None
    for index, turn in enumerate(turns):
        if not _eligible(turn):
            continue
        sentences = [
            s for s in split_sentences(turn.transcript)
            if len(s.split()) >= 6 and not s.endswith("?") and sentence_allowed(s, allowed)
        ]
        if not sentences:
            continue
        a = turn.analysis
        rebuts = [r for r in (a.rebuts if a else []) if r in claims]
        warranted = [s for s in sentences if _WARRANT.search(s)]
        score = (
            _STRENGTH.get(a.point_strength, 1) if a and a.point_strength else 1
        ) + int(bool(rebuts)) + int(bool(warranted))
        sentence = (warranted or sorted(sentences, key=len, reverse=True))[0]
        key = (score, -index)
        if best is None or key > best[0]:
            best = (key, turn, sentence)
    if best is None:
        return None
    _, turn, sentence = best
    lang = language.value if language.value in _BEST_REASON else "en"
    reasons = _BEST_REASON[lang]
    a = turn.analysis
    rebuts = [r for r in (a.rebuts if a else []) if r in claims]
    warrant = bool(_WARRANT.search(sentence))
    if rebuts:
        key = "rebut_warrant" if warrant else "rebut"
        reason = reasons[key].format(claim=claims[rebuts[0]].rstrip("."))
    else:
        reason = reasons["warrant" if warrant else "plain"]
    return BestLine(turn_id=turn.turn_id, quote=sentence, reason=reason, chosen_by="code")


def judge_best_line(
    draft: DebateJudgeOutput,
    turns: Sequence[CandidateTurn],
    allowed: FigurePolicy,
) -> tuple[BestLine | None, str]:
    """The judge's best line if it passes every check, else (None, why not)."""
    cited = draft.best_line
    if cited is None:
        return None, "the judge gave no best line"
    by_id = {t.turn_id: t for t in turns}
    turn = by_id.get(cited.turn_id)
    if not quote_in_turn(cited.quote, turn):
        return None, "the judge's best line was not found in the cited turn"
    if turn is None or not _eligible(turn):
        return None, "the judge's best line came from an off-limits or garbled turn"
    if not sentence_allowed(cited.quote, allowed):
        return None, "the judge's best line has a figure that is not on the motion's cards"
    reason = " ".join(draft.best_line_reason.split())
    if not reason or takes_sides(reason):
        return None, "the judge's best-line reason was empty or took a side on the motion"
    return BestLine(turn_id=cited.turn_id, quote=cited.quote.strip(), reason=reason,
                    chosen_by="judge"), ""


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _common(state: DebateReportState, deps: BrainDeps) -> dict[str, Any]:
    s = deps.settings
    ledger = state.get("ledger", [])
    used_cards = {c.card_id for c in ledger if c.card_id}
    return dict(
        session_id=state["session_id"],
        report_language=state.get("language", ReportLanguage.en),
        motion=state.get("motion", ""),
        user_side=state.get("user_side", "for"),
        claims=list(ledger),
        card_sources={
            cid: (card.source_url or card.source_title or "")
            if not card.synthetic_example
            else "SYNTHETIC TEST CARD (no real source)"
            for cid in sorted(used_cards)
            if (card := deps.cards.get(cid)) is not None
        },
        moves=list(state.get("moves", [])),
        feature_flags=s.feature_flags(),
        prompt_versions=s.prompt_versions(MODE),
    )


def scoreboard_for(state: DebateReportState) -> DebateScoreboard:
    return compute_scoreboard(state.get("rows", []), [a.id for a in state.get("case", [])])


def assemble_debate_report(
    draft: DebateJudgeOutput, state: DebateReportState, deps: BrainDeps
) -> DebateReport:
    """Keep only verified evidence; a score left without evidence becomes insufficient."""
    turns = state.get("turns", [])
    language = state.get("language", ReportLanguage.en)
    allowed = state.get("allowed") or FigurePolicy()
    failed = check_citations(draft.citations(), turns)
    failed_ids = {id(e) for e in failed}
    good: dict[DebateDimension, list[Evidence]] = {}
    for e in draft.evidence:
        if id(e) not in failed_ids:
            good.setdefault(e.dimension, []).append(Evidence(turn_id=e.turn_id, quote=e.quote))
    notes: list[str] = []
    scores: list[DebateDimensionScore] = []
    for s in draft.scores:
        ev = good.get(s.dimension, [])
        score = s.score
        if isinstance(score, int) and not ev:
            notes.append(f"{s.dimension.value}: no verified evidence, marked insufficient_evidence")
            score = INSUFFICIENT_EVIDENCE
        scores.append(DebateDimensionScore(
            dimension=s.dimension, score=score, rationale=s.rationale, evidence=ev
        ))
    evidence_failed = [e for e in failed if e.dimension != "best_line"]
    if evidence_failed:
        notes.append(
            f"{len(evidence_failed)} evidence quote(s) dropped: not found in the cited turn"
        )
    best, why_not = judge_best_line(draft, turns, allowed)
    if best is None:
        best = code_best_line(turns, state.get("case", []), allowed, language)
        notes.append(f"best line chosen by rule: {why_not}")
    board = scoreboard_for(state)
    return DebateReport(
        scores=scores,
        scoreboard=board,
        best_line=best,
        verdict=build_verdict(scores, board, language),
        citation_check_passed=not failed,
        judge_revisions=state.get("revisions", 0),
        notes=notes,
        **_common(state, deps),
    )


def fallback_debate_report(
    state: DebateReportState, deps: BrainDeps, reason: str
) -> DebateReport:
    """The judge is unavailable: no scores (none are guessed). The scoreboard and a rule-chosen
    best line still come from the debate record, and the Moderator says there is no verdict."""
    language = state.get("language", ReportLanguage.en)
    board = scoreboard_for(state)
    best = code_best_line(
        state.get("turns", []), state.get("case", []), state.get("allowed") or FigurePolicy(),
        language,
    )
    return DebateReport(
        scores=[],
        scoreboard=board,
        best_line=best,
        verdict=build_verdict([], board, language, judged=False),
        is_fallback=True,
        citation_check_passed=False,
        judge_revisions=state.get("revisions", 0),
        notes=[f"judge unavailable ({reason}); scores omitted rather than guessed"],
        **_common(state, deps),
    )


# ---------------------------------------------------------------------------
# The graph (same wiring as the UPSC report graph)
# ---------------------------------------------------------------------------


async def _run(deps: BrainDeps, state: DebateReportState, failed: list) -> DebateJudgeOutput:
    return await run_debate_judge(
        deps.llm, deps.mode(MODE).kit, state, failed, max_tokens=deps.settings.judge_max_tokens
    )


async def debate_citation_check(state: DebateReportState) -> dict[str, Any]:
    draft = state.get("draft")
    if draft is None:
        return {"failed": []}
    return {"failed": check_citations(draft.citations(), state.get("turns", []))}


async def debate_report(
    state: DebateReportState, runtime: Runtime[BrainDeps]
) -> dict[str, Any]:
    deps = runtime.context
    draft = state.get("draft")
    if draft is None:
        reason = state.get("error") or "no draft"
        return {"report": fallback_debate_report(state, deps, reason)}
    try:
        rep = assemble_debate_report(draft, state, deps)
    except (ValueError, LLMError) as exc:
        rep = fallback_debate_report(state, deps, f"assembly failed: {type(exc).__name__}")
    return {"report": rep}


def compile_debate_report_graph(
    *, max_attempts: int = 3, initial_interval_s: float = 0.5, jitter: bool = True
):
    return wire_report_graph(
        DebateReportState,
        run=_run,
        check=debate_citation_check,
        report_node=debate_report,
        max_attempts=max_attempts,
        initial_interval_s=initial_interval_s,
        jitter=jitter,
    ).compile(name="debate_report")


async def generate_debate_report(
    deps: BrainDeps,
    *,
    session_id: str,
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    moves: Sequence[BoardMove],
    board_lines: Sequence[BoardLine],
    rows: Sequence[dict],
    motion: str,
    user_side: str,
    case: Sequence[CaseArgument],
    allowed: FigurePolicy,
    language: ReportLanguage | str | None = None,
    graph: Any = None,
    callbacks: Sequence[Any] = (),
) -> DebateReport:
    """Run the debate report graph and return the validated report."""
    s = deps.settings
    graph = graph or compile_debate_report_graph(
        max_attempts=s.graph_retry_max_attempts,
        initial_interval_s=s.graph_retry_initial_interval_s,
        jitter=s.graph_retry_jitter,
    )
    cfg: dict[str, Any] = {"callbacks": list(callbacks)} if callbacks else {}
    out = await graph.ainvoke(
        {
            "session_id": session_id,
            "language": ReportLanguage(language or s.report_language),
            "turns": list(turns),
            "ledger": list(ledger),
            "moves": list(moves),
            "board_lines": list(board_lines),
            "rows": [dict(r) for r in rows],
            "motion": motion,
            "user_side": user_side,
            "case": list(case),
            "allowed": allowed,
            "revisions": 0,
            "failed": [],
        },
        cfg,
        context=deps,
    )
    return out["report"]
