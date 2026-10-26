"""Actor: turns the precomputed `BoardMove` into one spoken line (at most 30 words).

This is a plain async streaming call, NOT a LangGraph node: it is the only model call on the
real-time critical path. Three guards live here in code, not only in the prompt:

1. Word cap: streaming stops at `max_words` words, whatever the model does.
2. Ask, never assert (ADR-003): a fact challenge must end as a question, must not contain
   assertive "you are wrong" phrasing, must say the card's figure (in the card's own scale,
   see `spoken.py`) and must not read the card text aloud; otherwise a deterministic
   template replaces it.
3. Opener guard (ruling 5): the form-driven opener must be open-ended and must not state a
   fact about a place unless it cites an existing fact card; otherwise a template replaces it.

4. Actor fallback (ruling 14): the actor gets ONE attempt with a time budget
   (`first_token_budget_s`, from the request to the first token). If the call fails, or no
   token arrives in time, nothing is retried: the board says a fallback line that needs no
   model at all. A fact challenge falls back to its deterministic template (the claim is
   marked as challenged, so the challenge must still be heard); a `move_on` falls back to
   its move template (the topic really changes, so the board must say so); every other move
   falls back to one of the speaking persona's `fallback_lines` from its YAML file, which
   hands the turn back to the candidate ("Ek minute... aap apni baat jaari rakhiye").

A move may carry `preface="acknowledge"` (the candidate conceded an error, ruling 11). The
acknowledgement is a fixed sentence prepended here in code, not left to the model: it is
always said, it never pushes the line over the word cap, and in voice mode it can be spoken
from cached audio while the model's part is still streaming.
"""

from __future__ import annotations

import asyncio
import json
import re
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace

