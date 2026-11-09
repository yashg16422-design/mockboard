"""Actor: turns the precomputed `BoardMove` into one spoken line (at most 30 words).

This is a plain async streaming call, NOT a LangGraph node. It is the last model call on the
real-time critical path; the other one is the last segment's listener call, which can start
only after the candidate stops (ADR-002). Guards live here in code, not only in the prompt:

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

5. Voice mode (ruling 17). If a masking filler ("Hmm...") has already been played this turn,
   a failed actor call falls back to the move's template question instead of the persona
   line: a filler followed by "Ek minute... aap apni baat jaari rakhiye" sounds broken and
   asks nothing. Pipecat tells us through `filler_played` (a callable read at failure time,
   because the filler starts while the actor call is still pending). Text mode has no
   filler, so it never sets it and nothing changes there.

6. Fast-throttle retry (ruling 17, off by default). With `fast_throttle_retry_window_s` set,
   a call that fails with a ThrottlingException within that window of its request, before
   any token, is retried ONCE inside what is left of the first-token budget. Any other
   failure, a slow throttle, or a second failure takes the fallback above.

A move may carry `preface="acknowledge"` (the candidate conceded an error, ruling 11). The
acknowledgement is a fixed sentence prepended here in code, not left to the model: it is
always said, it never pushes the line over the word cap, and in voice mode it can be spoken
from cached audio while the model's part is still streaming.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import zlib
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

from mockboard.brain.cards import CardSet
from mockboard.brain.llm import LLMClient, LLMError, LLMThrottledError, PromptBlock
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
    "ta-en": "சரி, இதை ஒப்புக்கொண்டது நல்லது.",
}


def language_mix(persona: Persona, form: CandidateForm) -> str:
    """The language mix the board speaks in: the candidate's choice of pure English or pure
    Hindi wins; otherwise the persona's own mix (Hinglish by default)."""
    if form.interview_language == "english":
        return "en"
    if form.interview_language == "hindi":
        return "hi"
    if form.interview_language == "tanglish":
        return "ta-en"
    return persona.language if persona.language in ACKNOWLEDGE_LINES else "hi-en"


# Output tokens per word are far higher in Tamil and Devanagari script than in Latin script. With
# the Latin-sized budget a Tamil line was cut mid-word on a real Nova sitting (Nov 6): "... அப்படி
# என்ற". The word cap still sets the length; this only lets a line of that length finish.
TOKEN_SCALE_BY_MIX = {"ta-en": 4, "hi": 3}


def token_budget(max_tokens: int, mix: str) -> int:
    """The actor's output-token budget for a line in this language mix."""
    return max_tokens * TOKEN_SCALE_BY_MIX.get(mix, 1)


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


# Voice mode (V3, ADR-008): `session.speak_stream()` sets this, inside its own actor task only,
# to a callable that receives the capped text as it streams, so complete sentences can go to
# Timbre before the line ends. Context variables are per task, so concurrent sessions and the
# text CLI (where it stays None) are unaffected; the guards below still see the whole line.
ACTOR_TEXT_SINK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "actor_text_sink", default=None
)


def _to_sink(text: str) -> None:
    sink = ACTOR_TEXT_SINK.get()
    if sink is not None:
        sink(text)


async def stream_capped(
    chunks: AsyncIterator[str], max_words: int, gate: SentenceGate | None = None
) -> AsyncIterator[str]:
    """Pass text chunks through until `max_words` words have been emitted, then stop.
    Every chunk passed on also goes to `ACTOR_TEXT_SINK` when one is set (voice mode).

    With a `gate` (rulings 36 and 37), text is passed on one whole sentence at a time, and
    the line stops before the first sentence the gate refuses; that sentence never reaches
    the sink, so in voice mode it is never spoken."""
    if gate is not None:
        async for sentence in _gated(stream_capped(chunks, max_words), gate):
            yield sentence
        return
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
            _to_sink("".join(out))
            yield "".join(out)
        if emitted >= max_words:
            return
    if pending.strip() and emitted < max_words:
        _to_sink(pending)
        yield pending


