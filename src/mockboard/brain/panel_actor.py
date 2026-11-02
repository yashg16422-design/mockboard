"""The panel's spoken lines (CTO_NOTES_06 §5, CTO_NOTES_12 §7, CTO_NOTES_15 §9).

A panel turn is up to three kinds of text: fixed lines said by code (an acknowledgement, a
hand-off, a repeat, a close), and one model line from `actor.v4`. `compose_panel_line` builds the
whole turn and keeps who said each part in `PanelLine.parts`, so the trace and the voice loop (P8)
can attribute it. The UPSC and debate actors are untouched; this module only reuses their
primitives.

Everything the model reads that is not the fixed system prompt goes in through `tag()`, and
`actor.v4` says that form words, summaries and avoid terms are data. Code then checks the line:
`PanelGate` stops it at a sentence that quizzes a number, quotes the candidate, states a place fact,
holds a digit or names a refused topic, and a fixed question finishes it. A failed or refused line
falls back to the seed, the last question or the persona's own line, never to silence.

# UNVERIFIED: actor.v4 has never run on Nova. The gate's place-fact patterns are conservative, and a
# plain invented sentence ("Kanpur Ganga ke kinare hai") passes them. The prompt forbids it, but
# the guard cannot prove it: that is a limit, not a guarantee.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from mockboard.brain.actor import (
    _SENTENCE_BREAK,  # the sentence gate's own break: held text is released where it would pass
    FILLER_TEMPLATES,
    PLACE_FACT_PATTERNS,
    ActorBudgetExceeded,
    LineResult,
    SentenceGate,
    actor_fallback,
    call_actor,
    cap_words,
    challenge_facts,
    collect_line,
    guard_fact_challenge,
    language_mix,
    opener_violations,
    preface_text,
    stream_line,
)
from mockboard.brain.affairs import AffairsNote, affairs_line
from mockboard.brain.agenda import NEUTRAL_SEEDS
from mockboard.brain.avoid_terms import find_avoided, prompt_terms
from mockboard.brain.cards import CardSet
from mockboard.brain.llm import LLMClient, LLMError
from mockboard.brain.panel_lines import CLARIFY_PANEL_LINES, line_for
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    BoardMove,
    CandidateForm,
    Claim,
    FactCard,
    Move,
    PanelContext,
    PanelMemory,
    Persona,
    PrefacePart,
    TurnNote,
)

# A line that asks the agenda seed has to hold the seed's own words (up to 28), so it gets room.
SEED_LINE_WORDS = 40
SUMMARY_WORDS = 120
RECENT_EXCHANGES = 3
_DIGIT = re.compile(r"\d")
# The place-fact patterns that are superlatives ("is the largest", "sabse bada", "has the highest").
# In a question they are the commonest open-ended wording ("sabse badi chunauti kya hai?"), so a
# question is checked only against the rest; a statement is checked against all of them.
_SUPERLATIVES = frozenset(PLACE_FACT_PATTERNS[2:5])
# A title with a full stop splits a streamed line into fragments ("Dr." / "Iyer ke baad..."), and
# the voice loop would say each fragment as its own request. Panel names have none, so a title
# from the model loses its dot here, before any splitter sees it.
_TITLE_DOT = re.compile(r"\b(Dr|Mr|Mrs|Ms|Smt|Shri|Sri|Prof|Er)\.(?=\s|$)")


@dataclass(frozen=True)
class PanelLine(LineResult):
    """A panel turn's text with who said each part. `text` is the parts joined by spaces."""

    parts: tuple[PrefacePart, ...] = ()


def joined(parts: Sequence[PrefacePart]) -> str:
    return " ".join(p.text for p in parts if p.text)


def undotted_titles(text: str) -> str:
    return _TITLE_DOT.sub(r"\1", text)


