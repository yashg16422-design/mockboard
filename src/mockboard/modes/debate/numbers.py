"""Spotting figures in debate text: digits and number words (CTO_NOTES_04 §3).

Used in three places: the case-file validator (curated arguments are qualitative), the
custom-motion case file check, and the opponent's number guard (`guards.py`), which drops any
spoken sentence with a figure that is not on one of the motion's fact cards.

Deliberate narrowing (see DEBATE_BUILDER_LOG.md): "one", "two", "ek" and "do" are NOT number
words here. In Hinglish they are everyday words ("ek baat", "de do", "two arguments"), and
treating them as statistics would drop ordinary rhetoric. Every digit, every number word from
three upwards, every scale word (hundred, thousand, lakh, crore, million...), percentages and
fractions are caught. "saath" (sixty, but usually "with") is excluded for the same reason.
"""

from __future__ import annotations

import re

DIGITS = re.compile(r"\d+(?:[.,]\d+)*")

NUMBER_WORDS: frozenset[str] = frozenset(
    {
        # English cardinals from three, tens, scales, percentages and fractions.
        "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
        "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "hundreds", "thousand", "thousands", "million", "millions", "billion",
        "billions", "trillion", "dozen", "dozens", "percent", "percentage", "percentages",
        "half", "halves", "quarter", "quarters", "third", "thirds", "twice", "triple",
        "tripled", "doubled", "fold",
        # Hindi and Hinglish (Latin script).
        "teen", "chaar", "char", "paanch", "panch", "chhah", "chhe", "saat", "aath", "nau",
        "das", "gyarah", "barah", "bees", "tees", "chalis", "chaalis", "pachaas", "pachas",
        "sattar", "assi", "nabbe", "sau", "hazaar", "hazar", "hazaron", "lakh", "lac", "laakh",
        "lakhs", "crore", "crores", "karod", "karor", "karodon", "arab", "pratishat", "fisadi",
        "aadha", "aadhe", "aadhi", "dedh", "dhai", "chauthai", "tihai", "dugna", "tigna",
        # Hindi (Devanagari).
        "तीन", "चार", "पांच", "पाँच", "छह", "सात", "आठ", "नौ", "दस", "बीस", "पचास", "सौ",
        "हज़ार", "हजार", "लाख", "करोड़", "प्रतिशत", "आधा", "आधे", "डेढ़", "ढाई",
    }
)
PHRASES = (re.compile(r"\bper\s+cent\b", re.IGNORECASE), re.compile(r"%"))
_WORD = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)


def digit_values(text: str) -> list[float]:
    """Every digit group in the text as a number ("1,200" -> 1200, "12.5" -> 12.5)."""
    out: list[float] = []
    for m in DIGITS.finditer(text):
        raw = m.group(0)
        # "1,200" is a thousands separator; "12,5" is unusual in our inputs, treat as 125.
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:  # pragma: no cover - the regex only matches digits
            continue
    return out


def number_words(text: str) -> list[str]:
    found = [w for w in _WORD.findall(text.lower()) if w in NUMBER_WORDS]
    found.extend(p.pattern for p in PHRASES if p.search(text))
    return found


def mentions_number(text: str) -> bool:
    """True when the text contains any digit or number word (as defined above)."""
    return bool(DIGITS.search(text)) or bool(number_words(text))