async def _gated(words: AsyncIterator[str], gate: SentenceGate) -> AsyncIterator[str]:
    """Re-chunk an (ungated, unsunk) word stream into whole sentences that pass the gate."""
    token = ACTOR_TEXT_SINK.set(None)  # the inner stream must not feed the sink itself
    try:
        buf = ""
        async for chunk in words:
            buf += chunk
            while (m := _SENTENCE_BREAK.search(buf)) is not None:
                sentence, buf = buf[: m.end()], buf[m.end():]
                if not gate.ok(sentence.strip()):
                    if gate.stopped is None:  # dropped, not stopped (`skip_quotes`)
                        continue
                    return
                ACTOR_TEXT_SINK.reset(token)
                _to_sink(sentence)
                token = ACTOR_TEXT_SINK.set(None)
                yield sentence
        if buf.strip() and gate.ok(buf.strip()):
            ACTOR_TEXT_SINK.reset(token)
            _to_sink(buf)
            token = ACTOR_TEXT_SINK.set(None)
            yield buf
    finally:
        ACTOR_TEXT_SINK.reset(token)


# A sentence ends at . ? ! or । followed by whitespace ("12.5" is not a break).
_SENTENCE_BREAK = re.compile(r"[.?!।](?=\s)\s*")

# Ruling 36: quiz-style questions that ask the candidate for a number, share, statistic or
# year. Word lists are our own (UNVERIFIED against real Nova lines); only questions count.
_QUIZ = re.compile(
    r"\bkitn[aei]\b|\bkitne\b|\bhow (?:much|many)\b|\bwhat percent|\bkis (?:saal|varsh)\b"
    r"|\b(?:which|what) year\b"
    r"|\b(?:share|percent(?:age)?|pratishat|figure|aankd[ae]|aankda|statistics?|gdp|number)\b"
    r"(?:\s+\S+)?\s+kya (?:hai|hain|tha|hoga)\b"
    r"|\bwhat (?:is|was|are|were) (?:the )?(?:share|percentage|figure|number|statistics?|"
    r"rate|gdp)\b"
    r"|कितन[ाीे]|किस (?:साल|वर्ष)|(?:प्रतिशत|आँकड़\S*|आंकड़\S*|हिस्सेदारी)(?:\s+\S+)?\s+क्या (?:है|था)",
    re.IGNORECASE,
)

# Ruling 37: reporting frames that introduce the candidate's own words.
_REPORTING = re.compile(
    r"\baapne\b(?P<a>.{0,60}?)\b(?:kaha|bola|bataya|zikr kiya)\b"
    r"|\baap(?:ka)? (?:keh rahe (?:hain|the)|kehna hai|kehte hain)(?: ki)?\b(?P<b>.{0,60})"
    r"|\byou (?:said|mentioned|told us|say)\b(?P<c>.{0,60})"
    r"|आपने(?P<d>.{0,60}?)(?:कहा|बताया)",
    re.IGNORECASE,
)
_QUOTE_STOP = frozenset({
    "the", "a", "an", "of", "in", "is", "to", "and", "that", "it", "hai", "hain", "ka", "ki",
    "ke", "ko", "mein", "me", "se", "aur", "jo", "yeh", "ye", "woh", "bhi", "hi",
    "है", "हैं", "का", "की", "के", "को", "में", "से", "और", "जो", "यह", "भी",
})
_TOKEN = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")
MIN_COPIED_RUN = 5  # this many consecutive words copied from the answer is a quote anywhere


def quiz_question(sentence: str) -> bool:
    """True when a question asks the candidate for a figure, a share or a year (ruling 36)."""
    return sentence.rstrip().endswith("?") and bool(_QUIZ.search(sentence))


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _runs(a: list[str], b: list[str], n: int) -> bool:
    grams = {tuple(b[i:i + n]) for i in range(len(b) - n + 1)}
    return any(tuple(a[i:i + n]) in grams for i in range(len(a) - n + 1))


def quotes_answer(sentence: str, answer: str) -> bool:
    """True when the sentence repeats the candidate's words (ruling 37): a reporting frame
    ("Aapne ... kaha", "you said ...") around two or more consecutive words of the answer
    that are not all filler, or a run of `MIN_COPIED_RUN` words copied anywhere."""
    said = _tokens(answer)
    if not said:
        return False
    line = _tokens(sentence)
    if _runs(line, said, MIN_COPIED_RUN):
        return True
    for m in _REPORTING.finditer(sentence):
        inner = _tokens(next((g for g in m.groups() if g), ""))
        for i in range(len(inner) - 1):
            pair = inner[i:i + 2]
            if _runs(pair, said, 2) and not all(w in _QUOTE_STOP for w in pair):
                return True
    return False