from mockboard.brain.cards import CardSet
from mockboard.brain.llm import LLMClient, LLMError
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import (
    BoardMove,
    CandidateForm,
    Claim,
    ClaimKind,
    FactCard,
    Move,
    Persona,
)
from mockboard.brain.spoken import (
    GENERIC_SOURCE,
    SYNTHETIC_SOURCE,
    challenge_sentence,
    spoken_parts,
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


# The acknowledgement said before a move when the candidate concedes an error, by language
# mix. "hi-en" is Hinglish in Latin script (the default persona language).
ACKNOWLEDGE_LINES: dict[str, str] = {
    "hi-en": "Theek hai, yeh maanna achhi baat hai.",
    "en": "Fair enough, it is good that you say so.",
    "hi": "ठीक है, यह मानना अच्छी बात है।",
}


def language_mix(persona: Persona, form: CandidateForm) -> str:
    """The language mix the board speaks in: the candidate's choice of pure English or pure
    Hindi wins; otherwise the persona's own mix (Hinglish by default)."""
    if form.interview_language == "english":
        return "en"
    if form.interview_language == "hindi":
        return "hi"
    return persona.language if persona.language in ACKNOWLEDGE_LINES else "hi-en"


def preface_text(move: BoardMove, mix: str) -> str:
    if move.preface == "acknowledge":
        return ACKNOWLEDGE_LINES.get(mix, ACKNOWLEDGE_LINES["hi-en"])
    return ""


def with_preface(preface: str, line: LineResult) -> LineResult:
    if not preface:
        return line
    return replace(line, text=f"{preface} {line.text}")


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
    # Ruling 14, for the trace: which fallback text was used ("persona_line",
    # "move_template", "challenge_template", "opener_template") and why the actor failed.
    fallback_kind: str = ""
    error: str = ""


class ActorBudgetExceeded(Exception):
    """No first token within the actor's time budget (ruling 14)."""


async def collect_line(
    chunks: AsyncIterator[str],
    *,
    first_token_budget_s: float | None,
    on_first_token: Callable[[], None] | None = None,
) -> str:
    """Collect a streamed line, giving up if the first chunk is later than the budget.

    Giving up cancels the wait, which closes the model stream (its call is recorded with
    error "cancelled") and raises `ActorBudgetExceeded`. The budget covers only the wait for
    the first chunk; a stream that stalls later ends by the lane's read timeout instead.
    """
    it = aiter(chunks)
    try:
        async with asyncio.timeout(first_token_budget_s):
            first = await anext(it)
    except StopAsyncIteration:
        return ""
    except TimeoutError as exc:
        raise ActorBudgetExceeded(
            f"no first token within {first_token_budget_s:g}s"
        ) from exc
    if on_first_token is not None:
        on_first_token()
    text = first
    async for chunk in it:
        text += chunk
    return text


def persona_fallback_line(persona: Persona, turn_id: str) -> str:
    """The speaking member's cached fallback line for this turn (ruling 14).

    Chosen by a stable hash of the turn id, so a run is reproducible and consecutive turns
    usually get different lines. A persona without lines gets the generic probe template.
    """
    lines = persona.fallback_lines
    if not lines:
        return FILLER_TEMPLATES[Move.probe]
    return lines[zlib.crc32(turn_id.encode("utf-8")) % len(lines)]


def actor_fallback(
    move: BoardMove,
    persona: Persona,
    turn_id: str,
    *,
    error: str,
    guard: str,
    claim: Claim | None = None,
    card: FactCard | None = None,
    mix: str = "hi-en",
) -> LineResult:
    """What the board says when the actor call failed or missed its budget (ruling 14)."""
    if move.move == Move.fact_challenge and claim and card:
        text, kind = fact_challenge_template(claim, card, mix), "challenge_template"
    elif move.move == Move.move_on:
        text, kind = FILLER_TEMPLATES[Move.move_on], "move_template"
    else:
        text, kind = persona_fallback_line(persona, turn_id), "persona_line"
    return LineResult(text, is_fallback=True, guard=guard, fallback_kind=kind, error=error)


# ---------------------------------------------------------------------------
# Deterministic templates (used as guards and as the offline fallback)
# ---------------------------------------------------------------------------


def spoken_source(card: FactCard, mix: str = "hi-en") -> str:
    """The short source name the board says aloud. Never the long `source_title`."""
    if card.source_short:
        return card.source_short
    table = SYNTHETIC_SOURCE if card.synthetic_example else GENERIC_SOURCE
    return table.get(mix, table["hi-en"])


@dataclass(frozen=True)
class ChallengeFacts:
    """The structured fields of a fact challenge, all rendered in the card's own scale."""

    claim_figure: str
    card_figure: str
    card_figure_core: str  # the part the guard requires in a model line ("15 lakh")
    card_year: int | None
    source_short: str
    is_date: bool
    mix: str

    def as_prompt_json(self) -> str:
        return json.dumps(
            {
                "claim_figure": self.claim_figure,
                "card_figure": self.card_figure,
                "card_year": self.card_year,
                "is_date": self.is_date,
                "source_short": self.source_short,
                "language_mix": self.mix,
            },
            ensure_ascii=False,
        )


def challenge_facts(claim: Claim, card: FactCard, mix: str = "hi-en") -> ChallengeFacts:
    is_date = card.kind == ClaimKind.date or claim.kind == ClaimKind.date
    if card.value is not None:
        card_parts = spoken_parts(card.value, card.unit, is_date=is_date, mix=mix)
    else:  # defensive: the strategist only challenges cards that carry a value
        card_parts = None
    if claim.value is not None:
        claim_text = spoken_parts(
            claim.value, claim.unit, like=card.unit, is_date=is_date, mix=mix
        ).text
    else:
        claim_text = f"\"{cap_words(claim.text, 8)}\""
    return ChallengeFacts(
        claim_figure=claim_text,
        card_figure=card_parts.text if card_parts else "",
        card_figure_core=card_parts.core if card_parts else "",
        card_year=card.year,
        source_short=spoken_source(card, mix),
        is_date=is_date,
        mix=mix,
    )


def fact_challenge_template(claim: Claim, card: FactCard, mix: str = "hi-en") -> str:
    f = challenge_facts(claim, card, mix)
    return challenge_sentence(
        claim_figure=f.claim_figure,
        card_figure=f.card_figure,
        card_year=f.card_year,
        source_short=f.source_short,
        is_date=f.is_date,
        mix=f.mix,
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def says_figure(text: str, core: str) -> bool:
    """True when the line says the card's figure core ("12 percent", "15 lakh"), allowing
    "%" for "percent" and any spacing or case."""
    if not core:
        return False
    line = _norm(text).replace("%", " percent")
    return _norm(core) in re.sub(r"\s+", " ", line)


def fact_challenge_violations(text: str, claim: Claim, card: FactCard, mix: str) -> list[str]:
    """Why a model-written fact challenge is not acceptable (empty list = acceptable)."""
    problems: list[str] = []
    if not text.strip().endswith("?"):
        problems.append("does not end with a question")
    if has_assertion(text):
        problems.append("asserts instead of asking")
    if not says_figure(text, challenge_facts(claim, card, mix).card_figure_core):
        problems.append("does not say the card's figure")
    low = _norm(text)
    if "test card" in low or _norm(card.statement) in low:
        problems.append("reads the card text aloud")
    return problems


def guard_fact_challenge(
    text: str, claim: Claim, card: FactCard, mix: str = "hi-en"
) -> LineResult:
    if fact_challenge_violations(text, claim, card, mix):
        return LineResult(fact_challenge_template(claim, card, mix), guard="ask_never_assert")
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
        # Already spoken by code before the model's line; the model must not repeat it.
        "preface_already_spoken": move.preface,
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
        # Structured, pre-rendered fields only (actor.v2): the card's text is NOT passed, so
        # the model cannot read it aloud, and both figures are already in the card's scale.
        parts.append(tag("claim_to_revisit", claim.text))
        facts = challenge_facts(claim, card, language_mix(persona, form))
        parts.append(tag("fact_challenge", facts.as_prompt_json()))
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
    first_token_budget_s: float | None = None,
) -> LineResult:
    """Collect the streamed line and apply the guards. Used by the text CLI.

    `first_token_budget_s` is the actor's time budget (ruling 14); None means no budget."""
    preface = preface_text(move, language_mix(persona, form))
    line = await _compose_move_line(
        llm, kit, persona, move, topic=topic, form=form, last_question=last_question,
        last_answer=last_answer, max_words=max_words - count_words(preface),
        max_tokens=max_tokens, turn_id=turn_id, claim=claim, card=card,
        on_first_token=on_first_token, first_token_budget_s=first_token_budget_s,
    )
    return with_preface(preface, line)


async def _compose_move_line(
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
    claim: Claim | None,
    card: FactCard | None,
    on_first_token: Callable[[], None] | None,
    first_token_budget_s: float | None,
) -> LineResult:
    if move.move == Move.fact_challenge and (claim is None or card is None):
        # Defensive: the strategist never emits this, but a challenge without a card must
        # never be spoken.
        return LineResult(FILLER_TEMPLATES[Move.probe], is_fallback=True, guard="no_card")
    user = build_actor_user(
        persona, move, topic=topic, form=form, last_question=last_question,
        last_answer=last_answer, claim=claim, card=card,
    )
    mix = language_mix(persona, form)
    # One attempt only (ruling 14): any failure is answered with a fallback line, not a retry.
    try:
        text = await collect_line(
            stream_line(llm, kit, user, max_words=max_words, max_tokens=max_tokens,
                        tag_=turn_id),
            first_token_budget_s=first_token_budget_s,
            on_first_token=on_first_token,
        )
    except ActorBudgetExceeded as exc:
        return actor_fallback(move, persona, turn_id, error=str(exc), guard="actor_budget",
                              claim=claim, card=card, mix=mix)
    except LLMError as exc:
        return actor_fallback(move, persona, turn_id, error=f"{type(exc).__name__}: {exc}"[:300],
                              guard="actor_failed", claim=claim, card=card, mix=mix)
    text = cap_words(text, max_words)
    if not text:
        return actor_fallback(move, persona, turn_id, error="empty line", guard="actor_failed",
                              claim=claim, card=card, mix=mix)
    if move.move == Move.fact_challenge and claim and card:
        return guard_fact_challenge(text, claim, card, mix)
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
    max_words: int, max_tokens: int, turn_id: str, first_token_budget_s: float | None = None,
) -> LineResult:
    """The form-driven opening question, checked by `opener_violations`. The opener runs on
    the actor lane, so it gets the same single attempt and time budget (ruling 14)."""
    try:
        text = await collect_line(
            stream_line(llm, kit, build_opener_user(form, cards), max_words=max_words,
                        max_tokens=max_tokens, tag_=turn_id, prompt_id="opener",
                        name="opener"),
            first_token_budget_s=first_token_budget_s,
        )
    except (ActorBudgetExceeded, LLMError) as exc:
        return LineResult(opener_template(form), is_fallback=True, guard="opener_failed",
                          fallback_kind="opener_template",
                          error=f"{type(exc).__name__}: {exc}"[:300])
    text = cap_words(text, max_words)
    if not text:
        return LineResult(opener_template(form), is_fallback=True, guard="opener_guard",
                          fallback_kind="opener_template", error="empty line")
    if opener_violations(text, cards):
        return LineResult(opener_template(form), guard="opener_guard")
    return LineResult(text)
