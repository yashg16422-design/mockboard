"""Nova writes the panel's questions, and code decides which of them are fit to ask (P6).

The deterministic agenda (`build_fallback_agenda`) always exists and the interview can start on it
whatever happens to Nova (CTO_NOTES_06 §2, CTO_NOTES_12 §7, §8). `plan_agenda` only ever upgrades
seed TEXT: segment ids, members, budgets, the `must_ask` situation and the aliases are never the
model's to change, so a plan cannot move a cap.

    START -> plan_agenda -> validate_agenda -> END        (a separate, stateless graph)

What Nova may and may not write, from CTO_NOTES_12 §8:
- No `current_affairs` seeds. The candidate chooses that issue; no fact source exists and Nova's
  knowledge may be stale. The tool has no field for it, and the segment keeps its templates.
- No plan for a Hindi interview: Devanagari has no capitals to check, so the templates stand.
- Every seed is one sentence ending in one question mark, 6 to 28 words, letters of Latin script
  only (no digit, quote, bracket, colon, full stop or markup), open-ended, free of the place-fact
  patterns and of quiz numbers, clear of every refused topic, and with no capitalised word that the
  form does not hold or that is not a plain question opener. That includes the first word. A name
  with a title ("Shri Kumar", "Doctor Rao") is refused too.
- A seed that fails any check is replaced ALONE by its own template. A plan with no usable seed is
  `template`; one with some is `nova`.

# UNVERIFIED: how fast Nova answers, and how often its seeds pass. Both come from an approved real
# run. The checks are conservative but they are not proof: a lower-case name can still pass a regex,
# and so can a fact presupposed about something other than the district or state. A seed that names
# the district or the state is refused outright (Ruling 76 item 1), because that is where a
# presupposed place fact ("Kanpur ke chamda udyog ne yahan ki zindagi kaise badli?") would sit.
# `check_place_facts` in the actor gate and the prompt are the remaining nets. If real output does
# not hold up, cut P6 (it is on the cut list); the template agenda is the product.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from mockboard.brain.agenda import (
    _WORK_SEEDS,
    MAX_SEED_WORDS,
    SEGMENT_MEMBERS,
    seed_violations,
)
from mockboard.brain.avoid_terms import find_avoided
from mockboard.brain.llm import LLMClient, LLMError
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import Agenda, AgendaSeed, AgendaSegment, CandidateForm
from mockboard.brain.tools import AGENDA_TOOL, PLANNED_SEGMENTS, AgendaOutput

__all__ = [
    "MIN_PLANNED_WORDS",
    "PlanReport",
    "build_plan_graph",
    "build_plan_user",
    "names_the_place",
    "place_terms",
    "plan_agenda",
    "planned_seed_violations",
    "replaceable_positions",
    "upgrade_agenda",
    "validate_plan",
]

MIN_PLANNED_WORDS = 6

# Letters of Latin script, spaces, comma, apostrophe, hyphen, and one final question mark. The
# absence of everything else is the rule: digits, quotes, brackets, colons, full stops, "!", angle
# brackets, braces, backticks, newlines and every non-Latin script.
# A seed that states something before it asks ("Electronics is the oldest subject here, so why
# did you choose it?"): a copula in the part before the last comma, and no question word there.
_COPULA = re.compile(r"\b(?:is|are|was|were|hai|hain|tha|thi)\b", re.IGNORECASE)
_QUESTION_WORD = re.compile(
    r"\b(?:why|how|what|which|who|when|where|kya|kyun|kyon|kaise|kaisa|kaisi|kaun|kaunsa|kaunsi|"
    r"kis|kisi|kab|kahan)\b",
    re.IGNORECASE,
)
_ALLOWED = re.compile(r"^[A-Za-z][A-Za-z ,'\-]*\?$")
_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z'-]*")
_ACRONYM_OK = frozenset({"dm", "sp", "ias", "ips"})
# A capitalised word that is not in the form is fine only if it is one of these: the openers of the
# plain questions a Hinglish or English panel asks. Lower-case, so the comparison ignores case.
_OPENERS = frozenset(
    {
        # Hinglish: the first words of the plain questions a panel asks.
        "aap", "aapko", "aapke", "aapki", "aapka", "aapne", "agar", "apne", "apni", "apna", "is",
        "iss", "isse", "iske", "iski", "yeh", "ye", "woh", "kya", "kyun", "kaise", "kaun", "kaunsi",
        "kaunsa", "kis", "kisi", "kab", "kahan", "jab", "jo", "maan", "padhai", "prashasan",
        "samaj", "sarkari", "pehle", "phir", "ek", "do", "kuch", "sabse", "dono", "zindagi",
        "hamare", "humare", "unke", "unka", "unki", "log", "logon", "ilaake", "ilake", "shehar",
        "gaon", "vichar", "soch", "baat", "kaam", "anubhav", "samajh", "badlav",
        # English
        "what", "how", "why", "which", "when", "where", "who", "if", "in", "on", "as", "given",
        "suppose", "imagine", "looking", "living", "studying", "having", "thinking", "from", "for",
        "with", "between", "among", "after", "before", "does", "would", "could", "should",
        "can", "your", "you", "i", "a", "an", "the", "one", "two", "are", "of", "to", "at",
        "by", "and", "or", "so", "that", "this", "these", "those", "while", "being", "growing",
        "working", "reading", "playing",
    }
)  # fmt: skip
# A title followed by a CAPITALISED word is a name ("Shri Kumar"); "sir" alone is plain address.
_TITLE_THEN_CAPITAL = re.compile(
    r"\b(?:Shri|Shrimati|Smt|Sri|Dr|Doctor|Prof|Professor|Mr|Mrs|Ms|Miss|Pandit|Sardar|Swami|Babu|"
    r"Netaji|Mahatma|Justice|Minister|President)\s+[A-Z][A-Za-z]{2,}"
)


def _allowed_capitals(form: CandidateForm) -> frozenset[str]:
    """What a capital letter may stand on: the form's own words, apart from the work-experience
    text (a seed does not use it), the openers and the few DM/SP acronyms."""
    values = [form.home_state, form.district, form.degree_subject, form.optional_subject,
              *form.hobbies]  # fmt: skip
    words = {w.lower() for v in values if v for w in re.findall(r"[A-Za-z][A-Za-z'-]*", v)}
    return frozenset(words | _OPENERS | _ACRONYM_OK)


# Words of a place that say which kind of place or which direction it is, not which place: a state
# "Uttar Pradesh" is named by the whole phrase, not by "uttar" (which is also a Hindi word), and a
# district "Kanpur Nagar" is named by "Kanpur". Lower case.
_GENERIC_PLACE_WORDS = frozenset(
    {
        "nagar", "pradesh", "uttar", "dakshin", "madhya", "purvi", "paschim", "district", "state",
        "city", "urban", "rural", "union", "territory", "north", "south", "east", "west",
    }
)  # fmt: skip
_MIN_PLACE_WORD = 5


def place_terms(form: CandidateForm) -> tuple[str, ...]:
    """What names the candidate's district or state: each whole value, and each word of it that
    is long enough and not a generic word. Ruling 76 item 1 (CTO_NOTES_16 S1): the template lines
    already carry the place, so a planned seed need not, and one that does presupposes a fact about
    it ("Kanpur ke chamda udyog ne yahan ki zindagi kaise badli?")."""
    terms: list[str] = []
    for value in (form.district, form.home_state):
        if not value:
            continue
        whole = " ".join(value.split())
        terms.append(whole)
        terms.extend(
            w for w in re.findall(r"[A-Za-z]+", whole)
            if len(w) >= _MIN_PLACE_WORD and w.lower() not in _GENERIC_PLACE_WORDS
        )
    return tuple(dict.fromkeys(terms))


def names_the_place(seed: str, form: CandidateForm) -> str | None:
    """The term of the district or state the seed names, whole-word and ignoring case, or None."""
    for term in place_terms(form):
        if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", seed, re.IGNORECASE):
            return term
    return None


def planned_seed_violations(
    text: str, segment_id: str, form: CandidateForm, avoid: Sequence[str] = ()
) -> list[str]:
    """Why Nova's seed is not fit to ask (empty list = fit). Stricter than `seed_violations`,
    which the templates also pass. Pure."""
    if not isinstance(text, str):
        return ["not text"]
    seed = text.strip()
    problems: list[str] = []
    if names_the_place(seed, form) is not None:
        problems.append("names the candidate's district or state")
    if not _ALLOWED.match(seed):
        problems.append("has a character a plain question does not (digit, quote, mark-up, stop)")
    if seed.count("?") != 1:
        problems.append("is not one question")
    words = seed.split()
    if len(words) < MIN_PLANNED_WORDS:
        problems.append(f"under {MIN_PLANNED_WORDS} words")
    if len(words) > MAX_SEED_WORDS:
        problems.append(f"over {MAX_SEED_WORDS} words")
    problems += seed_violations(seed, segment_id, form)
    head = seed.rpartition(",")[0]
    if head and _COPULA.search(head) and not _QUESTION_WORD.search(head):
        problems.append("states something before it asks")
    allowed = _allowed_capitals(form)
    names = [w for w in _CAPITALISED.findall(seed) if w.lower() not in allowed]
    if names:
        problems.append(f"names something the form does not hold: {names[:3]}")
    if _TITLE_THEN_CAPITAL.search(seed):
        problems.append("a title and a name")
    if avoid and find_avoided(seed, avoid) is not None:
        problems.append("says a topic the candidate refused")
    return list(dict.fromkeys(problems))


@dataclass(frozen=True)
class PlanReport:
    """What came of a plan, for the trace and the tests. No question text, only counts and why."""

    status: str  # "used": some seed was Nova's; "template": none was; "failed": no usable reply;
    # "skipped": no call was made
    accepted: int = 0
    rejected: tuple[tuple[str, int, tuple[str, ...]], ...] = ()  # (segment, index, reasons)
    error: str = ""
    ms: float = 0.0

    def as_trace(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "rejected": [
                {"segment": s, "seed": i, "why": list(why)} for s, i, why in self.rejected
            ],
            "error": self.error,
            "ms": self.ms,
        }


def replaceable_positions(segment: AgendaSegment, form: CandidateForm) -> list[int]:
    """The positions of a segment's seeds that Nova may rewrite: every one but the situation and
    the board's own work-experience question. Nova's k-th question goes to the k-th of these."""
    return [
        i for i, seed in enumerate(segment.seeds)
        if not seed.must_ask and not _is_work_seed(seed, form)
    ]


def validate_plan(
    out: AgendaOutput,
    fallback: Agenda,
    form: CandidateForm,
    avoid: Sequence[str] = (),
) -> tuple[Agenda, PlanReport]:
    """Merge Nova's seeds into the fallback agenda, one seed at a time. Never raises.

    Position i of a segment takes Nova's i-th question if it passes every check and is not a
    repeat of anything already in the agenda; otherwise the template's i-th stays. Only
    `PLANNED_SEGMENTS` are read; the work-experience seed of `form_hr` is the board's own and is
    never replaced."""
    taken = {seed.text for segment in fallback.segments for seed in segment.seeds}
    segments: list[AgendaSegment] = []
    accepted = 0
    rejected: list[tuple[str, int, tuple[str, ...]]] = []
    for segment in fallback.segments:
        if segment.id not in PLANNED_SEGMENTS:
            segments.append(segment)
            continue
        proposed = out.seeds_for(segment.id)
        seeds = list(segment.seeds)
        for k, i in enumerate(replaceable_positions(segment, form)):
            if k >= len(proposed):
                break
            template = seeds[i]
            text = " ".join(proposed[k].split())
            problems = planned_seed_violations(text, segment.id, form, avoid)
            if not problems and text != template.text and text in taken:
                problems = ["repeats another question"]
            if problems:
                rejected.append((segment.id, i, tuple(problems)))
                continue
            taken.discard(template.text)
            taken.add(text)
            seeds[i] = AgendaSeed(text=text)
            accepted += int(text != template.text)
        segments.append(segment.model_copy(update={"seeds": seeds}))
    agenda = Agenda(segments=segments, source="nova" if accepted else "template")
    status = "used" if accepted else "template"
    return agenda, PlanReport(status, accepted, tuple(rejected))


def _is_work_seed(seed: AgendaSeed, form: CandidateForm) -> bool:
    return bool(form.work_experience) and seed.text in _WORK_SEEDS.values()


def upgrade_agenda(current: Agenda, planned: Agenda, started: int) -> Agenda:
    """`current` with the questions of every segment after index `started` taken from `planned`
    (a late plan upgrades only segments not begun, CTO_NOTES_06 §2). Same ids, members, budgets,
    aliases and must-ask seeds: only seed text moves, and only when the counts agree."""
    segments = []
    changed = False
    for index, segment in enumerate(current.segments):
        later = index > started
        match = next((p for p in planned.segments if p.id == segment.id), None)
        if later and match is not None and len(match.seeds) == len(segment.seeds):
            changed = changed or match.seeds != segment.seeds
            segments.append(segment.model_copy(update={"seeds": list(match.seeds)}))
        else:
            segments.append(segment)
    return Agenda(segments=segments, source="nova" if changed else current.source)


# --- The graph ----------------------------------------------------------------------------------

SEGMENT_FIELDS = {
    "form_hr": "the form and the candidate's motivation (the Chairperson)",
    "home_district": "home state and district (a member who knows administration on the ground)",
    "degree": "the degree subject (an academic member)",
    "interests": "hobbies and the optional subject (a member from the humanities)",
}


def build_plan_user(form: CandidateForm, fallback: Agenda, briefing: str = "") -> str:
    """The planner's user message. The form goes in through `tag()` only: it is the candidate's own
    text and the main way into this prompt (CTO_NOTES_12 §7). `briefing` is the same: recent public
    lines, present only on a real sitting, and never an order."""
    segments = [
        {
            "id": s.id,
            "field": SEGMENT_FIELDS[s.id],
            "questions_needed": len(replaceable_positions(s, form)),
            "template_questions": [s.seeds[i].text for i in replaceable_positions(s, form)],
        }
        for s in fallback.segments
        if s.id in PLANNED_SEGMENTS
    ]
    parts = [
        tag("application_form", form.as_prompt_lines()),
        tag("segments", json.dumps(segments, ensure_ascii=False)),
    ]
    if briefing.strip():
        parts.append(tag("briefing", briefing.strip()))
    parts.append("Call the record_agenda tool now.")
    return "\n".join(parts)


class PlanState(TypedDict, total=False):
    form: CandidateForm
    fallback: Agenda
    avoid: tuple[str, ...]
    briefing: str
    llm: LLMClient
    kit: PromptKit
    started: float
    output: AgendaOutput | None
    error: str
    agenda: Agenda
    report: PlanReport


async def plan_node(state: PlanState) -> dict[str, Any]:
    """One forced-tool call on the background lane (`plan_agenda` is not in `LANE_BY_CALL`). A
    failure is an answer: the next node falls back to the templates."""
    try:
        out = await state["llm"].structured(
            name="plan_agenda",
            system=state["kit"].system("agenda_plan"),
            user=build_plan_user(state["form"], state["fallback"], state.get("briefing", "")),
            tool=AGENDA_TOOL,
            output_model=AgendaOutput,
            tag="plan",
        )
    except asyncio.CancelledError:
        raise
    except (LLMError, ValueError, TypeError) as exc:  # a validation error is a ValueError
        return {"output": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"output": out, "error": ""}


async def validate_node(state: PlanState) -> dict[str, Any]:
    started = state.get("started", time.monotonic())
    ms = round((time.monotonic() - started) * 1000, 1)
    out = state.get("output")
    if out is None:
        report = PlanReport("failed", error=state.get("error", "no plan"), ms=ms)
        return {"agenda": state["fallback"], "report": report}
    try:
        agenda, report = validate_plan(out, state["fallback"], state["form"], state["avoid"])
    except Exception as exc:  # validation is pure, but a bug here must not stop an interview
        report = PlanReport("failed", error=f"validate: {type(exc).__name__}: {exc}"[:300], ms=ms)
        return {"agenda": state["fallback"], "report": report}
    done = PlanReport(report.status, report.accepted, report.rejected, "", ms)
    return {"agenda": agenda, "report": done}


def build_plan_graph() -> StateGraph:
    graph = StateGraph(PlanState)
    graph.add_node("plan_agenda", plan_node)
    graph.add_node("validate_agenda", validate_node)
    graph.add_edge(START, "plan_agenda")
    graph.add_edge("plan_agenda", "validate_agenda")
    graph.add_edge("validate_agenda", END)
    return graph


_PLAN_GRAPH: Any = None


def _compiled() -> Any:
    """Stateless: no checkpointer, so nothing outlives a call and nothing is shared between
    sessions but the compiled graph itself."""
    global _PLAN_GRAPH
    if _PLAN_GRAPH is None:
        _PLAN_GRAPH = build_plan_graph().compile(name="plan_agenda")
    return _PLAN_GRAPH


async def plan_agenda(
    llm: LLMClient,
    kit: PromptKit,
    form: CandidateForm,
    fallback: Agenda,
    *,
    avoid: Sequence[str] = (),
    briefing: str = "",
) -> tuple[Agenda, PlanReport]:
    """The planned agenda and what came of it. Never raises (a cancelled call does): any failure
    returns `fallback` itself. A Hindi interview is not planned: no call is made. `briefing` is
    attached only when a real sitting fetched usable headlines."""
    if form.interview_language == "hindi":
        return fallback, PlanReport("skipped", error="hindi interviews keep the template questions")
    started = time.monotonic()
    try:
        state = await _compiled().ainvoke(
            {
                "form": form, "fallback": fallback, "avoid": tuple(avoid), "briefing": briefing,
                "llm": llm, "kit": kit, "started": started,
            }
        )
        return state["agenda"], state["report"]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # the graph itself broke: still an answer
        ms = round((time.monotonic() - started) * 1000, 1)
        return fallback, PlanReport("failed", error=f"{type(exc).__name__}: {exc}"[:300], ms=ms)


assert set(PLANNED_SEGMENTS) <= set(SEGMENT_MEMBERS)  # the tool and the agenda name the same parts
