"""Likely questions from the form, each with a hint on how to approach it (P9, Utility A).

    form -> fallback agenda (pure, no model call) -> questions per member
         -> hints: template (no model call), or `summary.v1` on Nova, validated one by one

The questions are the ones the board prepares from the form (`build_fallback_agenda`, uncapped
budgets): the questions it asks when its planner is off or late. The board may word them
differently and will ask follow-ups, and the page says so. They are in the interview language the
form names. A hint is about structure and angle (where to start, what to weigh, what to be honest
about), never a fact, and is always in English, because it is written for the person preparing.

Calling Nova costs credits, so the server route puts the model path behind access admission
(`voice/server.py`, `access/limits.py`); the template path makes no call and is always free.

What Nova may and may not write is in `prompts/summary.v1.md`. Code decides which hint is fit to
show (`hint_violations`): a hint that fails any check is replaced ALONE by its own template hint,
so a bad reply cannot show the candidate anything the templates could not.

# UNVERIFIED: how Nova's hints read and how often they pass. The checks are conservative but a
# regex cannot prove a sentence states no fact; the prompt is the other net.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from mockboard.brain.actor import PLACE_FACT_PATTERNS
from mockboard.brain.agenda import (
    _WORK_SEEDS,
    CURRENT_AFFAIRS,
    DEFAULT_SEGMENT_BUDGETS,
    DEGREE,
    FORM_HR,
    HOME_DISTRICT,
    INTERESTS,
    _unbacked_capitals,
    build_fallback_agenda,
)
from mockboard.brain.agenda_plan import names_the_place
from mockboard.brain.llm import LLMClient, LLMError
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import Agenda, CandidateForm
from mockboard.brain.tools import HINTS_TOOL, HintsOutput

__all__ = [
    "MAX_HINT_WORDS",
    "MIN_HINT_WORDS",
    "NOTE",
    "TEMPLATE_HINTS",
    "HintReport",
    "LikelyQuestion",
    "LikelyQuestions",
    "build_hints_user",
    "format_likely_questions",
    "hint_violations",
    "kit_with_summary",
    "likely_questions",
    "template_questions",
]

MIN_HINT_WORDS = 6
MAX_HINT_WORDS = 30
MAX_HINT_TOKENS = 1200
# Said once, under the list, in the CLI and the page: what these questions are and are not.
NOTE = (
    "These are questions the board prepares from your form. It may word them differently and it "
    "will ask follow-ups on what you say, so use them to practise, not to memorise."
)

# Template hints by segment, English, structure and angle only: no fact, no figure, no name. A
# hint is picked by the question's position in its segment, wrapping round. `hint_violations`
# holds every one of them to the same rules as Nova's (a test checks).
TEMPLATE_HINTS: dict[str, tuple[str, ...]] = {
    FORM_HR: (
        "Start with one real experience that pointed you here, then say what you want to do with "
        "the chance.",
        "Be honest about your reasons and name one other path that would still let you serve "
        "people.",
        "Keep work experience to what you did and what it taught you, in two or three sentences.",
    ),
    HOME_DISTRICT: (
        "Describe one problem you saw yourself, who it affected and the first step you would "
        "take.",
        "Say what you would change first and name one trade-off a real administrator would face.",
        "Stay with what you have seen yourself, and say who gains and who may lose from the "
        "change you suggest.",
    ),
    DEGREE: (
        "Link one idea from your subject to an everyday problem, and say where the idea stops "
        "working.",
        "Pick one concept you know well and explain it simply, as you would to a neighbour.",
        "Name one way your subject could help a public decision and one limit of that help.",
    ),
    INTERESTS: (
        "Say what the hobby taught you about patience or judgement, with one small moment as "
        "proof.",
        "Be honest about what was hard and what you did when it was, rather than only the wins.",
        "Explain why you chose your optional subject and one question it still leaves open for "
        "you.",
    ),
    CURRENT_AFFAIRS: (
        "Choose an issue you have followed closely and say plainly what you do not yet know about "
        "it.",
        "Weigh two sides before you decide, then say what you would do first and why.",
        "For a district situation, name who is involved, your first step and whom you would "
        "consult.",
    ),
}

# What a hint may be made of: letters, spaces, comma, semicolon, apostrophe and hyphen, ending in a
# single full stop. No digit, quote, bracket, colon, markup or non-Latin script.
_HINT_OK = re.compile(r"^[A-Za-z][A-Za-z ,;'\-]*\.$")
# A hint that cites or counts is stating a fact, whatever it says.
_FACT_WORDS = re.compile(
    r"\b(?:according to|as per|studies show|research shows?|data shows?|statistics?|survey|"
    r"percent|per cent|crore|lakh|million|billion|thousand|hundred|census|article|section|act)\b",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|override|bypass|jailbreak|instructions?|prompts?|assistant|forget)\b",
    re.IGNORECASE,
)
_ACRONYMS = frozenset({"dm", "sp", "ias", "ips", "upsc"})


def hint_violations(text: object, form: CandidateForm | None = None) -> list[str]:
    """Why a hint is not fit to show (empty list = fit). Pure."""
    if not isinstance(text, str):
        return ["not text"]
    hint = " ".join(text.split())
    problems: list[str] = []
    if not _HINT_OK.match(hint):
        problems.append("has a character a plain sentence does not, or is not one sentence")
    words = hint.split()
    if len(words) < MIN_HINT_WORDS:
        problems.append(f"under {MIN_HINT_WORDS} words")
    if len(words) > MAX_HINT_WORDS:
        problems.append(f"over {MAX_HINT_WORDS} words")
    low = hint.lower()
    if _FACT_WORDS.search(hint):
        problems.append("cites a source or a figure")
    if any(re.search(p, low) for p in PLACE_FACT_PATTERNS):
        problems.append("states a fact about a place")
    if _INSTRUCTION.search(hint):
        problems.append("reads like an instruction")
    if form is not None and names_the_place(hint, form) is not None:
        problems.append("names the candidate's district or state")
    # The first word is capitalised anyway; any other capital must stand on the form or be one of
    # the few acronyms. `_unbacked_capitals` allows DM, SP, IAS and IPS and the form's own words.
    extra = [w for w in _unbacked_capitals(hint, form) if w.lower() not in _ACRONYMS]
    if extra:
        problems.append(f"names something the form does not hold: {extra[:3]}")
    return list(dict.fromkeys(problems))


@dataclass(frozen=True)
class LikelyQuestion:
    segment: str  # the agenda segment id
    member: str  # the member's display name
    text: str
    hint: str
    hint_source: str = "template"  # "template" or "nova"


@dataclass(frozen=True)
class HintReport:
    """What came of the model's hints, for tests and the CLI. No question text."""

    status: str  # "template": no call was made; "used": at least one hint is Nova's;
    # "failed": the call failed or gave nothing usable (every hint is a template)
    accepted: int = 0
    rejected: tuple[tuple[int, tuple[str, ...]], ...] = ()  # (question number, reasons)
    error: str = ""
    calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "accepted": self.accepted, "calls": self.calls,
            "rejected": [{"question": n, "why": list(why)} for n, why in self.rejected],
            "error": self.error,
        }


