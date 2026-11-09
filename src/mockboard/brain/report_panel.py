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
from mockboard.brain.panel_actor import question_part
from mockboard.brain.schemas import (
    UPSC_QUALITIES,
    Analysis,
    BoardMove,
    CandidateForm,
    CandidateTurn,
    Dimension,
    HeardTurn,
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
    "heard_turns",
    "outline_violations",
    "page_fields",
    "pick_weakest",
    "practice_plan",
    "score_turn",
]

REQUEST_KINDS = frozenset({"change_topic", "skip", "repeat", "clarify_question", "end", "think"})
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
    # An honest "I do not know" is not ranked as weak by itself: the board values it, and the plan
    # tells candidates to practise saying it (real-user review). Length and balance still count.
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


def _fixed_openings() -> tuple[str, ...]:
    from mockboard.brain.panel_lines import ALL_LINE_TABLES  # lazy: keep import order simple

    found = {
        text.strip()
        for table in ALL_LINE_TABLES
        for options in table.variants.values()
        for text in options
        if "{" not in text and not text.rstrip().endswith("?")
    }
    return tuple(sorted(found, key=len, reverse=True))


def strip_fixed_lines(text: str) -> str:
    """The question without a fixed line in front of it ("Zaroor, sawal phir se suniye. ...").

    `question_part` drops only acknowledgements of a few words; the repeat, skip and topic lines are
    longer, and a practice question must start with the question itself."""
    text = text.strip()
    openings = _fixed_openings()
    changed = True
    while changed:
        changed = False
        for opening in openings:
            if text.startswith(opening) and len(text) > len(opening):
                text = text[len(opening):].strip()
                changed = True
    return text


def _asked(question: str) -> str:
    stripped = strip_fixed_lines(question)
    return question_part(stripped) or stripped


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
    asked = _asked(turn.question)
    return WeakestMoment(
        turn_id=turn.turn_id,
        segment=turn.topic,
        member=member,
        question=asked,
        answer=turn.transcript,
        outline=list(outline),
        score=scored,
    )


REQUEST_WORDS: dict[str, str] = {
    "change_topic": "change the topic",
    "skip": "skip the question",
    "repeat": "hear the question again",
    "clarify_question": "have the question explained",
    "end": "end the interview",
    "think": "take a moment to think",
}
REQUEST_SOURCE_WORDS: dict[str, str] = {
    "rules": "the board's phrase rules",
    "rules+llm": "the board's phrase rules, and the listener agreed",
    "llm": "the listener (a language model), not the phrase rules",
}
NEXT_STEP_WORDS: dict[Move, str] = {
    Move.probe: "asked a follow-up on this answer",
    Move.counter: "asked you to weigh another side",
    Move.steelman: "asked you to make the other side's best case",
    Move.pressure: "asked for a reason or an example",
    Move.reassure: "gave you time and an easier place to start",
    Move.clarify: "asked you to say it again",
    Move.interrupt: "asked you to come to the main point",
    Move.fact_challenge: "asked where your figure came from",
    Move.repeat_question: "repeated the question",
    Move.rephrase_question: "put the question another way",
    Move.next_question: "asked the next prepared question",
    Move.move_on: "moved on",
}
HEARD_SAID_WORDS = 60
HEARD_NOTE_WORDS = 40
HEARD_NOTE = (
    "What the speech recogniser wrote, and the listener's short note on it. If either is not what "
    "you meant, the board misheard you: that is a limit of this tool, not a mark against you."
)


def _trim(text: str, limit: int) -> str:
    words = text.split()
    return " ".join(words[:limit]) + (" …" if len(words) > limit else "")


def _next_step(move: BoardMove | None, this: CandidateTurn, following: CandidateTurn | None,
               member_of: dict[str, str]) -> str:
    if move is None:
        return ""
    if move.rule_id == "D-END-CONFIRM":
        return "asked you to confirm ending the interview"
    if move.rule_id == "D-THINK":
        return "gave you time to think; the same question stood"
    if following is None and move.move == Move.move_on:
        return "closed the interview"
    if following is not None and following.topic != this.topic:
        return f"handed over to {member_of.get(following.topic, following.topic)}"
    return NEXT_STEP_WORDS.get(move.move, move.move.value.replace("_", " "))


