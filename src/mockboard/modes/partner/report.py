"""The study partner's report: "Your points, made stronger". No scores anywhere.

Built in two steps:
1. Code drafts the points from the session (`draft_points`): a point starts at the first
   substantive turn after the opener or a recap; its reason, example and the answer to Mitra's
   counterpoint are the speaker's own sentences (from the reason or example marker onward). This
   draft is also the report when the model call fails (`is_fallback`).
2. One Nova call (`partner_summary`, forced tool `record_partner_notes`) refines the draft: better
   quote spans, what is still open, up to three practice suggestions. Code then checks every quote
   (`check_citations`): point, reason, example and answer must appear in what the speaker said,
   and the counterpoint in what Mitra said (case- and punctuation-insensitive, like the judge's
   check). A field that fails is emptied; a point whose own text fails is dropped; suggestions or
   "open" lines with a figure the speaker never said, or with score words, are dropped. If no
   point survives, the code draft stands.

"How you were heard" rows (transcript, what was understood, what Mitra did next) are built by code
from the session state, like the panel's.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mockboard.brain.actor import token_budget
from mockboard.brain.llm import LLMClient, LLMError, with_backoff
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    BoardLine,
    BoardMove,
    CandidateTurn,
    HeardTurn,
    Move,
    ReportLanguage,
)
from mockboard.brain.tools import PARTNER_SUMMARY_TOOL, PartnerSummaryOutput
from mockboard.modes.partner.lines import (
    CLOSE_TARGET,
    CONFIRM_END_TARGET,
    REPEAT_TARGET,
    RESUME_TARGET,
    SKIP_TARGET,
    THINK_TARGET,
    says_unsaid_figure,
)
from mockboard.modes.partner.strategist import example_marker, reason_marker
from mockboard.voice.lines import split_sentences

TITLE = "Your points, made stronger"
MAX_NEXT_STEPS = 3
QUOTE_WORDS = 40  # a quoted field is cut to this many words (still a substring)
HEARD_SAID_WORDS = 80
NOTE_WORDS = 30

_SCORE_WORDS = re.compile(
    r"\b(?:score[sd]?|marks?|rating|rated|grade[sd]?|out of|percentile|rank(?:ed|ing)?)\b"
    r"|\d\s*/\s*\d",
    re.IGNORECASE,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PartnerPoint(_Model):
    point: str  # their claim, in their own words
    reason: str = ""
    example: str = ""
    counterpoint: str = ""  # what Mitra raised ("some would argue...")
    answer: str = ""  # how they answered it
    open: str = ""  # what is still open, one line
    turn_ids: list[str] = Field(default_factory=list)


class PartnerReport(_Model):
    mode: Literal["study_partner"] = "study_partner"
    title: str = TITLE
    session_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    report_language: ReportLanguage = ReportLanguage.en
    topic: str
    language: str = "hinglish"
    points: list[PartnerPoint] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list, max_length=MAX_NEXT_STEPS)
    heard: list[HeardTurn] = Field(default_factory=list)
    source: Literal["model", "code"] = "code"  # where the points came from
    is_fallback: bool = False  # the model call failed: the code draft is shown
    citation_check_passed: bool = True
    judge_revisions: int = 0  # the session's report event reads it; the partner never revises
    notes: list[str] = Field(default_factory=list)
    prompt_versions: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\"'“”‘’.,!?;:।()\[\]\-–—…]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def appears_in(quote: str, texts: Sequence[str]) -> bool:
    """True if the quote occurs in one of the texts (case- and punctuation-insensitive)."""
    q = _norm(quote)
    return bool(q) and any(q in _norm(t) for t in texts)


def _words(text: str, n: int) -> str:
    words = text.split()
    return text.strip() if len(words) <= n else " ".join(words[:n])


def _lead(text: str) -> str:
    """The first sentence of a turn, at most QUOTE_WORDS words (a substring of the turn)."""
    sentences = split_sentences(text.strip())
    return _words(sentences[0] if sentences else text, QUOTE_WORDS)


def _from_marker(text: str, find) -> str:
    """The speaker's own words from the first marker to the end of its sentence, or ""."""
    for sentence in split_sentences(text.strip()):
        m = find(sentence)
        if m is not None:
            return _words(sentence[m.start():].strip(), QUOTE_WORDS)
    return ""


def _without_first_sentence(line: str) -> str:
    sentences = split_sentences(line.strip())
    return " ".join(sentences[1:]) if len(sentences) > 1 else line.strip()


# The words a counterpoint opens with ("kuch log kahenge...", "some would argue..."). Nova may
# open the line with it instead of a restatement (seen on the first real run, Nov 8).
_COUNTER_MARKER = re.compile(
    r"kuch log|kai log|some would|some (?:people )?(?:argue|say|might)|others (?:argue|say)|"
    r"कुछ लोग|कई लोग|சிலர்|sila per",
    re.IGNORECASE,
)


