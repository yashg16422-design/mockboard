"""How the board SAYS a figure, and the fact-challenge sentence built from it.

Pure functions, no model and no I/O. They sit beside `numbers.py` (which parses and
compares figures) and are shared by the actor's template, the actor's guard and the fake
actor, so all three speak a figure in exactly the same words (CTO_NOTES_02 M3, ruling 11).

The key rule is "render in the card's own scale". A candidate who says "15 lakh" and a card
that says "1.5 million" describe the same quantity, but "Aapne 1,500,000 kaha ... yeh 15
lakh hai" sounds like a contradiction and reads absurdly in a TTS voice. So the candidate's
figure is converted into the card's unit and scale word before it is spoken: "15 lakh
visitors" against "15 lakh visitors", or "1.5 million visitors" against "1.5 million
visitors".

Three language mixes are supported: "hi-en" (Hinglish in Latin script, the default persona
mix), "en" and "hi" (Hindi in Devanagari). Free-form unit nouns such as "visitors" or
"districts" come from the card and are kept as written in every mix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mockboard.brain.numbers import SCALE_WORDS, CanonicalUnit, canonical_unit

MIXES = ("hi-en", "en", "hi")
DEFAULT_MIX = "hi-en"

# One spoken word per scale token, per mix. Abbreviations and spelling variants are
# normalised ("cr" -> "crore", "lac" -> "lakh"); Devanagari words for the "hi" mix.
_SCALE_SPOKEN: dict[str, dict[str, str]] = {
    "hundred": {"hi-en": "sau", "en": "hundred", "hi": "सौ"},
    "thousand": {"hi-en": "hazaar", "en": "thousand", "hi": "हज़ार"},
    "lakh": {"hi-en": "lakh", "en": "lakh", "hi": "लाख"},
    "million": {"hi-en": "million", "en": "million", "hi": "मिलियन"},
    "crore": {"hi-en": "crore", "en": "crore", "hi": "करोड़"},
    "billion": {"hi-en": "billion", "en": "billion", "hi": "बिलियन"},
    "arab": {"hi-en": "arab", "en": "arab", "hi": "अरब"},
    "trillion": {"hi-en": "trillion", "en": "trillion", "hi": "ट्रिलियन"},
    "kharab": {"hi-en": "kharab", "en": "kharab", "hi": "खरब"},
}
_SCALE_ALIASES: dict[str, str] = {
    "sau": "hundred",
    "k": "thousand", "hazaar": "thousand", "hazar": "thousand", "hajar": "thousand",
    "lac": "lakh", "lakhs": "lakh",
    "mn": "million",
    "crores": "crore", "karod": "crore", "karor": "crore", "cr": "crore",
    "bn": "billion",
}
_UNIT_SPOKEN: dict[str, dict[str, str]] = {
    CanonicalUnit.percent.value: {"hi-en": "percent", "en": "percent", "hi": "प्रतिशत"},
    CanonicalUnit.percentage_points.value: {
        "hi-en": "percentage points", "en": "percentage points", "hi": "प्रतिशत अंक",
    },
    CanonicalUnit.inr.value: {"hi-en": "rupaye", "en": "rupees", "hi": "रुपये"},
    CanonicalUnit.usd.value: {"hi-en": "dollar", "en": "dollars", "hi": "डॉलर"},
}
_YEAR_WORD = {"hi-en": "saal", "en": "", "hi": "साल"}


def _mix(mix: str | None) -> str:
    return mix if mix in MIXES else DEFAULT_MIX


def format_number(x: float) -> str:
    """Digits as a voice would read them: no thousands separators, at most two decimals."""
    if abs(x - round(x)) < 1e-9:
        return str(round(x))
    return f"{x:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Scale:
    """A scale word and its multiplier (factor 1 and an empty word mean "no scale word")."""

    factor: float = 1.0
    word: str = ""  # canonical key in _SCALE_SPOKEN, or ""


def _scale_of(unit: str | None) -> Scale:
    """The scale word written in a unit string ("lakh visitors" -> lakh, 1e5)."""
    if not unit:
        return Scale()
    factor, word = 1.0, ""
    for token in unit.lower().split():
        if token in SCALE_WORDS:
            factor *= SCALE_WORDS[token]
            word = _SCALE_ALIASES.get(token, token)
    return Scale(factor, word if word in _SCALE_SPOKEN else "")


def _auto_scale(absolute: float) -> Scale:
    """Indian scale for large numbers that carry no scale word of their own."""
    if abs(absolute) >= 1e7:
        return Scale(1e7, "crore")
    if abs(absolute) >= 1e5:
        return Scale(1e5, "lakh")
    return Scale()


def _noun(unit: str | None, base: str, mix: str) -> str:
    if base in _UNIT_SPOKEN:
        return _UNIT_SPOKEN[base][mix]
    if base in (CanonicalUnit.count.value, CanonicalUnit.year.value) or not unit:
        return ""
    # A free-form noun: the card's own words minus the scale word ("lakh visitors" -> visitors).
    words = [t for t in unit.replace("_", " ").split() if t.lower() not in SCALE_WORDS]
    return " ".join(words)


@dataclass(frozen=True)
class SpokenFigure:
    """A figure split into its core ("15 lakh", "12 percent", "saal 2019") and an optional
    trailing noun ("visitors"). The guard looks for the core; the line speaks both."""

    core: str
    noun: str = ""

    @property
    def text(self) -> str:
        return f"{self.core} {self.noun}".strip()


def spoken_parts(
    value: float,
    unit: str | None,
    *,
    like: str | None = None,
    is_date: bool = False,
    mix: str | None = DEFAULT_MIX,
) -> SpokenFigure:
    """Render `value` (given in `unit`) in the scale and unit words of `like` (the card's
    unit). Without `like`, the figure is rendered in its own unit."""
    mix = _mix(mix)
    if is_date:
        year = format_number(value)
        return SpokenFigure(f"{_YEAR_WORD[mix]} {year}".strip())

    scale_in, base_in = canonical_unit(unit)
    absolute = value * scale_in
    target_unit = like if like is not None else unit
    _, base_t = canonical_unit(target_unit)
    if like is not None and base_t != base_in:
        # Different base units cannot share a scale (the numeric gate never challenges
        # these); speak the figure in its own unit instead.
        target_unit, base_t = unit, base_in
    scale = _scale_of(target_unit)
    if not scale.word:
        scale = _auto_scale(absolute)
    shown = absolute / scale.factor
    if scale.factor > 1 and 0 < abs(shown) < 0.01:
        scale = _auto_scale(absolute)  # e.g. 500 rupees against a card in crore
        shown = absolute / scale.factor
    number = format_number(shown)
    scale_word = _SCALE_SPOKEN[scale.word][mix] if scale.word else ""
    noun = _noun(target_unit, base_t, mix)
    if base_t in (CanonicalUnit.percent.value, CanonicalUnit.percentage_points.value):
        return SpokenFigure(f"{number} {noun}".strip())
    return SpokenFigure(f"{number} {scale_word}".strip(), noun)


def spoken_figure(
    value: float,
    unit: str | None,
    *,
    like: str | None = None,
    is_date: bool = False,
    mix: str | None = DEFAULT_MIX,
) -> str:
    """The figure as the board says it, e.g. spoken_figure(1_500_000, "visitors",
    like="lakh visitors") == "15 lakh visitors"."""
    return spoken_parts(value, unit, like=like, is_date=is_date, mix=mix).text


# ---------------------------------------------------------------------------
# The fact-challenge sentence
# ---------------------------------------------------------------------------

# Spoken source name when a synthetic card carries no `source_short`.
SYNTHETIC_SOURCE = {"hi-en": "hamare test data", "en": "our test data", "hi": "हमारे टेस्ट डेटा"}
# Spoken source name when a real card has no `source_short` (never the long source_title).
GENERIC_SOURCE = {"hi-en": "official data", "en": "official data", "hi": "आधिकारिक आँकड़ों"}


def sentence_case(text: str) -> str:
    """Upper-case the first letter of every sentence; leave the rest alone (source names
    keep their own capitals)."""
    out = []
    for part in re.split(r"(?<=[.?!।])(\s+)", text.strip()):
        if part and not part.isspace():
            part = part[0].upper() + part[1:]
        out.append(part)
    return "".join(out)


def challenge_sentence(
    *,
    claim_figure: str,
    card_figure: str,
    card_year: int | None,
    source_short: str,
    is_date: bool = False,
    mix: str | None = DEFAULT_MIX,
) -> str:
    """The required shape (ruling 11): the candidate's figure, then the card's figure with
    its year and short source, then a question about the candidate's source. Asks, never
    asserts. Sentence case, in the persona's language mix."""
    mix = _mix(mix)
    year = None if is_date or card_year is None else str(card_year)
    if mix == "en":
        said = f"You said {claim_figure}."
        if is_date:
            card = f"According to {source_short}, it was {card_figure}."
        elif year:
            card = f"According to {source_short} for {year}, it is {card_figure}."
        else:
            card = f"According to {source_short}, it is {card_figure}."
        ask = "Where is your figure from?"
    elif mix == "hi":
        said = f"आपने {claim_figure} कहा।"
        when = f" के {year} के आँकड़े" if year else ""
        card = f"{source_short}{when} के हिसाब से यह {card_figure} है।"
        ask = "आपका आँकड़ा कहाँ से है?"
    else:
        said = f"Aapne {claim_figure} kaha."
        when = f" ke {year} ke figure" if year else ""
        card = f"{source_short}{when} ke hisaab se yeh {card_figure} hai."
        ask = "Aapka figure kahan se hai?"
    return sentence_case(f"{said} {card} {ask}")