async def strip_title_dots(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Remove the full stop after a title in a streamed line. A chunk may end inside a word, so
    the last word is held until the next chunk shows it is whole."""
    held = ""
    async for chunk in chunks:
        held += chunk
        cut = max(held.rfind(" "), held.rfind("\n"))
        if cut >= 0:
            yield undotted_titles(held[: cut + 1])
            held = held[cut + 1 :]
    if held:
        yield undotted_titles(held)


# A greeting in a model line (Ruling 73 item 4): only the first hand-off carries one, and that is
# the prompt's rule (actor.v5). This is the code that backs it, since a model can slip.
_GREETING = re.compile(
    r"(?:\bNamaste\b|\bNamaskar\b|\bGood (?:morning|afternoon|evening|day)\b|नमस्ते|नमस्कार)"
    r"[\s,.!।]*",
    re.IGNORECASE,
)
GREETING_WINDOW_WORDS = 8


def _strip_window(text: str, window: int) -> str:
    """The greeting removed from the first `window` words of `text` (none left: untouched)."""
    if window <= 0:
        return text
    words = text.split(" ")
    head = " ".join(words[:window])
    cleaned = _GREETING.sub("", head).strip()
    tail = " ".join(words[window:])
    return (f"{cleaned} {tail}" if cleaned and tail else cleaned or tail).strip()


def without_greeting(text: str) -> str:
    """Remove a greeting from the start of a line ("Shukriya. Namaste. Aapko ..." becomes
    "Shukriya. Aapko ..."). Only the first words are read: a line is a thanks, then a question, so a
    greeting later in it would be part of what the member is asking about."""
    return _strip_window(text, GREETING_WINDOW_WORDS)


async def strip_greeting(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """`without_greeting` on a stream, one whole sentence at a time. A sentence inside the first
    `GREETING_WINDOW_WORDS` words is held only until it is complete, which is also when the sentence
    gate downstream could pass it, so the check adds no wait to the first audio of an ordinary line
    (holding the whole window would have: about eight words of model text before anything is
    said). A greeting that makes a sentence empty ("Namaste.") drops it. Past the window the stream
    passes through untouched. The text joined is what `without_greeting` gives."""
    held = ""
    used = 0  # words of the window already read
    async for chunk in chunks:
        if used >= GREETING_WINDOW_WORDS:
            yield chunk
            continue
        held += chunk
        while used < GREETING_WINDOW_WORDS and (m := _SENTENCE_BREAK.search(held)) is not None:
            sentence, held = held[: m.end()], held[m.end() :]
            room = GREETING_WINDOW_WORDS - used
            used += len(sentence.split())
            clean = _strip_window(sentence.strip(), room)
            if clean:
                yield clean + " "
        if used >= GREETING_WINDOW_WORDS and held:
            yield held
            held = ""
    if held:
        tail = _strip_window(held.strip(), GREETING_WINDOW_WORDS - used)
        if tail:
            yield tail


class _TitleDotLLM:
    """The model client with `stream_text` passed through `strip_title_dots`, and through
    `strip_greeting` unless the line may greet. Anything else is the client's own, so `offline`
    and the fake's call log still read through."""

    def __init__(self, llm: LLMClient, *, greeting_allowed: bool = True) -> None:
        self._llm = llm
        self._greeting_allowed = greeting_allowed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def stream_text(self, **kwargs: Any) -> AsyncIterator[str]:
        stream = strip_title_dots(self._llm.stream_text(**kwargs))
        return stream if self._greeting_allowed else strip_greeting(stream)


@dataclass
class PanelGate(SentenceGate):
    """`SentenceGate` plus the panel's own checks (CTO_NOTES_06 §4): no place fact, no digit, no
    refused topic. `SentenceGate` alone checks neither outside the opener."""

    avoid: tuple[str, ...] = ()
    check_place_facts: bool = True

    def ok(self, sentence: str) -> bool:
        if not super().ok(sentence):
            return False
        low = sentence.lower()
        question = sentence.rstrip().endswith("?")
        patterns = [p for p in PLACE_FACT_PATTERNS if not (question and p in _SUPERLATIVES)]
        if self.check_place_facts and (
            _DIGIT.search(sentence) or any(re.search(p, low) for p in patterns)
        ):
            self.stopped, self.reason = sentence, "place_fact"
        elif find_avoided(sentence, self.avoid) is not None:
            self.stopped, self.reason = sentence, "avoid_topic"
        return self.stopped is None


# --- What the model reads ------------------------------------------------------------------------


def render_summary(notes: Sequence[TurnNote], max_words: int = SUMMARY_WORDS) -> str:
    """The gists oldest to newest, over the word cap dropping the oldest first but keeping one
    note per finished segment (CTO_NOTES_06 §5). Empty gists are left out: a fallback analysis
    never contributes raw text."""
    kept = [n for n in notes if n.gist.strip()]
    lines = [f"- {n.member}: {n.gist.strip()}" for n in kept]

    def words() -> int:
        return sum(len(line.split()) for line in lines)

    index = 0
    while words() > max_words and index < len(kept):
        # Drop this note unless it is the only one of its segment.
        if sum(1 for n in kept if n.segment_id == kept[index].segment_id) > 1:
            del lines[index], kept[index]
        else:
            index += 1
    return "\n".join(lines)


def render_recent(notes: Sequence[TurnNote], n: int = RECENT_EXCHANGES) -> str:
    """The last `n` exchanges: the question put and the gist of the answer."""
    rows = []
    for note in list(notes)[-n:]:
        gist = note.gist.strip() or "(answer not summarised)"
        rows.append(f"- Question: {note.question.strip()}\n  Answer in short: {gist}")
    return "\n".join(rows)


def board_context(
    persona: Persona,
    move: BoardMove,
    *,
    form: CandidateForm,
    spoken: Sequence[str],
    after_handoff: bool,
    first_handoff: bool = False,
) -> str:
    actor_move = Move.next_question if move.move == Move.move_on else move.move
    ctx = {
        "speaker": f"{persona.name}, {persona.title}",
        "speaker_style": persona.style,
        "speaker_domain": persona.domain,
        "manner": persona.manner,
        "move": actor_move.value,
        "move_reason": move.reason,
        "interview_language": form.interview_language,
        "after_handoff": after_handoff,
        # Only the first hand-off of the session may carry a "Namaste" (actor.v5, Ruling 73 item 4).
        "first_handoff": first_handoff,
        # Already said by code before this line; the model must not repeat it.
        "preface_already_spoken": list(spoken),
    }
    return tag("board_context", json.dumps(ctx, ensure_ascii=False, indent=1))


def build_panel_user(
    persona: Persona,
    move: BoardMove,
    panel: PanelContext,
    memory: PanelMemory,
    *,
    form: CandidateForm,
    spoken: Sequence[str],
    after_handoff: bool,
    last_question: str,
    last_answer: str,
    avoid: Sequence[str] = (),
    claim: Claim | None = None,
    card: FactCard | None = None,
    mix: str = "hi-en",
    briefing: tuple[AffairsNote, ...] | None = None,
) -> str:
    """The panel actor's user message. Every block is built with `tag()`, never an f-string: form
    text, gists and avoid terms are untrusted (CTO_NOTES_12 §7)."""
    item = {"segment": panel.segment_title, "member_field": persona.domain, "seed": panel.seed}
    parts = [
        board_context(
            persona, move, form=form, spoken=spoken, after_handoff=after_handoff,
            first_handoff=panel.first_handoff,
        ),
        tag("agenda_item", json.dumps(item, ensure_ascii=False)),
    ]
    # Every member has read the same briefing. It is background, not the question.
    # `briefing` is this sitting's search notes; None keeps the file.
    line = affairs_line(briefing)
    if line:
        parts.append(tag("affairs", line))
    summary = render_summary(memory.notes)
    if summary:
        parts.append(tag("conversation_summary", summary))
    recent = render_recent(memory.notes)
    if recent:
        parts.append(tag("recent_exchanges", recent))
    shown = prompt_terms(avoid)
    if shown:
        parts.append(tag("avoid_topics", ", ".join(shown)))
    parts += [tag("last_question", last_question), tag("candidate_speech", last_answer)]
    if move.move == Move.fact_challenge and claim and card:
        parts.append(tag("claim_to_revisit", claim.text))
        parts.append(tag("fact_challenge", challenge_facts(claim, card, mix).as_prompt_json()))
    parts.append("Say the line now.")
    return "\n".join(parts)


# --- The line ------------------------------------------------------------------------------------


_SENTENCE_SPLIT = re.compile(r"(?<=[.?!।])\s+")
_ACK_MAX_WORDS = 4


def question_part(text: str) -> str:
    """The question inside a model line, without a leading acknowledgement ("Achha.", "Shukriya.
    Namaste."), so a repeat says the question and not the pleasantry."""
    sentences = [x for x in _SENTENCE_SPLIT.split(text.strip()) if x]
    while len(sentences) > 1 and not sentences[0].endswith("?") and (
        len(sentences[0].split()) <= _ACK_MAX_WORDS
    ):
        sentences.pop(0)
    return " ".join(sentences)


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def fixed_parts(
    preface: Sequence[PrefacePart], move: BoardMove, mix: str, ack_member: str
) -> list[PrefacePart]:
    """What code says before the model's part: a conceded error's acknowledgement, said by the
    member who heard it, then the director's lines. Deterministic, so the voice loop can read it
    before the model has answered."""
    parts = list(preface)
    ack = preface_text(move, mix)
    if ack:
        parts.insert(0, PrefacePart(member=ack_member, text=ack))
    return parts


def fixed_line(parts: Sequence[PrefacePart], *, guard: str = "fixed_line") -> PanelLine:
    return PanelLine(joined(parts), guard=guard, parts=tuple(parts))


async def compose_panel_line(
    llm: LLMClient,
    kit: PromptKit,
    persona: Persona,
    move: BoardMove,
    panel: PanelContext,
    memory: PanelMemory,
    *,
    form: CandidateForm,
    preface: Sequence[PrefacePart],
    ack_member: str,
    repeat_text: str | None,
    line_complete: bool,
    after_handoff: bool,
    last_question: str,
    last_answer: str,
    avoid: Sequence[str],
    max_words: int,
    max_tokens: int,
    turn_id: str,
    claim: Claim | None = None,
    card: FactCard | None = None,
    on_first_token: Callable[[], None] | None = None,
    first_token_budget_s: float | None = None,
    filler_played: Callable[[], bool] | None = None,
    fast_throttle_retry_window_s: float | None = None,
    briefing: tuple[AffairsNote, ...] | None = None,
) -> PanelLine:
    """One panel turn. `preface` is the director's fixed lines; a conceded error adds the
    acknowledgement first, by `ack_member`, as in the UPSC board."""
    mix = language_mix(persona, form)
    fixed = fixed_parts(preface, move, mix, ack_member)

    if line_complete:  # a repeat, a close or the end confirmation: no model call, whatever the move
        if repeat_text:
            fixed.append(PrefacePart(member=move.member, text=repeat_text))
        return fixed_line(fixed)
    if move.move == Move.clarify:  # Ruling 37: ask generically, never echo the transcript
        uses = memory.line_uses.get(CLARIFY_PANEL_LINES.id, 0)
        clarify = line_for(CLARIFY_PANEL_LINES, mix, uses)
        fixed.append(PrefacePart(member=move.member, text=clarify))
        return fixed_line(fixed, guard="clarify_template")
    if move.move == Move.fact_challenge and (claim is None or card is None):
        fixed.append(PrefacePart(member=move.member, text=FILLER_TEMPLATES[Move.probe]))
        return fixed_line(fixed, guard="no_card")

    seeded = move.move in (Move.next_question, Move.move_on) and bool(panel.seed)
    is_fact = move.move == Move.fact_challenge
    gate = None if is_fact else PanelGate(answer=last_answer, avoid=tuple(avoid))
    user = build_panel_user(
        persona, move, panel, memory, form=form, spoken=[p.text for p in fixed],
        after_handoff=after_handoff, last_question=last_question, last_answer=last_answer,
        avoid=avoid, claim=claim, card=card, mix=mix, briefing=briefing,
    )
    cap = max(max_words, SEED_LINE_WORDS) if seeded else max_words
    call = await call_actor(
        _TitleDotLLM(llm, greeting_allowed=panel.first_handoff), kit, user, max_words=cap,
        max_tokens=max_tokens, turn_id=turn_id,
        on_first_token=on_first_token, first_token_budget_s=first_token_budget_s,
        fast_throttle_retry_window_s=fast_throttle_retry_window_s, gate=gate,
    )

    def say(text: str, **fields: Any) -> PanelLine:
        parts = (*fixed, PrefacePart(member=move.member, text=text))
        return PanelLine(joined(parts), parts=tuple(parts), **fields)

    def fallback(error: str, guard: str, retried: bool) -> PanelLine:
        if seeded:  # the verbatim seed, behind the same gate (CTO_NOTES_12 §7.5)
            seed = panel.seed or ""
            if not PanelGate(answer="", avoid=tuple(avoid)).ok(seed):
                seed = NEUTRAL_SEEDS.get(mix, NEUTRAL_SEEDS["hi-en"])
            return say(seed, is_fallback=True, guard=guard, error=error,
                       fallback_kind="seed_template", retried=retried)
        if move.move == Move.rephrase_question and last_question:
            return say(last_question, is_fallback=True, guard=guard, error=error,
                       fallback_kind="repeat_verbatim", retried=retried)
        played = bool(filler_played()) if filler_played is not None else False
        line = actor_fallback(move, persona, turn_id, error=error, guard=guard, claim=claim,
                              card=card, mix=mix, filler_played=played)
        return say(line.text, is_fallback=True, guard=guard, error=error,
                   fallback_kind=line.fallback_kind, retried=retried)

    if call.error:
        return fallback(call.error, call.guard, call.retried)
    text = undotted_titles(call.text)
    if not text:  # every sentence was refused by the gate
        return fallback("every sentence refused", call.guard or "gate", call.retried)
    if is_fact and claim and card:
        checked = guard_fact_challenge(text, claim, card, mix)
        return say(checked.text, guard=checked.guard, retried=call.retried)
    earlier = {_norm(q) for q in (last_question, *(n.question for n in memory.notes)) if q}
    if _norm(question_part(text)) in earlier or _norm(text) in earlier:  # no repeated questions
        return fallback("repeats an earlier question", "repeat_guard", call.retried)
    if gate is not None and gate.stopped is not None and not text.rstrip().endswith("?"):
        if seeded and panel.seed and find_avoided(panel.seed, avoid) is None:
            question = panel.seed
        else:
            question = FILLER_TEMPLATES.get(move.move, FILLER_TEMPLATES[Move.probe])
        return say(f"{text} {question}".strip(), is_fallback=True, guard=gate.reason,
                   fallback_kind="move_template", retried=call.retried)
    return say(text, guard=gate.reason if gate and gate.stopped else "", retried=call.retried)


async def generate_panel_opener(
    llm: LLMClient,
    kit: PromptKit,
    form: CandidateForm,
    cards: CardSet,
    seed: str,
    *,
    max_words: int,
    max_tokens: int,
    turn_id: str,
    first_token_budget_s: float | None = None,
) -> LineResult:
    """The first question, phrased by the Chairperson from the first seed (`opener.v2`, no
    greeting: the ritual line comes first). Checked by `opener_violations`, the digit rule and
    the quote-free gate; any failure says the verbatim seed."""
    allowed = [
        {"id": c.id, "statement": c.statement}
        for c in cards.cards.values()
        if not c.synthetic_example
        and any(
            place and place.lower() in (c.statement + " " + " ".join(c.aliases)).lower()
            for place in (form.home_state, form.district)
        )
    ]
    user = "\n".join(
        [
            tag("application_form", form.as_prompt_lines()),
            tag("agenda_item", json.dumps({"seed": seed}, ensure_ascii=False)),
            tag("allowed_facts", json.dumps(allowed, ensure_ascii=False)),
            "Say the opening question now.",
        ]
    )
    try:
        text = await collect_line(
            stream_line(_TitleDotLLM(llm), kit, user, max_words=max_words, max_tokens=max_tokens,
                        tag_=turn_id, prompt_id="opener", name="opener"),
            first_token_budget_s=first_token_budget_s,
        )
    except (ActorBudgetExceeded, LLMError) as exc:
        return LineResult(seed, is_fallback=True, guard="opener_failed",
                          fallback_kind="seed_template",
                          error=f"{type(exc).__name__}: {exc}"[:300])
    text = undotted_titles(cap_words(text, max_words))
    if not text:
        return LineResult(seed, is_fallback=True, guard="opener_guard",
                          fallback_kind="seed_template", error="empty line")
    if opener_violations(text, cards) or _DIGIT.search(text):
        return LineResult(seed, guard="opener_guard")
    return LineResult(text)