def _counterpoint_of(line: str) -> str:
    """Mitra's counterpoint: from the sentence that raises it to the end of the line; without
    such a sentence, the line after its restating first sentence."""
    sentences = split_sentences(line.strip())
    for i, sentence in enumerate(sentences):
        if _COUNTER_MARKER.search(sentence):
            return " ".join(sentences[i:])
    return _without_first_sentence(line)


def _open_line(p: PartnerPoint) -> str:
    if not p.reason:
        return "Why this point holds is still open: give it a reason."
    if not p.example:
        return "One concrete example is still open."
    if p.counterpoint and not p.answer:
        return "Your answer to the counterpoint is still open."
    return "Say it once more in one go: point, reason, example, and your answer to the other side."


# ---------------------------------------------------------------------------
# Step 1: the code draft
# ---------------------------------------------------------------------------


def _by_turn(moves: Sequence[BoardMove]) -> dict[str, BoardMove]:
    out: dict[str, BoardMove] = {}
    for move in moves:
        if move.for_turn:
            out[move.for_turn] = move
    return out


def draft_points(
    turns: Sequence[CandidateTurn],
    moves: Sequence[BoardMove],
    board_lines: Sequence[BoardLine],
    counted: Mapping[str, bool],
) -> list[PartnerPoint]:
    """The points, from the session state alone. Pure."""
    move_for = _by_turn(moves)
    line_for = {b.turn_id: b.text for b in board_lines}
    points: list[PartnerPoint] = []
    current: PartnerPoint | None = None
    for turn in turns:
        text = turn.transcript
        if counted.get(turn.turn_id) and text:
            if current is None:
                current = PartnerPoint(point=_lead(text), turn_ids=[turn.turn_id])
                points.append(current)
            else:
                current.turn_ids.append(turn.turn_id)
                if current.counterpoint and not current.answer:
                    current.answer = _lead(text)
            if not current.reason:
                current.reason = _from_marker(text, reason_marker)
            if not current.example:
                current.example = _from_marker(text, example_marker)
        move = move_for.get(turn.turn_id)
        if move is None or current is None:
            continue
        if move.move == Move.counter and not current.counterpoint:
            current.counterpoint = _words(_counterpoint_of(line_for.get(turn.turn_id, "")),
                                          QUOTE_WORDS)
        if move.move == Move.move_on:
            current = None  # a recap or the close ends the point
    for p in points:
        p.open = _open_line(p)
    return points


def code_next_steps(points: Sequence[PartnerPoint]) -> list[str]:
    if not points:
        return ["Start with one point you are sure of, and say aloud why you believe it."]
    steps: list[str] = []
    if any(not p.reason for p in points):
        steps.append("Pick one point and say aloud why it holds, in two sentences.")
    if any(not p.example for p in points):
        steps.append("For each point, find one example you know well and practise saying it.")
    if any(p.counterpoint and not p.answer for p in points):
        steps.append("Take the counterpoint you did not answer and give a one-line reply to it.")
    if len(steps) < MAX_NEXT_STEPS:
        steps.append("Explain the whole topic again in two minutes, point by point.")
    return steps[:MAX_NEXT_STEPS]


def heard_rows(
    turns: Sequence[CandidateTurn], moves: Sequence[BoardMove], rows: Mapping[str, Mapping]
) -> list[HeardTurn]:
    """Each turn as Mitra heard it. Pure; built from session state only."""
    from mockboard.brain.director import copies_speech  # lazy: the director imports much

    move_for = _by_turn(moves)
    out: list[HeardTurn] = []
    for turn in turns:
        a, said = turn.analysis, turn.transcript
        row = rows.get(turn.turn_id, {})
        request = row.get("request", "")
        understood = ""
        if not said:
            status = "silence"
        elif request:
            status = "request"
        elif a is None or a.is_fallback or a.any_fallback:
            status = "not_analysed"
        elif a.garbled:
            status = "unclear"
        else:
            status = "honest_unknown" if a.honest_unknown else "answer"
            pieces = [p.strip() for p in [a.stance, *a.reasons[:2]]
                      if p.strip() and not copies_speech(p, said)]
            understood = _words("; ".join(dict.fromkeys(pieces)), NOTE_WORDS)
        out.append(HeardTurn(
            turn_id=turn.turn_id,
            member="Mitra",
            question=_words(turn.question, NOTE_WORDS),
            said=_words(said, HEARD_SAID_WORDS),
            understood=understood,
            status=status,
            request=REQUEST_WORDS.get(request, ""),
            request_source="the partner's rules" if request else "",
            next_step=_next_step(move_for.get(turn.turn_id)),
        ))
    return out