@dataclass
class SentenceGate:
    """Checks each sentence of a streamed UPSC board line (rulings 36 and 37). `stopped` holds
    the first refused sentence and `reason` why."""

    answer: str = ""
    check_quotes: bool = True
    stopped: str | None = None
    reason: str = ""
    # The study partner: a sentence that copies the speaker is dropped and the line goes on, so
    # Mitra's own question survives a restatement that leaned on their words (Nov 9: 3 of 8
    # real partner lines were lost to the stop). A quiz question still stops the line.
    skip_quotes: bool = False
    skipped: list[str] = field(default_factory=list)

    def ok(self, sentence: str) -> bool:
        if self.stopped is not None:
            return False
        if quiz_question(sentence):
            self.stopped, self.reason = sentence, "quiz_question"
        elif self.check_quotes and quotes_answer(sentence, self.answer):
            if self.skip_quotes:
                self.skipped.append(sentence)
                self.reason = "quotes_answer"
                return False
            self.stopped, self.reason = sentence, "quotes_answer"
        return self.stopped is None


@dataclass(frozen=True)
class LineResult:
    text: str
    is_fallback: bool = False
    guard: str = ""  # which guard replaced the model's line, if any
    # Ruling 14, for the trace: which fallback text was used ("persona_line",
    # "move_template", "challenge_template", "opener_template") and why the actor failed.
    fallback_kind: str = ""
    error: str = ""
    # Ruling 17: True when the actor's first attempt was throttled fast and retried once.
    retried: bool = False


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


def _mixed_filler(move: Move, mix: str) -> str:
    """Hinglish keeps FILLER_TEMPLATES (what the debate and old board always said); other mixes
    use their own language."""
    if mix in ("en", "hi", "ta-en"):
        return filler_for(move, mix)
    return FILLER_TEMPLATES.get(move, FILLER_TEMPLATES[Move.probe])


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
    filler_played: bool = False,
) -> LineResult:
    """What the board says when the actor call failed or missed its budget (ruling 14).

    `filler_played` (voice mode, ruling 17): a masking filler was already played this turn,
    so the persona's fallback line (itself a pause) is replaced by the move's template
    question, and the candidate never hears two pauses in a row."""
    if move.move == Move.fact_challenge and claim and card:
        text, kind = fact_challenge_template(claim, card, mix), "challenge_template"
    elif move.move == Move.move_on:
        text, kind = _mixed_filler(Move.move_on, mix), "move_template"
    elif filler_played or mix in ("en", "hi", "ta-en"):
        # The persona's cached lines are Hinglish: another language hears its own template.
        text, kind = _mixed_filler(move.move, mix), "move_template"
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
    if form.interview_language == "tanglish":
        return (
            f"வணக்கம், அமருங்கள். நீங்கள் {place} பகுதியைச் சேர்ந்தவர்; "
            f"{form.degree_subject} படித்திருக்கிறீர்கள். "
            "உங்கள் பகுதியில் நீங்கள் நெருக்கமாகக் கவனித்த ஒரு பிரச்சினை என்ன, அதை எப்படி தீர்ப்பீர்கள்?"
        )
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
    Move.clarify: "Maaf kijiye, main theek se sun nahi paaya. Kya aap phir se bata sakte hain?",
    Move.interrupt: "Ek minute, main beech mein rokta hoon. Seedhe mukhya baat par aaiye.",
}
_TAMIL_FILLERS = {
    Move.probe: "சரி. இதில் உங்களுக்கு முக்கியமாகத் தோன்றுவது என்ன?",
    Move.counter: "மறுபக்கத்தையும் பார்க்கும்போது, அதற்கு உங்கள் பதில் என்ன?",
    Move.steelman: "மறுபக்கத்தின் வலுவான கருத்தை நீங்கள் எப்படி விளக்குவீர்கள்?",
    Move.pressure: "உங்கள் கருத்தைத் தெளிவாக்கும் ஒரு உதாரணம் சொல்ல முடியுமா?",
    Move.reassure: "பரவாயில்லை, நிதானமாக யோசிக்கலாம். எளிய இடத்திலிருந்து தொடங்குங்கள்.",
    Move.move_on: "சரி, நன்றி. இப்போது அடுத்த தலைப்புக்குச் செல்வோம்.",
    Move.clarify: "மன்னிக்கவும், தெளிவாகக் கேட்கவில்லை. மீண்டும் சொல்ல முடியுமா?",
    Move.interrupt: "ஒரு நிமிடம், முக்கியமான கருத்துக்கு நேராக வர முடியுமா?",
}
for _move, _line in _TAMIL_FILLERS.items():
    FILLER_TEMPLATES.setdefault(_move, _line)
