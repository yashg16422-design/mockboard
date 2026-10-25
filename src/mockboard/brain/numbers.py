"""Deterministic number handling: Indian scale words, Hinglish number words, unit
canonicalisation, and the numeric check that acts as the second vote on a contradiction.

Nothing here calls a model. The listener (Nova) normalises claims to `value`/`unit`/`year`,
and this module re-checks them with plain arithmetic, so "the model proposes, code confirms".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

SCALE_WORDS: dict[str, float] = {
    "hundred": 1e2,
    "sau": 1e2,
    "thousand": 1e3,
    "hazaar": 1e3,
    "hazar": 1e3,
    "hajar": 1e3,
    "k": 1e3,
    "lakh": 1e5,
    "lac": 1e5,
    "lakhs": 1e5,
    "million": 1e6,
    "mn": 1e6,
    "crore": 1e7,
    "crores": 1e7,
    "karod": 1e7,
    "karor": 1e7,
    "cr": 1e7,
    "billion": 1e9,
    "bn": 1e9,
    "arab": 1e9,
    "trillion": 1e12,
    "kharab": 1e11,
}

# Small Hindi/Hinglish and English number words. Romanised spellings vary a lot, so this
# list is intentionally conservative; the listener prompt asks Nova to output digits, and
# this table is the deterministic fallback used by tests and the offline demo.
NUMBER_WORDS: dict[str, float] = {
    # English
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    # Hindi (romanised). "do" (2) and "saath" (60) are left out on purpose: they collide
    # with the English verb "do" and the Hindi postposition "ke saath" (with).
    "shunya": 0, "ek": 1, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
    "panch": 5, "chhe": 6, "chhah": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "baarah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20, "pachees": 25,
    "tees": 30, "chalees": 40, "chaalees": 40, "pachaas": 50, "pachas": 50,
    "sattar": 70, "assi": 80, "nabbe": 90,
}

# Fractional prefixes used in Hindi counting: "dedh crore" = 1.5 crore, "saade teen" = 3.5.
FRACTION_WORDS: dict[str, float] = {"dedh": 1.5, "dhai": 2.5, "dhaai": 2.5, "aadha": 0.5}
MODIFIER_WORDS: dict[str, float] = {"saade": 0.5, "sadhe": 0.5, "sava": 0.25, "paune": -0.25}

PERCENT_WORDS = {"%", "percent", "per cent", "pct", "pratishat", "prateeshat", "fisdi",
                 "feesdi", "pratishat.", "फीसदी", "प्रतिशत"}
PP_WORDS = {"pp", "percentage point", "percentage points", "percent point", "percent points"}
INR_WORDS = {"rs", "rs.", "inr", "rupee", "rupees", "rupaye", "rupay", "₹"}
USD_WORDS = {"usd", "$", "dollar", "dollars", "us$"}
YEAR_WORDS = {"year", "years", "saal", "varsh", "ad", "ce"}


class CanonicalUnit(StrEnum):
    percent = "percent"
    percentage_points = "percentage_points"
    inr = "inr"
    usd = "usd"
    year = "year"
    count = "count"


@dataclass(frozen=True)
class Quantity:
    """An absolute number with a canonical base unit (scale words already applied)."""

    value: float
    unit: str  # CanonicalUnit value, or a lower-cased free-form noun such as "visitors"


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def canonical_unit(unit: str | None) -> tuple[float, str]:
    """Split a unit string into (scale multiplier, canonical base unit).

    Examples: "crore rupees" -> (1e7, "inr"); "%" -> (1, "percent");
    "lakh visitors" -> (1e5, "visitor"); None -> (1, "count").
    """
    if unit is None or not unit.strip():
        return 1.0, CanonicalUnit.count.value
    text = unit.strip().lower()
    # Multi-word phrases first.
    for phrase in sorted(PP_WORDS, key=len, reverse=True):
        if phrase in text:
            return 1.0, CanonicalUnit.percentage_points.value
    if text in PERCENT_WORDS or text.endswith("%") or "percent" in text or "pratishat" in text:
        return 1.0, CanonicalUnit.percent.value
    scale = 1.0
    rest: list[str] = []
    for token in re.findall(r"[^\s]+", text.replace("₹", " ₹ ").replace("$", " $ ")):
        if token in SCALE_WORDS:
            scale *= SCALE_WORDS[token]
        else:
            rest.append(token)
    base = " ".join(rest).strip()
    if not base:
        return scale, CanonicalUnit.count.value
    if base in INR_WORDS or any(t in INR_WORDS for t in rest):
        return scale, CanonicalUnit.inr.value
    if base in USD_WORDS or any(t in USD_WORDS for t in rest):
        return scale, CanonicalUnit.usd.value
    if base in YEAR_WORDS:
        return scale, CanonicalUnit.year.value
    return scale, " ".join(_singular(t) for t in rest)


def to_quantity(value: float, unit: str | None) -> Quantity:
    scale, base = canonical_unit(unit)
    return Quantity(value=value * scale, unit=base)


# ---------------------------------------------------------------------------
# Parsing Hinglish number phrases
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^\d{1,3}(,\d{2,3})+(\.\d+)?$|^\d+(\.\d+)?$")


def _token_value(token: str) -> float | None:
    if _NUM_RE.match(token):
        return float(token.replace(",", ""))
    return NUMBER_WORDS.get(token)


def parse_number_phrase(text: str) -> float | None:
    """Parse a short Hinglish/English number phrase into an absolute value.

    Handles digits with Indian or Western grouping ("1,23,456"), scale words
    ("2.5 crore", "dedh lakh"), and Hindi fractional modifiers ("saade teen crore" = 3.5e7).
    Returns None if no number is recognised. Percent signs are ignored here; units are
    handled by `canonical_unit`.
    """
    tokens = re.findall(r"[\w.,]+", text.lower().replace("%", " "))
    tokens = [t.strip(".,") if not _NUM_RE.match(t) else t for t in tokens]
    total = 0.0
    current: float | None = None
    modifier = 0.0
    found = False
    last_scale = math.inf
    for tok in tokens:
        if tok in MODIFIER_WORDS:
            modifier = MODIFIER_WORDS[tok]
            continue
        if tok in FRACTION_WORDS:
            current = (current or 0.0) + FRACTION_WORDS[tok]
            found = True
            continue
        val = _token_value(tok)
        if val is not None:
            current = (current or 0.0) + val + modifier
            modifier = 0.0
            found = True
            continue
        if tok in SCALE_WORDS and found:
            scale = SCALE_WORDS[tok]
            base = current if current is not None else 1.0
            if scale == 100:  # "teen sau" = 300, and keeps accumulating below a thousand
                current = base * scale
            elif scale > last_scale:
                # A bigger scale after a smaller one multiplies everything so far:
                # "5 hazaar crore" = 5,000 crore = 5e10.
                total = (total + (current or 0.0)) * scale
                last_scale = scale
                current = None
            else:
                # A smaller scale adds a new chunk: "2 crore 50 lakh" = 2e7 + 5e6.
                total += base * scale
                last_scale = scale
                current = None
            continue
        if found:
            break  # the number phrase has ended; do not merge numbers from later words
    if not found:
        return None
    return total + (current or 0.0)


# ---------------------------------------------------------------------------
# The numeric check (second vote)
# ---------------------------------------------------------------------------


class NumericResult(StrEnum):
    match = "match"
    mismatch = "mismatch"
    incomparable = "incomparable"


@dataclass(frozen=True)
class Tolerance:
    """How far a figure may be from the card and still count as agreeing.

    `rel` applies to ordinary quantities (5% relative by default). `abs_pp` applies to
    percentages and percentage points, in points (1.0 by default), because a relative
    tolerance on a small percentage is too strict and on a large one too loose.
    `year_abs` applies to date claims.
    """

    rel: float = 0.05
    abs_pp: float = 1.0
    year_abs: int = 0


@dataclass(frozen=True)
class NumericCheck:
    result: NumericResult
    note: str


def numeric_check(
    claim_value: float | None,
    claim_unit: str | None,
    claim_year: int | None,
    card_value: float | None,
    card_unit: str | None,
    card_year: int | None,
    tolerance: Tolerance,
    *,
    is_date: bool = False,
) -> NumericCheck:
    """Compare a claimed figure with a card's figure, deterministically.

    Returns `incomparable` (never `mismatch`) whenever the comparison would be unfair:
    a missing number, different base units, percent versus percentage points, or a
    figure that refers to a different year than the card. Only `mismatch` can confirm a
    contradiction.
    """
    if claim_value is None or card_value is None:
        return NumericCheck(NumericResult.incomparable, "missing value on claim or card")
    if any(math.isnan(v) or math.isinf(v) for v in (claim_value, card_value)):
        return NumericCheck(NumericResult.incomparable, "non-finite value")

    if is_date:
        diff = abs(claim_value - card_value)
        if diff <= tolerance.year_abs:
            return NumericCheck(NumericResult.match, f"year {claim_value:g} matches")
        return NumericCheck(
            NumericResult.mismatch, f"year {claim_value:g} vs card {card_value:g}"
        )

    c = to_quantity(claim_value, claim_unit)
    k = to_quantity(card_value, card_unit)
    pct = {CanonicalUnit.percent.value, CanonicalUnit.percentage_points.value}
    if c.unit != k.unit:
        if {c.unit, k.unit} == pct:
            return NumericCheck(
                NumericResult.incomparable, "percent vs percentage points are different things"
            )
        return NumericCheck(NumericResult.incomparable, f"unit {c.unit!r} vs card {k.unit!r}")

    if claim_year is not None and card_year is not None and claim_year != card_year:
        return NumericCheck(
            NumericResult.incomparable, f"claim year {claim_year} vs card year {card_year}"
        )

    if c.unit in pct:
        diff = abs(c.value - k.value)
        ok = diff <= tolerance.abs_pp
        note = f"{c.value:g} vs card {k.value:g} ({diff:g} points, tolerance {tolerance.abs_pp:g})"
    else:
        denom = max(abs(k.value), 1e-12)
        rel = abs(c.value - k.value) / denom
        ok = rel <= tolerance.rel
        note = f"{c.value:g} vs card {k.value:g} ({rel:.1%} off, tolerance {tolerance.rel:.0%})"
    return NumericCheck(NumericResult.match if ok else NumericResult.mismatch, note)