@dataclass(frozen=True)
class LikelyQuestions:
    questions: tuple[LikelyQuestion, ...]
    source: str  # "template" or "nova": where the hints came from
    report: HintReport = field(default_factory=lambda: HintReport("template"))
    note: str = NOTE

    def as_json(self) -> dict[str, Any]:
        """What `POST /api/likely-questions` answers: the shape `docs/tasks/WEB_REQUESTS.md` asks
        for (`member`, `text`) with `segment`, `hint` and where the hint came from added."""
        return {
            "questions": [
                {"member": q.member, "text": q.text, "segment": q.segment, "hint": q.hint,
                 "hint_source": q.hint_source}
                for q in self.questions
            ],
            "source": self.source,
            "note": self.note,
        }


def template_questions(
    form: CandidateForm, names: Mapping[str, str], agenda: Agenda | None = None
) -> tuple[LikelyQuestion, ...]:
    """The questions with template hints, from the form alone. No model call, no I/O. `names` maps
    a persona id to the display name; an unknown id is shown as is."""
    agenda = agenda or build_fallback_agenda(form, DEFAULT_SEGMENT_BUDGETS)
    work_seeds = set(_WORK_SEEDS.values())
    found: list[LikelyQuestion] = []
    for segment in agenda.segments:
        hints = TEMPLATE_HINTS[segment.id]
        position = 0
        for seed in segment.seeds:
            if segment.id == FORM_HR and seed.text in work_seeds:
                hint = hints[2]  # the board's own work-experience question has its own hint
            else:
                hint = hints[position % (2 if segment.id == FORM_HR else len(hints))]
                position += 1
            found.append(
                LikelyQuestion(
                    segment.id, names.get(segment.member, segment.member), seed.text, hint
                )
            )
    return tuple(found)


