"""Actor: turns the precomputed `BoardMove` into one spoken line (at most 30 words).

This is a plain async streaming call, NOT a LangGraph node: it is the only model call on the
real-time critical path. Three guards live here in code, not only in the prompt:

1. Word cap: streaming stops at `max_words` words, whatever the model does.
2. Ask, never assert (ADR-003): a fact challenge must end as a question and must not contain
   assertive "you are wrong" phrasing; otherwise a deterministic template replaces it.
3. Opener guard (ruling 5): the form-driven opener must be open-ended and must not state a
   fact about a place unless it cites an existing fact card; otherwise a template replaces it.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from mockboard.brain.cards import CardSet
from mockboard.brain.llm import LLMClient, LLMError
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    BoardMove,
    CandidateForm,
    Claim,
    FactCard,
    Move,
    Persona,
)

_WORD = re.compile(r"\S+")
_SENTENCE_END = re.compile(r"[.?!।]")
_CARD_REF = re.compile(r"\[([a-z0-9_\-]+)\]")

# Assertive phrasings that must never appear in a fact challenge (English + Hinglish).
ASSERTIVE_PATTERNS = [
    r"\byou(?:'re| are) (?:wrong|incorrect|mistaken)\b",
    r"\bthat(?:'s| is) (?:wrong|incorrect|false|not true)\b",
    r"\bgalat (?:hai|hain|bola|kaha)\b",
    r"\baap galat\b",
    r"\bsahi nahi hai\b",
    r"\bjhooth\b",
    r"\bfactually wrong\b",
]
# Phrases that state a fact about a place (opener guard). Conservative on purpose.
PLACE_FACT_PATTERNS = [
    r"\b(?:is|are|was) (?:famous|known|renowned|popular) for\b",
    r"\b(?:mashhoor|prasiddh|famous) hai\b",
    r"\b(?:is|was) the (?:largest|biggest|smallest|first|highest|lowest|most|least)\b",
    r"\bsabse (?:bada|badi|zyada|kam|pehla|pehli)\b",
    r"\bhas the (?:highest|lowest|largest|most)\b",
    r"\bhas (?:a|an) (?:literacy|population|gdp|crime|tourism)\b",
]
OPEN_ENDED_MARKERS = [
    "kyun", "kyon", "kaise", "kaisa", "kaisi", "kya lagta", "kya sochte", "kya dekha",
    "kya challenge", "kya badlav", "kis tarah", "kaunsi", "kaun si", "kaun sa", "kya raay",
    "why", "how", "what", "which", "in what way", "tell us", "batayiye", "bataiye",
    "samjhaiye", "your view", "aapki raay", "aapka anubhav",
]
CLOSED_STARTS = re.compile(
    r"^(?:do|does|did|is|are|was|were|have|has|can|will|would|should) you\b|"
    r"^kya aap(?:ne)? .*\b(?:hain|hai|tha|thi|the)\s*\?$",
    re.IGNORECASE,
)


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def cap_words(text: str, max_words: int) -> str:
    words = _WORD.findall(text)
    return text.strip() if len(words) <= max_words else " ".join(words[:max_words])


async def stream_capped(chunks: AsyncIterator[str], max_words: int) -> AsyncIterator[str]:
    """Pass text chunks through until `max_words` words have been emitted, then stop."""
    emitted = 0
    pending = ""
    async for chunk in chunks:
        pending += chunk
        # Emit only whole words (the last token may still be growing).
        parts = re.split(r"(\s+)", pending)
        complete, pending = parts[:-1], parts[-1]
        out = []
        for part in complete:
            if part.strip():
                if emitted >= max_words:
                    break
                emitted += 1
            out.append(part)
        if out:
            yield "".join(out)
        if emitted >= max_words:
            return
    if pending.strip() and emitted < max_words:
        yield pending


@dataclass(frozen=True)
class LineResult:
    text: str
    is_fallback: bool = False
    guard: str = ""  # which guard replaced the model's line, if any


# ---------------------------------------------------------------------------
# Deterministic templates (used as guards and as the offline fallback)
# ---------------------------------------------------------------------------


def source_label(card: FactCard) -> str:
    if card.synthetic_example:
        return "hamare synthetic test card"
    return card.source_title or "official data"


def claim_figure(claim: Claim) -> str:
    """A short spoken reference to the claimed figure ("30 percent", "2015"), so a challenge
    quotes the number, not the whole sentence (keeps the line near the word cap)."""
    if claim.value is None:
        return f"\"{cap_words(claim.text, 8)}\""
    number = f"{claim.value:,.0f}" if claim.value >= 1000 or claim.value.is_integer() else (
        f"{claim.value:g}")
    if claim.kind.value == "date":
        return f"saal {claim.value:.0f}"
    return f"{number} {claim.unit}".strip() if claim.unit else number


def fact_challenge_template(claim: Claim, card: FactCard) -> str:
    return (
        f"Aapne {claim_figure(claim)} kaha tha. {source_label(card)} ke anusaar: "
        f"{card.statement} Ise aap kaise reconcile karenge?"
    )


def opener_template(form: CandidateForm) -> str:
    place = form.district or form.home_state
    return (
        f"Namaste, baithiye. Aap {place} se hain aur {form.degree_subject} padha hai. "
        f"Wahan rehte hue aapne kaunsi ek samasya dekhi, aur use kaise suljhayenge?"
    )


FILLER_TEMPLATES: dict[Move, str] = {
    Move.probe: "Achha. Is par thoda aur detail mein bataiye, aisa kyun?",
    Move.counter: "Lekin doosra paksh bhi hai. Uske baare mein aap kya kahenge?",
    Move.steelman: "Theek hai. Ab doosre paksh ka sabse mazboot argument kya hoga?",
    Move.pressure: "Koi ek thos example dijiye, jisse aapki baat saaf ho.",
    Move.reassure: (
        "Koi baat nahi, aaram se sochiye. Chaliye, ek aasaan sawaal se shuru karte hain."
    ),
    Move.move_on: "Theek hai, shukriya. Ab ek naye vishay par aate hain.",
    Move.clarify: "Maaf kijiye, main theek se samajh nahi paaya. Kya aap phir se bata sakte hain?",
    Move.interrupt: "Ek minute, main beech mein rokta hoon. Seedhe mukhya baat par aaiye.",
}


def has_assertion(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in ASSERTIVE_PATTERNS)


def guard_fact_challenge(text: str, claim: Claim, card: FactCard) -> LineResult:
    if not text.strip().endswith("?") or has_assertion(text):
        return LineResult(fact_challenge_template(claim, card), guard="ask_never_assert")
    return LineResult(text)


def opener_violations(text: str, cards: CardSet) -> list[str]:
    """Why an opener line is not acceptable (empty list = acceptable)."""
    problems: list[str] = []
    low = text.lower().strip()
    cited = _CARD_REF.findall(low)
    unknown = [c for c in cited if cards.get(c) is None]
    if unknown:
        problems.append(f"cites unknown cards {unknown}")
    backed = bool(cited) and not unknown
    if not backed:
        if any(re.search(p, low) for p in PLACE_FACT_PATTERNS):
            problems.append("states a fact about a place without a fact card")
        # A number in an opener is a statistic unless a card backs it.
        if re.search(r"\d", _CARD_REF.sub("", low)):
            problems.append("contains a number without a fact card")
    if not low.endswith("?"):
        problems.append("does not end with a question")
    if CLOSED_STARTS.search(low.split(".")[-1].strip()) or not any(
        m in low for m in OPEN_ENDED_MARKERS
    ):
        problems.append("question is not open-ended")
    return problems


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------


def board_context(
    persona: Persona, move: BoardMove, topic: str, form: CandidateForm
) -> str:
    ctx = {
        "speaker": f"{persona.name}, {persona.title}",
        "speaker_style": persona.style,
        "move": move.move.value,
        "move_reason": move.reason,
        "topic": topic,
        "next_topic": move.target if move.move == Move.move_on else None,
        "interview_language": form.interview_language,
    }
    return tag("board_context", json.dumps(ctx, ensure_ascii=False, indent=1))


def build_actor_user(
    persona: Persona,
    move: BoardMove,
    *,
    topic: str,
    form: CandidateForm,
    last_question: str,
    last_answer: str,
    claim: Claim | None = None,
    card: FactCard | None = None,
) -> str:
    parts = [
        board_context(persona, move, topic, form),
        tag("last_question", last_question),
        tag("candidate_speech", last_answer),
    ]
    if move.move == Move.fact_challenge and claim and card:
        parts.append(tag("claim_to_revisit", claim.text))
        parts.append(
            tag(
                "fact_card",
                json.dumps(
                    {"id": card.id, "statement": card.statement, "source": source_label(card)},
                    ensure_ascii=False,
                ),
            )
        )
    parts.append("Say the line now.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def stream_line(
    llm: LLMClient, kit: PromptKit, user: str, *, max_words: int, max_tokens: int, tag_: str,
    prompt_id: str = "actor", name: str = "actor",
) -> AsyncIterator[str]:
    """The raw streamed line, capped at `max_words`. Phase 2 feeds this to Timbre."""
    system = kit.system(prompt_id, max_words=str(max_words))
    chunks = llm.stream_text(
        name=name, system=system, user=user, max_tokens=max_tokens, tag=tag_
    )
    return stream_capped(chunks, max_words)


async def compose_line(
    llm: LLMClient,
    kit: PromptKit,
    persona: Persona,
    move: BoardMove,
    *,
    topic: str,
    form: CandidateForm,
    last_question: str,
    last_answer: str,
    max_words: int,
    max_tokens: int,
    turn_id: str,
    claim: Claim | None = None,
    card: FactCard | None = None,
    on_first_token: Callable[[], None] | None = None,
) -> LineResult:
    """Collect the streamed line and apply the guards. Used by the text CLI."""
    if move.move == Move.fact_challenge and (claim is None or card is None):
        # Defensive: the strategist never emits this, but a challenge without a card must
        # never be spoken.
        return LineResult(FILLER_TEMPLATES[Move.probe], is_fallback=True, guard="no_card")
    user = build_actor_user(
        persona, move, topic=topic, form=form, last_question=last_question,
        last_answer=last_answer, claim=claim, card=card,
    )
    text = ""
    try:
        async for chunk in stream_line(
            llm, kit, user, max_words=max_words, max_tokens=max_tokens, tag_=turn_id
        ):
            if not text and on_first_token is not None:
                on_first_token()
            text += chunk
    except LLMError:
        text = ""
    text = cap_words(text, max_words)
    if not text:
        if move.move == Move.fact_challenge and claim and card:
            return LineResult(fact_challenge_template(claim, card), is_fallback=True,
                              guard="actor_failed")
        return LineResult(FILLER_TEMPLATES.get(move.move, FILLER_TEMPLATES[Move.probe]),
                          is_fallback=True, guard="actor_failed")
    if move.move == Move.fact_challenge and claim and card:
        return guard_fact_challenge(text, claim, card)
    return LineResult(text)


def build_opener_user(form: CandidateForm, cards: CardSet) -> str:
    allowed = [
        {"id": c.id, "statement": c.statement}
        for c in cards.cards.values()
        if not c.synthetic_example
        and any(
            place and place.lower() in (c.statement + " " + " ".join(c.aliases)).lower()
            for place in (form.home_state, form.district)
        )
    ]
    return "\n".join(
        [
            tag("application_form", form.as_prompt_lines()),
            tag("allowed_facts", json.dumps(allowed, ensure_ascii=False)),
            "Say the opening line now.",
        ]
    )


async def generate_opener(
    llm: LLMClient, kit: PromptKit, form: CandidateForm, cards: CardSet, *,
    max_words: int, max_tokens: int, turn_id: str,
) -> LineResult:
    """The form-driven opening question, checked by `opener_violations`."""
    text = ""
    try:
        async for chunk in stream_line(
            llm, kit, build_opener_user(form, cards), max_words=max_words,
            max_tokens=max_tokens, tag_=turn_id, prompt_id="opener", name="opener",
        ):
            text += chunk
    except LLMError:
        return LineResult(opener_template(form), is_fallback=True, guard="opener_failed")
    text = cap_words(text, max_words)
    if not text or opener_violations(text, cards):
        return LineResult(opener_template(form), is_fallback=not text, guard="opener_guard")
    return LineResult(text)
