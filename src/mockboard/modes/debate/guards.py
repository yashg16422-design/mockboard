"""Code guards on the debate board's spoken lines (CTO_NOTES_04 §3). The prompt asks for the
same things; these make the important ones hold whatever the model writes.

1. **Number guard.** A sentence may contain a figure only if it matches a figure on one of
   the motion's fact cards (value, year) or one written in the motion itself ("Class 10").
   Any other sentence with a digit or number word (`numbers.py`) is dropped and logged. The
   whole line is checked for opening and closing statements; in voice mode the same check
   runs per sentence before TTS (`sentence_allowed`, for the voice loop's mode-agnostic
   `guard(move, sentence)` hook). Without cards, the opponent argues qualitatively.
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
from mockboard.modes.debate.numbers import digit_values, number_words

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


def allowed_figures(cards: Iterable[FactCard], motion_text: str) -> frozenset[float]:
    """Figures the opponent may say: the motion's cards' values and years, and any number
    written in the motion itself."""
    out: set[float] = set(digit_values(motion_text))
    for card in cards:
        if card.value is not None:
            out.add(float(card.value))
        if card.year is not None:
            out.add(float(card.year))
    return frozenset(out)


def sentence_allowed(sentence: str, allowed: frozenset[float]) -> bool:
    """True when every figure in the sentence is allowed.

    Every digit group must be an allowed value. A number word ("lakh", "percent", "teen")
    is accepted only alongside an allowed digit in the same sentence (a card's figure spoken
    as "42 lakh" or "12 percent"); on its own it is an uncarded figure."""
    digits = digit_values(sentence)
    if any(d not in allowed for d in digits):
        return False
    return not (number_words(sentence) and not digits)


@dataclass(frozen=True)
class NumberGuardResult:
    text: str
    dropped: tuple[str, ...]


def number_guard(text: str, allowed: frozenset[float]) -> NumberGuardResult:
    kept, dropped = [], []
    for sentence in split_sentences(text):
        (kept if sentence_allowed(sentence, allowed) else dropped).append(sentence)
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