_ENGLISH_FILLERS = {
    Move.probe: "I see. Could you say a little more about why?",
    Move.counter: "There is another side to this. What would you say to it?",
    Move.steelman: "All right. What would be the strongest argument on the other side?",
    Move.pressure: "Could you give one concrete example that makes your point clear?",
    Move.reassure: "No hurry, take a moment. Let us start from a simpler point.",
    Move.move_on: "Thank you. Let us move to a new subject.",
    Move.clarify: "Sorry, I could not hear that clearly. Could you say it again?",
    Move.interrupt: "One moment, please. Could you come to your main point?",
}
_HINDI_FILLERS = {
    Move.probe: "अच्छा। इस पर थोड़ा और विस्तार से बताइए, ऐसा क्यों?",
    Move.counter: "लेकिन दूसरा पक्ष भी है। उसके बारे में आप क्या कहेंगे?",
    Move.steelman: "ठीक है। अब दूसरे पक्ष का सबसे मज़बूत तर्क क्या होगा?",
    Move.pressure: "कोई एक ठोस उदाहरण दीजिए, जिससे आपकी बात साफ़ हो।",
    Move.reassure: "कोई बात नहीं, आराम से सोचिए। चलिए, एक आसान बात से शुरू करते हैं।",
    Move.move_on: "ठीक है, धन्यवाद। अब एक नए विषय पर आते हैं।",
    Move.clarify: "माफ़ कीजिए, आपकी बात ठीक से सुनाई नहीं दी। क्या आप फिर से बता सकते हैं?",
    Move.interrupt: "एक क्षण, कृपया सीधे मुख्य बात पर आइए।",
}
# The panel has women members, so its Hinglish fallbacks avoid the masculine first person of
# FILLER_TEMPLATES' clarify and interrupt ("sun nahi paaya", "rokta hoon"). FILLER_TEMPLATES
# itself is unchanged for anything else that reads it.
_PANEL_HINGLISH_FILLERS = {
    **FILLER_TEMPLATES,
    Move.clarify: (
        "Maaf kijiye, aapki baat theek se sunai nahi di. Kya aap phir se bata sakte hain?"
    ),
    Move.interrupt: "Ek minute, kripya seedhe mukhya baat par aaiye.",
}
_FILLERS_BY_MIX: dict[str, dict[Move, str]] = {
    "hi-en": _PANEL_HINGLISH_FILLERS,
    "en": _ENGLISH_FILLERS,
    "hi": _HINDI_FILLERS,
    "ta-en": _TAMIL_FILLERS,
}


def filler_for(move: Move, mix: str = "hi-en") -> str:
    """The move's fixed fallback question in the sitting's language (Hinglish when unknown).

    Before this, English, Hindi and Tanglish sittings heard the Hinglish template whenever the
    model line failed or a guard cut it."""
    table = _FILLERS_BY_MIX.get(mix, _PANEL_HINGLISH_FILLERS)
    return table.get(move, table[Move.probe])


# Ruling 37: the generic request to repeat, by language mix. "sun nahi paaya" (I could not
# hear) puts the problem on the board's side, which suits speech recognition errors.
CLARIFY_LINES: dict[str, str] = {
    "hi-en": "Maaf kijiye, main theek se sun nahi paaya. Kya aap phir se bata sakte hain?",
    "en": "Sorry, I could not hear that clearly. Could you say it again?",
    "hi": "माफ़ कीजिए, मैं ठीक से सुन नहीं पाया। क्या आप फिर से बता सकते हैं?",
    "ta-en": "மன்னிக்கவும், தெளிவாகக் கேட்கவில்லை. இன்னொரு முறை சொல்ல முடியுமா?",
}

