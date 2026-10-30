"""Code guards on the debate board's spoken lines (CTO_NOTES_04 §3). The prompt asks for the
same things; these make the important ones hold whatever the model writes.

1. **Number guard** (tightened by ruling 29, CTO_NOTES_05 M1/M2). A figure is allowed only
   in context:
   * a number written in the motion only inside the motion's own phrase ("Class 10"), and
     never next to a number word, a percent sign or a scale word;
   * a card's value or year only in a sentence that shares at least two content words with
     that card's statement or aliases (so "Hill towns got 42 lakh visitors" does not borrow
     a national arrivals figure);
   * the session decides which cards count: synthetic test cards only in offline (fake) runs.
   Any other sentence with a digit or number word (`numbers.py`) is dropped and logged. The
   whole line is checked for opening and closing statements; in voice mode the same check
   runs per sentence before TTS (`sentence_allowed`, for the voice loop's mode-agnostic
   `guard(move, sentence)` hook). Without cards, the opponent argues qualitatively.
4. **Off-limits backstop** (ruling 29, CTO should-fix 6). The motion screen's rule patterns
   for parties, religion, caste and communal conflict run over every opponent sentence; a
   hit is dropped and logged, whatever the model wrote. (The user's side is in the
   strategist's D2.)
2. **Closing concession.** A closing statement must concede something; if it does not, a
   template concession is put in front, inside the word cap.
3. **Restate fairly (soft).** Opening statements and rebuttals should restate the user's
   point first. A content-word overlap check logs a miss; it never blocks the line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from mockboard.brain.schemas import FactCard, Move
from mockboard.modes.debate.numbers import NUMBER_WORDS, digit_values, number_words

_SENTENCE = re.compile(r"(?<=[.?!।])\s+")
_WORD = re.compile(r"[a-zऀ-ॿ]+")
_STOP = frozenset(
    {
        "the", "a", "an", "of", "in", "is", "was", "are", "to", "and", "for", "by", "on", "it",
        "its", "this", "that", "these", "those", "be", "not", "no", "but", "with", "as", "at",
        "or", "if", "so", "do", "does", "did", "you", "your", "i", "we", "they", "he", "she",
        "my", "our", "their", "me", "us", "them", "hai", "hain", "ka", "ki", "ke", "ko", "mein",
        "se", "aur", "ya", "par", "bhi", "jo", "yeh", "ye", "woh", "wo", "kya", "nahi", "na",
        "hi", "tha", "thi", "ek", "aap", "aapka", "aapki", "aapne", "main", "mera", "meri",
        "hum", "hamara",
    }
)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]


@dataclass(frozen=True)
class CardFigures:
    """One card's figures and the content words a sentence must share to use them."""

    card_id: str
    figures: frozenset[float]
    words: frozenset[str]


@dataclass(frozen=True)
class FigurePolicy:
    """Which figures the opponent may say, and where (see the module docstring, item 1)."""

    motion_phrases: tuple[tuple[str, float], ...] = ()  # ("class", 10.0) for "Class 10"
    cards: tuple[CardFigures, ...] = ()

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(c.card_id for c in self.cards)


# Words in every test card's label; they say nothing about what a card is about.
_LABEL_WORDS = frozenset({"test", "card", "built", "data", "synthetic"})
_MIN_CARD_OVERLAP = 2
_PHRASE = re.compile(r"([A-Za-zऀ-ॿ]+)\s+(\d+(?:\.\d+)?)")
_NEXT_TOKEN = re.compile(r"\s*(%|[\wऀ-ॿ]+)")
_PREV_TOKEN = re.compile(r"([\wऀ-ॿ]+)\s*$")


def _topic_words(text: str) -> frozenset[str]:
    return frozenset(
        w for w in content_words(text) if w not in NUMBER_WORDS and w not in _LABEL_WORDS
    )


def allowed_figures(cards: Iterable[FactCard], motion_text: str) -> FigurePolicy:
    """The figure policy for one debate: the motion's own number phrases, and each card's
    value and year with the words a sentence must share to use them. The caller passes only
    the cards that may be cited (no synthetic cards outside offline runs, ruling 29)."""
    phrases = tuple(
        (m.group(1).lower(), float(m.group(2))) for m in _PHRASE.finditer(motion_text)
    )
    out = []
    for card in cards:
        figs = {float(v) for v in (card.value, card.year) if v is not None}
        if figs:
            words = _topic_words(" ".join([card.statement, *card.aliases]))
            out.append(CardFigures(card.id, frozenset(figs), words))
    return FigurePolicy(phrases, tuple(out))