# What each request a turn made is called on the page ("How Mitra heard you").
REQUEST_WORDS: dict[str, str] = {
    "end": "end the session",
    "think": "a moment to think",
    "repeat": "hear the question again",
    "skip": "leave this point",
    "continue": "carry on (no clear yes to stopping)",
}
_FIXED_STEPS: dict[str, str] = {
    THINK_TARGET: "gave you time to think",
    REPEAT_TARGET: "said the question again",
    SKIP_TARGET: "left that point and asked for the next one",
    CONFIRM_END_TARGET: "checked whether you wanted to stop",
    RESUME_TARGET: "carried on with the same question",
}


def _next_step(move: BoardMove | None) -> str:
    if move is None:
        return ""
    if move.target in _FIXED_STEPS:
        return _FIXED_STEPS[move.target]
    if move.move == Move.move_on:
        return "closed the session" if move.target == CLOSE_TARGET else (
            "recapped your points and asked for the next one")
    return {
        Move.probe: "asked for your reason",
        Move.pressure: "asked for an example",
        Move.counter: "raised a counterpoint",
        Move.steelman: "asked for the strongest form of your point",
        Move.reassure: "offered a starting angle",
        Move.clarify: "asked you to say it again",
    }.get(move.move, move.move.value)


# ---------------------------------------------------------------------------
# Step 2: the model's notes, checked
# ---------------------------------------------------------------------------


def _clean_free_text(text: str, speech: str, words: int) -> str:
    text = _words(text.strip(), words)
    if not text or _SCORE_WORDS.search(text) or says_unsaid_figure(text, speech):
        return ""
    return text


def check_citations(
    out: PartnerSummaryOutput,
    speaker_texts: Sequence[str],
    partner_texts: Sequence[str],
    drafts: Sequence[PartnerPoint],
) -> tuple[list[PartnerPoint], list[str], list[str]]:
    """(points that survive, next steps that survive, problems found). Pure."""
    speech = " ".join(speaker_texts)
    problems: list[str] = []
    points: list[PartnerPoint] = []
    for i, raw in enumerate(out.points):
        if not raw.point:
            continue
        if not appears_in(raw.point, speaker_texts):
            problems.append(f"point {i + 1}: not in the speaker's words")
            continue
        fields: dict[str, str] = {}
        for name in ("reason", "example", "answer"):
            value = getattr(raw, name)
            if value and not appears_in(value, speaker_texts):
                problems.append(f"point {i + 1} {name}: not in the speaker's words")
                value = ""
            fields[name] = _words(value, QUOTE_WORDS)
        counter = raw.counterpoint
        if counter and not appears_in(counter, partner_texts):
            problems.append(f"point {i + 1} counterpoint: not in Mitra's words")
            counter = ""
        p = PartnerPoint(point=_words(raw.point, QUOTE_WORDS), counterpoint=_words(counter, 60),
                         **fields)
        draft = next((d for d in drafts if appears_in(p.point, [d.point]) or
                      appears_in(d.point, [p.point])), None)
        p.turn_ids = list(draft.turn_ids) if draft else []
        if not p.counterpoint and draft is not None and draft.counterpoint:
            p.counterpoint = draft.counterpoint  # Mitra's own words, from the session
        opened = _clean_free_text(raw.open, speech, 25)
        if raw.open and not opened:
            problems.append(f"point {i + 1} open: dropped (figure or score words)")
        p.open = opened or _open_line(p)
        points.append(p)
    steps: list[str] = []
    for step in out.next_steps:
        clean = _clean_free_text(step, speech, 30)
        if clean:
            steps.append(clean)
        elif step.strip():
            problems.append("a next step was dropped (figure or score words)")
    return points, steps[:MAX_NEXT_STEPS], problems


def build_summary_user(
    topic: str,
    turns: Sequence[CandidateTurn],
    board_lines: Sequence[BoardLine],
    drafts: Sequence[PartnerPoint],
) -> str:
    session = [
        {"turn_id": t.turn_id, "mitra_asked": t.question, "speaker_said": t.transcript}
        for t in turns if t.transcript
    ]
    lines = [{"turn_id": b.turn_id, "move": b.move.value, "line": b.text} for b in board_lines]
    draft = [p.model_dump(exclude={"turn_ids"}) for p in drafts]
    return "\n".join([
        "Write the study notes now and call the tool.",
        tag("topic", topic),
        tag("session_turns", json.dumps(session, ensure_ascii=False, indent=1)),
        tag("partner_lines", json.dumps(lines, ensure_ascii=False, indent=1)),
        tag("draft_points", json.dumps(draft, ensure_ascii=False, indent=1)),
    ])