def heard_turns(
    turns: Sequence[CandidateTurn],
    *,
    moves: Sequence[BoardMove] = (),
    requests: Sequence[PanelRequest] = (),
    member_of: dict[str, str] | None = None,
) -> list[HeardTurn]:
    """Each turn as the board heard it, in order. Pure; built from session state only.

    The transcript is shown as recognised, because the candidate is the only one who knows
    what they said. The note is the listener's paraphrase, never the judge's."""
    from mockboard.brain.director import copies_speech  # lazy: director imports much

    member_of = member_of or {}
    move_for: dict[str, BoardMove] = {}
    for move in moves:
        if move.for_turn:
            move_for[move.for_turn] = move  # the last move computed for a turn is the one used
    request_for = {r.turn_id: r for r in requests if r.kind in REQUEST_KINDS}
    rows: list[HeardTurn] = []
    for index, turn in enumerate(turns):
        analysis = turn.analysis
        said = turn.transcript
        request = request_for.get(turn.turn_id)
        understood = ""
        if not said:
            status = "silence"
        elif request is not None:
            status = "request"
        elif analysis is None or analysis.is_fallback or analysis.any_fallback:
            status = "not_analysed"
        elif analysis.garbled:
            status = "unclear"
        else:
            status = "honest_unknown" if analysis.honest_unknown else "answer"
            # Only the listener's own words: a piece that copies the transcript adds nothing,
            # and the director drops the same pieces from its memory (`copies_speech`).
            pieces = [
                p.strip() for p in [analysis.stance, *analysis.reasons[:2]]
                if p.strip() and not copies_speech(p, said)
            ]
            understood = _trim("; ".join(dict.fromkeys(pieces)), HEARD_NOTE_WORDS)
        following = turns[index + 1] if index + 1 < len(turns) else None
        rows.append(HeardTurn(
            turn_id=turn.turn_id,
            member=member_of.get(turn.topic, turn.topic),
            question=_asked(turn.question),
            said=_trim(said, HEARD_SAID_WORDS),
            understood=understood,
            status=status,
            request=REQUEST_WORDS.get(request.kind, "") if request is not None else "",
            request_source=(
                REQUEST_SOURCE_WORDS.get(request.source, request.source)
                if request is not None else ""
            ),
            next_step=_next_step(move_for.get(turn.turn_id), turn, following, member_of),
        ))
    return rows


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
            if t.turn_id in req_by_turn:
                continue  # a request is not what the candidate thinks
            gist = (t.analysis.stance if t.analysis and t.analysis.stance else "")
            if gist:
                notes.append(gist)
        kind = req_by_turn.get(group[-1].turn_id, "")
        if kind:
            wanted = REQUEST_WORDS.get(kind, kind.replace("_", " "))
            notes.append(f"The candidate asked to {wanted}.")
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
    # The weakest moment's segment keeps one place, so the plan names the part of the interview
    # this candidate found hardest, not only the same dimension lines for everyone.
    segment_line = PLAN_BY_SEGMENT.get(weakest.segment, "") if weakest is not None else ""
    room = limit - 1 if segment_line else limit
    numeric = [
        s for s in report.scores
        if s.dimension in UPSC_QUALITIES and isinstance(s.score, int)
    ]
    numeric.sort(key=lambda s: int(s.score))
    for score in numeric:
        line = PLAN_BY_DIMENSION.get(score.dimension)
        if line and line not in items and len(items) < room:
            items.append(line)
    if segment_line and segment_line not in items:
        items.append(segment_line)
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
    moves: Sequence[BoardMove] | None = None,
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
    member_of = {sid: names.get(mid, mid) for sid, mid in segment_members.items()}
    heard = heard_turns(
        turns, moves=report.moves if moves is None else moves, requests=requests,
        member_of=member_of,
    )
    return report.model_copy(update={
        "heard": heard,
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
                "questions": list(m.questions),
                "weakest_question": m.weakest_question,
                "outline": list(m.outline),
                "evidence": list(m.evidence),
            }
            for m in report.by_member
        ],
        "scores": [
            {
                "dimension": score.dimension.value,
                "score": score.score,
                "rationale": score.rationale,
                "practice_indicator": score.is_practice_indicator,
                "evidence": [item.quote for item in score.evidence],
            }
            for score in report.scores
        ],
        "practice_plan": list(report.practice_plan),
        "heard": [h.model_dump() for h in report.heard],
        "heard_note": HEARD_NOTE if report.heard else "",
        "weakest": None if report.weakest is None else {
            "question": report.weakest.question,
            "segment": report.weakest.segment,
            "member": report.weakest.member,
            "outline": list(report.weakest.outline),
            "turn_id": report.weakest.turn_id,
            # the first answer, so a second attempt can be compared with it (upgrade #3)
            "answer": report.weakest.answer,
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