def _strip_motion_phrases(sentence: str, policy: FigurePolicy) -> str:
    """Remove the motion's own number phrases ("Class 10"), unless a number word, a percent
    sign or a scale word sits right next to them ("Class 10 percent" keeps its figure)."""
    out = sentence
    for word, value in policy.motion_phrases:
        number = f"{value:g}"
        pattern = re.compile(rf"\b{re.escape(word)}\s+{re.escape(number)}\b", re.IGNORECASE)
        for m in reversed(list(pattern.finditer(out))):
            nxt = _NEXT_TOKEN.match(out, m.end())
            prev = _PREV_TOKEN.search(out[: m.start()])
            beside = {t.group(1).lower() for t in (nxt, prev) if t}
            if "%" in beside or beside & NUMBER_WORDS:
                continue
            out = out[: m.start()] + word + out[m.end():]
    return out


def sentence_allowed(sentence: str, policy: FigurePolicy) -> bool:
    """True when every figure in the sentence is allowed in this context.

    Every digit group must be a card figure of a card the sentence is about (at least two
    shared content words), unless it is the motion's own phrase. A number word ("lakh",
    "percent", "teen") is accepted only alongside such a digit; on its own it is an uncarded
    figure."""
    text = _strip_motion_phrases(sentence, policy)
    digits = digit_values(text)
    if not digits:
        return not number_words(text)
    words = _topic_words(sentence)
    ok: set[float] = set()
    for card in policy.cards:
        if len(words & card.words) >= _MIN_CARD_OVERLAP:
            ok |= card.figures
    return all(d in ok for d in digits)


@dataclass(frozen=True)
class NumberGuardResult:
    text: str
    dropped: tuple[str, ...]


def number_guard(text: str, policy: FigurePolicy) -> NumberGuardResult:
    kept, dropped = [], []
    for sentence in split_sentences(text):
        (kept if sentence_allowed(sentence, policy) else dropped).append(sentence)
    return NumberGuardResult(" ".join(kept), tuple(dropped))


# The screen's rule patterns that no opponent sentence may match (ruling 29).
OPPONENT_OFF_LIMITS = ("partisan", "religion", "caste", "communal")
# The user's side forces the Moderator's redirect (D2); religion alone is not enough there,
# because "mandir ke bahar ka chaat" is ordinary street-food talk, not an attack.
USER_OFF_LIMITS = ("partisan", "caste", "communal")


def off_limits_category(text: str, categories: Sequence[str]) -> str | None:
    """The first screen rule the text matches, among `categories`, or None."""
    from mockboard.modes.debate.screen import RULES

    return next((c for c in categories if RULES[c].search(text)), None)


def off_limits_guard(text: str) -> NumberGuardResult:
    """Drop every sentence that names a party, religion, caste or communal conflict."""
    kept, dropped = [], []
    for sentence in split_sentences(text):
        hit = off_limits_category(sentence, OPPONENT_OFF_LIMITS)
        (dropped if hit else kept).append(sentence)
    return NumberGuardResult(" ".join(kept), tuple(dropped))


# Concession markers (English and Hinglish). Generous on purpose: the template is the backstop.
_CONCESSION = re.compile(
    r"\b(?:i concede|i grant|i'll grant|i will grant|i accept|i admit|fair point|to be fair|"
    r"you(?:'re| are) right|good point|strong point|maanta hoon|maanti hoon|maanna padega|"
    r"maan leta|maan leti|sahi kaha|aapki baat sahi|point mazboot|mazboot point|"
    r"mazboot tha|mazboot thi|achha point)\b",
    re.IGNORECASE,
)


def has_concession(text: str) -> bool:
    return bool(_CONCESSION.search(text))


def content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def restates_user(line: str, user_speech: str, min_overlap: int = 2) -> bool:
    """Soft check: does the line's first sentence share content words with the user's speech?"""
    sentences = split_sentences(line)
    if not sentences or not user_speech.strip():
        return True  # nothing to restate
    first = " ".join(sentences[:2])
    return len(content_words(first) & content_words(user_speech)) >= min_overlap


RESTATE_MOVES = frozenset({Move.opening_statement, Move.rebut})


def guard_scope(move: Move) -> str:
    """How the number guard applies: "whole" for statements (checked whole before speaking),
    "sentence" for every other model-written line, "template" for fact challenges, whose
    figures come from the ask-never-assert template and the card itself."""
    if move == Move.fact_challenge:
        return "template"
    if move in (Move.opening_statement, Move.closing_statement):
        return "whole"
    return "sentence"


def cards_for_topics(cards: Sequence[FactCard], topics: Sequence[str]) -> list[FactCard]:
    return [c for c in cards if c.topic in set(topics)]