async def generate_partner_report(
    llm: LLMClient,
    kit: PromptKit,
    *,
    prompt_id: str,
    session_id: str,
    topic: str,
    language: str,
    mix: str,
    turns: Sequence[CandidateTurn],
    moves: Sequence[BoardMove],
    board_lines: Sequence[BoardLine],
    rows: Mapping[str, Mapping[str, Any]],
    report_language: ReportLanguage = ReportLanguage.en,
    prompt_versions: Mapping[str, int] | None = None,
) -> PartnerReport:
    """The report: one checked model call over the code draft; the draft if the call fails."""
    counted = {k: bool(v.get("counted")) for k, v in rows.items()}
    drafts = draft_points(turns, moves, board_lines, counted)
    heard = heard_rows(turns, moves, rows)
    base = {
        "session_id": session_id, "topic": topic, "language": language, "heard": heard,
        "report_language": report_language, "prompt_versions": dict(prompt_versions or {}),
    }
    notes: list[str] = []
    if report_language != ReportLanguage.en:
        notes.append("The notes are in English; quotes stay in the words you used.")
    speaker_texts = [t.transcript for t in turns if t.transcript]
    if not speaker_texts:
        return PartnerReport(**base, points=[], next_steps=code_next_steps([]), source="code",
                             notes=[*notes, "Nothing was said, so there are no points."])
    try:
        out = await with_backoff(lambda: llm.structured(
            name="partner_summary",
            system=kit.system(prompt_id),
            user=build_summary_user(topic, turns, board_lines, drafts),
            tool=PARTNER_SUMMARY_TOOL,
            output_model=PartnerSummaryOutput,
            tag=session_id,
            max_tokens=min(token_budget(1500, mix), 5000),
        ))
    except LLMError as exc:
        notes.append(f"The notes were written by code ({type(exc).__name__}).")
        return PartnerReport(**base, points=drafts, next_steps=code_next_steps(drafts),
                             source="code", is_fallback=True, notes=notes)
    partner_texts = [b.text for b in board_lines]
    points, steps, problems = check_citations(out, speaker_texts, partner_texts, drafts)
    notes.extend(f"citation check: {p}" for p in problems)
    if not points:
        notes.append("No model point passed the quote check, so the code draft is shown.")
        return PartnerReport(**base, points=drafts, next_steps=steps or code_next_steps(drafts),
                             source="code", citation_check_passed=not problems, notes=notes)
    return PartnerReport(**base, points=points, next_steps=steps or code_next_steps(points),
                         source="model", citation_check_passed=not problems, notes=notes)


# ---------------------------------------------------------------------------
# Output: the web page and the text CLI
# ---------------------------------------------------------------------------


def partner_page_fields(report: PartnerReport) -> dict[str, Any]:
    """What the web page shows, under one key: `{"partner": {...}}`. No scores."""
    return {
        "partner": {
            "title": report.title,
            "topic": report.topic,
            "language": report.language,
            "points": [p.model_dump() for p in report.points],
            "next_steps": list(report.next_steps),
            "heard": [h.model_dump() for h in report.heard],
            "source": report.source,
            "is_fallback": report.is_fallback,
            "citation_check_passed": report.citation_check_passed,
            "notes": list(report.notes),
        }
    }


def format_partner_report(report: PartnerReport) -> str:
    """The plain-text report for the CLI and the saved report file."""
    lines = [
        "",
        "=" * 70,
        f"{report.title.upper()}  (session {report.session_id})",
        "=" * 70,
        f"Topic: {report.topic}",
    ]
    if report.is_fallback:
        lines.append("The notes were written by code from what you said (the model was not used).")
    if not report.points:
        lines.append("No points were recorded.")
    for i, p in enumerate(report.points, 1):
        lines.append(f"Point {i}: {p.point}")
        lines.append(f"  Your reason:   {p.reason or '(not given yet)'}")
        lines.append(f"  Your example:  {p.example or '(not given yet)'}")
        if p.counterpoint:
            lines.append(f"  Counterpoint:  {p.counterpoint}")
            lines.append(f"  Your answer:   {p.answer or '(not answered yet)'}")
        if p.open:
            lines.append(f"  Still open:    {p.open}")
    if report.next_steps:
        lines.append("Next steps:")
        lines.extend(f"  - {s}" for s in report.next_steps)
    if report.heard:
        lines.append("How you were heard:")
        for row in report.heard:
            lines.append(f"  {row.turn_id} {row.status}")
            if row.said:
                lines.append(f"    heard: {row.said}")
            if row.understood:
                lines.append(f"    understood: {row.understood}")
            if row.request:
                lines.append(f"    request: {row.request}")
            if row.next_step:
                lines.append(f"    next: Mitra {row.next_step}")
    lines.extend(f"note: {n}" for n in report.notes)
    return "\n".join(lines)