# Moves whose model lines are checked for repeating the candidate's words (ruling 37 names
# clarify, pressure and probe; clarify is a template, and counter and steelman answer the
# candidate's point just as closely, so they are checked too).
QUOTE_CHECKED_MOVES = frozenset(
    {Move.probe, Move.pressure, Move.counter, Move.steelman, Move.reassure, Move.interrupt}
)


def guard_question(move: BoardMove, topic: str, mix: str = "hi-en") -> str:
    """The fixed open-ended question that finishes a line a guard cut short."""
    if move.move == Move.move_on and move.target and move.target != "close":
        if mix == "ta-en":
            return "அடுத்த தலைப்புக்கு வருவோம். இதில் உங்களுக்கு முக்கியமான சவால் எது, ஏன்?"
        if mix == "en":
            return ("Let us move on. In this area, which challenge seems most pressing to you, "
                    "and why?")
        if mix == "hi":
            return "अब अगले विषय पर आते हैं। इस क्षेत्र में आपको कौन-सी चुनौती सबसे ज़रूरी लगती है, और क्यों?"
        return (f"Ab {move.target.replace('_', ' ')} par aate hain: is kshetra mein aapko "
                "kaunsi chunauti sabse zaroori lagti hai, aur kyun?")
    if move.move == Move.move_on:
        if mix == "ta-en":
            return "இன்னும் ஏதாவது சேர்க்க விரும்புகிறீர்களா?"
        if mix == "en":
            return "Would you like to add anything else?"
        if mix == "hi":
            return "क्या आप कुछ और कहना चाहेंगे?"
        return "Kya aap kuch aur kehna chahenge?"
    if mix in ("ta-en", "en", "hi"):
        return filler_for(move.move, mix)
    return FILLER_TEMPLATES.get(move.move, FILLER_TEMPLATES[Move.probe])


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


def line_system(kit: PromptKit, *, max_words: int, prompt_id: str = "actor") -> list[PromptBlock]:
    """The system blocks of a spoken-line call: the cached persona + cards prefix, then the
    role's instructions. Shared with `probes/nova_probe.py --app-prompts`."""
    return kit.system(prompt_id, max_words=str(max_words))


def stream_line(
    llm: LLMClient, kit: PromptKit, user: str, *, max_words: int, max_tokens: int, tag_: str,
    prompt_id: str = "actor", name: str = "actor", gate: SentenceGate | None = None,
) -> AsyncIterator[str]:
    """The raw streamed line, capped at `max_words`. Phase 2 feeds this to Timbre."""
    system = line_system(kit, max_words=max_words, prompt_id=prompt_id)
    chunks = llm.stream_text(
        name=name, system=system, user=user, max_tokens=max_tokens, tag=tag_
    )
    return stream_capped(chunks, max_words, gate)


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
    filler_played: Callable[[], bool] | None = None,
    fast_throttle_retry_window_s: float | None = None,
) -> LineResult:
    """Collect the streamed line and apply the guards. Used by the text CLI.

    `first_token_budget_s` is the actor's time budget (ruling 14); None means no budget.
    `filler_played` is read if the call fails: True means a masking filler already played
    this turn (voice mode, ruling 17). `fast_throttle_retry_window_s` enables the one
    fast-throttle retry (ruling 17); None (the default) means no retry at all."""
    preface = preface_text(move, language_mix(persona, form))
    line = await _compose_move_line(
        llm, kit, persona, move, topic=topic, form=form, last_question=last_question,
        last_answer=last_answer, max_words=max_words - count_words(preface),
        max_tokens=max_tokens, turn_id=turn_id, claim=claim, card=card,
        on_first_token=on_first_token, first_token_budget_s=first_token_budget_s,
        filler_played=filler_played, fast_throttle_retry_window_s=fast_throttle_retry_window_s,
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
    filler_played: Callable[[], bool] | None = None,
    fast_throttle_retry_window_s: float | None = None,
) -> LineResult:
    if move.move == Move.fact_challenge and (claim is None or card is None):
        # Defensive: the strategist never emits this, but a challenge without a card must
        # never be spoken.
        return LineResult(FILLER_TEMPLATES[Move.probe], is_fallback=True, guard="no_card")
    mix = language_mix(persona, form)
    if move.move == Move.clarify:
        # Ruling 37: ask generically, never echo the (possibly mis-heard) transcript. No model
        # call, so nothing of the candidate's words can be read back.
        return LineResult(CLARIFY_LINES.get(mix, CLARIFY_LINES["hi-en"]), guard="clarify_template")
    gate = (
        None if move.move == Move.fact_challenge
        else SentenceGate(answer=last_answer, check_quotes=move.move in QUOTE_CHECKED_MOVES)
    )
    user = build_actor_user(
        persona, move, topic=topic, form=form, last_question=last_question,
        last_answer=last_answer, claim=claim, card=card,
    )

    def fallback(error: str, guard: str, retried: bool) -> LineResult:
        played = bool(filler_played()) if filler_played is not None else False
        line = actor_fallback(move, persona, turn_id, error=error, guard=guard, claim=claim,
                              card=card, mix=mix, filler_played=played)
        return replace(line, retried=retried)

    call = await call_actor(
        llm, kit, user, max_words=max_words, max_tokens=max_tokens, turn_id=turn_id,
        on_first_token=on_first_token, first_token_budget_s=first_token_budget_s,
        fast_throttle_retry_window_s=fast_throttle_retry_window_s, gate=gate,
    )
    if call.error:
        return fallback(call.error, call.guard, call.retried)
    text, retried = call.text, call.retried
    if move.move == Move.fact_challenge and claim and card:
        return replace(guard_fact_challenge(text, claim, card, mix), retried=retried)
    if gate is not None and gate.stopped is not None and not text.rstrip().endswith("?"):
        # A refused sentence cut the line before its question: finish with the move's fixed,
        # open-ended question (voice mode appends the same one, `is_fallback` tells it to).
        question = guard_question(move, topic, mix)
        return LineResult(f"{text} {question}".strip(), is_fallback=True, guard=gate.reason,
                          fallback_kind="move_template", retried=retried)
    return LineResult(text, retried=retried, guard=gate.reason if gate and gate.stopped else "")