def build_hints_user(form: CandidateForm, questions: tuple[LikelyQuestion, ...]) -> str:
    """The hint writer's user message. The form and the questions go in through `tag()` only: the
    form is the candidate's own text, and the questions carry words from it."""
    rows = [{"id": n + 1, "part": q.segment, "question": q.text} for n, q in enumerate(questions)]
    return "\n".join(
        [
            tag("application_form", form.as_prompt_lines()),
            tag("questions", json.dumps(rows, ensure_ascii=False)),
            "Call the record_hints tool now, with one hint for each question, in order.",
        ]
    )


def kit_with_summary(kit: PromptKit, version: int) -> PromptKit:
    """The mode's prompt kit with the `summary` prompt added to its versions. A session's trace map
    does not list `summary` (no session uses it), so the kit is widened here."""
    return replace(kit, versions={**kit.versions, "summary": version})


async def likely_questions(
    form: CandidateForm,
    names: Mapping[str, str],
    *,
    llm: LLMClient | None = None,
    kit: PromptKit | None = None,
    timeout_s: float = 20.0,
) -> LikelyQuestions:
    """Likely questions with hints. Without `llm` (or `kit`) it is `template_questions`, with no
    call. With them, one `summary` call writes the hints and each is validated alone. Never raises
    (a cancelled call does): a failed, late or empty reply leaves every hint a template."""
    base = template_questions(form, names)
    if llm is None or kit is None:
        return LikelyQuestions(base, "template")
    calls = 1
    try:
        out = await asyncio.wait_for(
            llm.structured(
                name="summary",
                system=kit.system("summary"),
                user=build_hints_user(form, base),
                tool=HINTS_TOOL,
                output_model=HintsOutput,
                tag="likely",
                max_tokens=MAX_HINT_TOKENS,
            ),
            timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except (LLMError, ValueError, TypeError, TimeoutError) as exc:
        report = HintReport("failed", error=f"{type(exc).__name__}: {exc}"[:300], calls=calls)
        return LikelyQuestions(base, "template", report)
    chosen: list[LikelyQuestion] = []
    rejected: list[tuple[int, tuple[str, ...]]] = []
    accepted = 0
    for n, question in enumerate(base):
        proposed = " ".join(out.hints[n].split()) if n < len(out.hints) else ""
        problems = hint_violations(proposed, form) if proposed else ["missing"]
        if problems:
            rejected.append((n + 1, tuple(problems)))
            chosen.append(question)
            continue
        accepted += 1
        chosen.append(replace(question, hint=proposed, hint_source="nova"))
    if not accepted:
        return LikelyQuestions(
            base, "template",
            HintReport("failed", 0, tuple(rejected), "no hint was fit to show", calls),
        )
    return LikelyQuestions(
        tuple(chosen), "nova", HintReport("used", accepted, tuple(rejected), "", calls)
    )


def format_likely_questions(result: LikelyQuestions) -> str:
    """The list as plain text, grouped by member in panel order, for the CLI."""
    lines = ["", "=" * 70, "LIKELY QUESTIONS (from your application form)", "=" * 70]
    current = None
    for n, q in enumerate(result.questions, 1):
        if q.segment != current:
            current = q.segment
            lines.append(f"\n{q.member}")
        lines.append(f"  {n}. {q.text}")
        lines.append(f"     How to approach it: {q.hint}")
    where = (
        "written by the model, each checked by code, with template hints where a check failed"
        if result.source == "nova" else "template hints (no model was called)"
    )
    lines += ["", f"Hints: {where}.", result.note]
    return "\n".join(lines)
