"""Utility B: the report grouped by member, the weakest moment, a practice plan, text replay.

`by_member`, `weakest` and `practice_plan` are built by code, not by the judge (CTO_NOTES_06 §8).
A garbled, silent, fallback or request-only turn is never the weakest. The outline is a fixed
structure template unless a checked `weakest_outline` is supplied. Replay is text-only behind
`ReplaySource` so audio can be plugged in later (Ruling 48).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mockboard.brain.actor import PLACE_FACT_PATTERNS, count_words
from mockboard.brain.schemas import (
    UPSC_QUALITIES,
    Analysis,
    CandidateForm,
    CandidateTurn,
    Dimension,
    MemberReport,
    Move,
    PanelRequest,
    PracticeCompare,
    SessionReport,
    WeakestMoment,
)

__all__ = [
    "DEFAULT_OUTLINE",
    "ReplaySource",
    "TextReplay",
    "decorate_report",
    "outline_violations",
    "page_fields",
    "pick_weakest",
    "practice_plan",
    "score_turn",
]

REQUEST_KINDS = frozenset({"change_topic", "skip", "repeat", "clarify_question", "end"})
PRESSURE_MOVES = frozenset({
    Move.pressure, Move.counter, Move.steelman, Move.fact_challenge, Move.interrupt,
})
DEFAULT_OUTLINE: tuple[str, ...] = (
    "Define the terms you will use, in one sentence.",
    "Name two sides of the question and who is affected.",
    "Say what you would do first, and why.",
    "Name one trade-off you would not ignore.",
)
_OUTLINE_OK = re.compile(r"^[A-Za-z][A-Za-z ,;'\-]*\.$")
PLAN_BY_DIMENSION: dict[Dimension, str] = {
    Dimension.mental_alertness:
        "Practise answering in two breaths: the point, then one reason.",
    Dimension.critical_assimilation:
        "Take one claim you made and name the strongest objection to it.",
    Dimension.clear_logical_exposition:
        "Say the same answer once more, in three short sentences only.",
    Dimension.balance_of_judgement:
        "For your weakest answer, write the other side first, then your view.",
    Dimension.variety_depth_of_interest:
        "Prepare one hobby or optional-subject story with a public-service link.",
    Dimension.social_cohesion_leadership:
        "Describe one time you brought people along, and what you would do next.",
    Dimension.intellectual_moral_integrity:
        "Practise saying I do not know, then the first step you would take to find out.",
}
PLAN_BY_SEGMENT: dict[str, str] = {
    "form_hr": "Prepare why this service, in one lived example, not a slogan.",
    "home_district": "Prepare one local problem you have seen, who it affected, and a first step.",
    "degree": "Link one idea from your subject to an everyday public problem.",
    "interests": "Prepare what a hobby taught you about patience or judgement.",
    "current_affairs": "Pick one issue you follow, two sides, and a first administrative step.",
}


def outline_violations(points: object) -> list[str]:
    """Why an outline is not fit to show. Empty = fit. Pure."""
    if not isinstance(points, (list, tuple)):
        return ["not a list"]
    if not (3 <= len(points) <= 5):
        return ["not 3 to 5 points"]
    problems: list[str] = []
    for point in points:
        if not isinstance(point, str):
            problems.append("a point is not text")
            continue
        text = " ".join(point.split())
        if not _OUTLINE_OK.match(text):
            problems.append("a point has a character a plain sentence does not")
        if count_words(text) > 20:
            problems.append("a point is over 20 words")
        if any(c.isdigit() for c in text):
            problems.append("a point has a digit")
        if any(re.search(p, text.lower()) for p in PLACE_FACT_PATTERNS):
            problems.append("a point states a place fact")
    return list(dict.fromkeys(problems))


def _analysis(turn: CandidateTurn) -> Analysis | None:
    return turn.analysis


def score_turn(turn: CandidateTurn, *, request_ids: set[str], pressure: bool) -> int | None:
    """How weak this answer looks. None means it must not be ranked (garbled, silent, request)."""
    analysis = _analysis(turn)
    text = turn.transcript
    if not text or (analysis is not None and analysis.word_count == 0):
        return None
    if analysis is not None and (analysis.garbled or analysis.is_fallback or analysis.any_fallback):
        return None
    kind = (analysis.candidate_request if analysis is not None else "none") or "none"
    if kind in REQUEST_KINDS or turn.turn_id in request_ids:
        return None
    score = 0
    if analysis is not None and analysis.honest_unknown:
        score += 4
    words = analysis.word_count if analysis is not None else count_words(text)
    if words < 12:
        score += 3
    elif words < 25:
        score += 1
    if analysis is not None and analysis.balance.value == "one_sided":
        score += 2
    if pressure:
        score += 1
    return score


def pick_weakest(
    turns: Sequence[CandidateTurn],
    *,
    requests: Sequence[PanelRequest] = (),
    pressure_turns: set[str] | None = None,
    outline: Sequence[str] = DEFAULT_OUTLINE,
    names: dict[str, str] | None = None,
) -> WeakestMoment | None:
    request_ids = {r.turn_id for r in requests if r.kind in REQUEST_KINDS}
    pressure_turns = pressure_turns or set()
    ranked: list[tuple[int, CandidateTurn]] = []
    for turn in turns:
        scored = score_turn(
            turn, request_ids=request_ids, pressure=turn.turn_id in pressure_turns,
        )
        if scored is not None:
            ranked.append((scored, turn))
    if not ranked:
        return None
    scored, turn = max(ranked, key=lambda pair: (pair[0], pair[1].turn_index))
    member = ""
    if names:
        member = names.get(turn.topic, turn.topic)
    return WeakestMoment(
        turn_id=turn.turn_id,
        segment=turn.topic,
        member=member,
        question=turn.question,
        answer=turn.transcript,
        outline=list(outline),
        score=scored,
    )


def _member_name(segment_id: str, names: dict[str, str], segment_members: dict[str, str]) -> str:
    persona = segment_members.get(segment_id, segment_id)
    return names.get(persona, persona)


def by_member(
    turns: Sequence[CandidateTurn],
    *,
    names: dict[str, str],
    segment_members: dict[str, str],
    requests: Sequence[PanelRequest],
    weakest: WeakestMoment | None,
    evidence: dict[str, list[str]],
) -> list[MemberReport]:
    """One block per segment, in the order the segments first appeared."""
    order: list[str] = []
    grouped: dict[str, list[CandidateTurn]] = {}
    for turn in turns:
        grouped.setdefault(turn.topic, []).append(turn)
        if turn.topic not in order:
            order.append(turn.topic)
    req_by_turn = {r.turn_id: r.kind for r in requests}
    blocks: list[MemberReport] = []
    for segment in order:
        group = grouped[segment]
        asked = [t.question for t in group if t.question]
        notes = []
        for t in group:
            gist = (t.analysis.stance if t.analysis and t.analysis.stance else "")
            if gist:
                notes.append(gist)
        kind = req_by_turn.get(group[-1].turn_id, "")
        if kind:
            notes.append(f"The candidate asked to {kind.replace('_', ' ')}.")
        quotes = []
        for t in group:
            quotes.extend(evidence.get(t.turn_id, [])[:1])
        weak_q = weakest.question if weakest is not None and weakest.segment == segment else ""
        outline = list(weakest.outline) if weak_q else []
        blocks.append(MemberReport(
            name=_member_name(segment, names, segment_members),
            segment=segment,
            notes=" ".join(notes)[:400],
            questions=asked,
            weakest_question=weak_q,
            outline=outline,
            evidence=quotes[:3],
        ))
    return blocks


def practice_plan(
    report: SessionReport, weakest: WeakestMoment | None, *, limit: int = 3
) -> list[str]:
    items: list[str] = []
    numeric = [
        s for s in report.scores
        if s.dimension in UPSC_QUALITIES and isinstance(s.score, int)
    ]
    numeric.sort(key=lambda s: int(s.score))
    for score in numeric:
        line = PLAN_BY_DIMENSION.get(score.dimension)
        if line and line not in items:
            items.append(line)
        if len(items) >= limit:
            return items[:limit]
    if weakest is not None:
        extra = PLAN_BY_SEGMENT.get(weakest.segment)
        if extra and extra not in items:
            items.append(extra)
    return items[:limit]


def decorate_report(
    report: SessionReport,
    turns: Sequence[CandidateTurn],
    *,
    names: dict[str, str],
    segment_members: dict[str, str],
    requests: Sequence[PanelRequest] = (),
    pressure_turns: set[str] | None = None,
    form: CandidateForm | None = None,
    outline: Sequence[str] = DEFAULT_OUTLINE,
) -> SessionReport:
    """Attach by_member, weakest and practice_plan. Safe on a fallback report."""
    if outline_violations(list(outline)):
        outline = DEFAULT_OUTLINE
    weakest = pick_weakest(
        turns, requests=requests, pressure_turns=pressure_turns, outline=outline, names={
            sid: names.get(mid, mid) for sid, mid in segment_members.items()
        },
    )
    evidence: dict[str, list[str]] = {}
    for score in report.scores:
        for item in score.evidence:
            evidence.setdefault(item.turn_id, []).append(item.quote)
    members = by_member(
        turns, names=names, segment_members=segment_members, requests=requests,
        weakest=weakest, evidence=evidence,
    )
    plan = practice_plan(report, weakest)
    return report.model_copy(update={
        "by_member": members,
        "weakest": weakest,
        "practice_plan": plan,
        "form": form,
    })


def page_fields(report: SessionReport) -> dict[str, Any]:
    """The optional fields the page groups the report by (WEB_REQUESTS.md)."""
    return {
        "members": [
            {
                "name": m.name,
                "notes": m.notes,
                "weakest_question": m.weakest_question,
                "outline": list(m.outline),
            }
            for m in report.by_member
        ],
        "practice_plan": list(report.practice_plan),
        "weakest": None if report.weakest is None else {
            "question": report.weakest.question,
            "segment": report.weakest.segment,
            "member": report.weakest.member,
            "outline": list(report.weakest.outline),
            "turn_id": report.weakest.turn_id,
        },
    }


class ReplaySource(Protocol):
    """How a replay moment is stored and compared. Text now; audio can implement this later."""

    def load(self, path: Path) -> SessionReport: ...

    def compare(self, before: WeakestMoment, after: CandidateTurn) -> PracticeCompare: ...


@dataclass(frozen=True)
class TextReplay:
    """Ruling 48: text-only replay. No audio cache."""

    def load(self, path: Path) -> SessionReport:
        return SessionReport.model_validate_json(path.read_text(encoding="utf-8"))

    def compare(self, before: WeakestMoment, after: CandidateTurn) -> PracticeCompare:
        analysis = after.analysis
        reasons = list(analysis.reasons) if analysis is not None else []
        balance = analysis.balance.value if analysis is not None else "unclear"
        after_words = analysis.word_count if analysis is not None else count_words(after.transcript)
        before_words = count_words(before.answer)
        return PracticeCompare(
            before_words=before_words,
            after_words=after_words,
            before_balance="",
            after_balance=balance,
            after_reasons=reasons,
            word_gain=after_words - before_words,
        )
