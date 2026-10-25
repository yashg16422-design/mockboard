"""Judge: the post-session rubric report (orchestrator ruling 6).

Seven UPSC published qualities plus two Mock Board practice indicators, each scored 1-5 or
"insufficient_evidence", with verbatim evidence quotes and turn ids. There is no accent,
dialect, grammar or fluency dimension.

The evaluator-optimizer loop is bounded: after the judge drafts the report, the deterministic
`check_citations` confirms every quote appears in the cited turn's transcript. If any does
not, the judge is asked to revise once; quotes that still fail are dropped, and a score left
without evidence becomes "insufficient_evidence".
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence

from mockboard.brain.cards import CardSet
from mockboard.brain.llm import LLMClient
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    INSUFFICIENT_EVIDENCE,
    BoardMove,
    CandidateTurn,
    Claim,
    ComposureMoment,
    Dimension,
    DimensionScore,
    Evidence,
    ReportLanguage,
    SessionReport,
)
from mockboard.brain.tools import JUDGE_TOOL, JudgeEvidence, JudgeOutput


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\"'“”‘’.,!?;:।()\[\]-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in_turn(quote: str, turn: CandidateTurn | None) -> bool:
    """True if the quote occurs in the turn transcript (case/punctuation-insensitive)."""
    if turn is None or not quote.strip():
        return False
    return _norm(quote) in _norm(turn.transcript)


def check_citations(
    evidence: Sequence[JudgeEvidence], turns: Sequence[CandidateTurn]
) -> list[JudgeEvidence]:
    """Return the evidence items whose quote is NOT found in the cited turn."""
    by_id = {t.turn_id: t for t in turns}
    return [e for e in evidence if not quote_in_turn(e.quote, by_id.get(e.turn_id))]


def build_session_block(turns: Sequence[CandidateTurn], ledger: Sequence[Claim]) -> str:
    claims_by_turn: dict[str, list[dict]] = {}
    for c in ledger:
        claims_by_turn.setdefault(c.turn_id, []).append(
            {"claim": c.claim_en, "verdict": c.verdict.value, "card_id": c.card_id}
        )
    rows = [
        {
            "turn_id": t.turn_id,
            "topic": t.topic,
            "question": t.question,
            "candidate_transcript": t.transcript,
            "claims": claims_by_turn.get(t.turn_id, []),
        }
        for t in turns
    ]
    return tag("session", json.dumps(rows, ensure_ascii=False, indent=1))


def build_judge_user(
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    language: ReportLanguage,
    failed: Sequence[JudgeEvidence] = (),
) -> str:
    parts = [tag("report_language", language.value), build_session_block(turns, ledger)]
    if failed:
        listing = "\n".join(f"- {e.dimension.value} / {e.turn_id}: {e.quote!r}" for e in failed)
        parts.append(
            tag(
                "revision_request",
                "These evidence quotes were not found word for word in the cited turn. "
                "Replace each with an exact quote from that turn, or mark the dimension "
                f"insufficient_evidence:\n{listing}",
            )
        )
    parts.append("Call the tool now.")
    return "\n".join(parts)


async def run_judge(
    llm: LLMClient,
    kit: PromptKit,
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    language: ReportLanguage,
    *,
    session_id: str,
    failed: Sequence[JudgeEvidence] = (),
    max_tokens: int = 2500,
) -> JudgeOutput:
    return await llm.structured(
        name="judge",
        system=kit.system("judge"),
        user=build_judge_user(turns, ledger, language, failed),
        tool=JUDGE_TOOL,
        output_model=JudgeOutput,
        tag=session_id,
        max_tokens=max_tokens,
    )


def assemble_report(
    draft: JudgeOutput,
    *,
    session_id: str,
    turns: Sequence[CandidateTurn],
    ledger: Sequence[Claim],
    moves: Sequence[BoardMove],
    cards: CardSet,
    language: ReportLanguage,
    revisions: int,
    feature_flags: dict[str, bool],
    prompt_versions: dict[str, int],
) -> SessionReport:
    """Keep only verified evidence; a score left without evidence becomes insufficient."""
    failed = check_citations(draft.evidence, turns)
    failed_ids = {id(e) for e in failed}
    good: dict[Dimension, list[Evidence]] = {}
    for e in draft.evidence:
        if id(e) not in failed_ids:
            good.setdefault(e.dimension, []).append(Evidence(turn_id=e.turn_id, quote=e.quote))
    notes: list[str] = []
    scores: list[DimensionScore] = []
    for s in draft.scores:
        ev = good.get(s.dimension, [])
        score = s.score
        if isinstance(score, int) and not ev:
            notes.append(f"{s.dimension.value}: no verified evidence, marked insufficient_evidence")
            score = INSUFFICIENT_EVIDENCE
        scores.append(
            DimensionScore(dimension=s.dimension, score=score, rationale=s.rationale, evidence=ev)
        )
    if failed:
        notes.append(f"{len(failed)} evidence quote(s) dropped: not found in the cited turn")
    turn_ids = {t.turn_id for t in turns}
    used_cards = {c.card_id for c in ledger if c.card_id}
    return SessionReport(
        session_id=session_id,
        report_language=language,
        scores=scores,
        claims=list(ledger),
        card_sources={
            cid: (card.source_url or card.source_title or "")
            if not card.synthetic_example
            else "SYNTHETIC TEST CARD (no real source)"
            for cid in sorted(used_cards)
            if (card := cards.get(cid)) is not None
        },
        composure_moments=[
            ComposureMoment(turn_id=m.turn_id, note=m.note)
            for m in draft.composure_moments
            if m.turn_id in turn_ids
        ],
        moves=list(moves),
        model_answer=draft.model_answer,
        citation_check_passed=not failed,
        judge_revisions=revisions,
        notes=notes,
        feature_flags=feature_flags,
        prompt_versions=prompt_versions,
    )


def fallback_report(
    *, session_id: str, ledger: Sequence[Claim], moves: Sequence[BoardMove],
    language: ReportLanguage, reason: str, feature_flags: dict[str, bool],
    prompt_versions: dict[str, int],
) -> SessionReport:
    """When the judge is unavailable: no invented scores, only the deterministic parts."""
    return SessionReport(
        session_id=session_id,
        report_language=language,
        scores=[],
        claims=list(ledger),
        moves=list(moves),
        is_fallback=True,
        citation_check_passed=False,
        notes=[f"judge unavailable ({reason}); scores omitted rather than guessed"],
        feature_flags=feature_flags,
        prompt_versions=prompt_versions,
    )