@dataclass(frozen=True)
class ActorCall:
    """The outcome of the actor's single attempt (ruling 14): the capped text, or an error
    with the guard name to record ("actor_budget" or "actor_failed")."""

    text: str = ""
    error: str = ""
    guard: str = ""
    retried: bool = False


async def call_actor(
    llm: LLMClient,
    kit: PromptKit,
    user: str,
    *,
    max_words: int,
    max_tokens: int,
    turn_id: str,
    on_first_token: Callable[[], None] | None,
    first_token_budget_s: float | None,
    fast_throttle_retry_window_s: float | None = None,
    prompt_id: str = "actor",
    gate: SentenceGate | None = None,
) -> ActorCall:
    """One streamed actor attempt with the first-token budget (ruling 14) and the optional
    fast-throttle retry (ruling 17). Never raises for model failures; the caller chooses the
    fallback line. Shared by every mode (ADR-009); `prompt_id` picks the mode's actor prompt."""
    started = time.monotonic()
    budget = first_token_budget_s
    retried = False
    while True:
        got_token = False

        def first_token() -> None:
            nonlocal got_token
            got_token = True
            if on_first_token is not None:
                on_first_token()

        attempt_started = time.monotonic()
        try:
            text = await collect_line(
                stream_line(llm, kit, user, max_words=max_words, max_tokens=max_tokens,
                            tag_=turn_id, prompt_id=prompt_id, gate=gate),
                first_token_budget_s=budget,
                on_first_token=first_token,
            )
            break
        except ActorBudgetExceeded as exc:
            return ActorCall(error=str(exc), guard="actor_budget", retried=retried)
        except LLMError as exc:
            now = time.monotonic()
            left = None if first_token_budget_s is None else first_token_budget_s - (now - started)
            if (
                fast_throttle_retry_window_s is not None
                and not retried
                and not got_token
                and isinstance(exc, LLMThrottledError)
                and now - attempt_started <= fast_throttle_retry_window_s
                and (left is None or left > 0)
            ):
                retried, budget = True, left
                continue
            return ActorCall(
                error=f"{type(exc).__name__}: {exc}"[:300], guard="actor_failed", retried=retried
            )
    text = cap_words(text, max_words)
    if not text and gate is not None and (gate.stopped is not None or gate.skipped):
        return ActorCall(text="", guard=gate.reason, retried=retried)  # every sentence refused
    if not text:
        return ActorCall(error="empty line", guard="actor_failed", retried=retried)
    return ActorCall(text=text, retried=retried)


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
