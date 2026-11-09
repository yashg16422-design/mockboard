"""A request for a moment to think ("ek minute sochne dijiye"), in every language the board hears.

Owner's request (Nov 7): a candidate must be able to think and then speak at length, as in a
real board, where "May I take a moment, sir?" is normal and costs nothing. Before this, the
phrase was taken as the whole answer and the board moved on.

The board answers with one fixed line ("Zaroor, aaram se sochiye") and keeps the same question;
the next turn is a long one, so pauses while the candidate gathers their thoughts do not end it
(`hooks.expects_long_turn`). Additive, like `requests_ta`: the frozen request detector
(Ruling 57) is not touched and goes first.

Only short turns count (at most `MAX_WORDS` words), a negation anywhere blocks it ("sochne ki
zaroorat nahi"), and a time phrase that is part of a sentence ("ek minute mein ho jaata hai") does
not count. The phrase lists are ours and unmeasured on real speech. UNVERIFIED: Tamil wording.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["MAX_WORDS", "is_think_request"]

MAX_WORDS = 10

# "ek minute" counts unless it is part of a sentence: "ek minute mein", "ek minute ka kaam".
_NOT_A_PAUSE = r"(?!\s*(?:mein|me|mai|men|में|ka|ki|ke|का|की|के|se|से|tak|तक|lagta|लगता))"
_PATTERNS = [
    # romanised Hindi / Hinglish
    rf"\b(?:ek|1)\s*(?:minute|minat|min|second|sec|pal|moment)\b{_NOT_A_PAUSE}",
    r"\bsoch(?:ne)?\s*(?:dijiye|dijie|dijiyega|do|dein|den|de)\b",
    r"\bsochne\s*(?:ka|ki)\s*(?:samay|time|mauka|waqt)\b",
    r"\bsoch\s*(?:ke|kar|kr)\s*(?:batata|batati|batau|bataun|bata\w*|bolta|bolti|bolu)\b",
    r"\bthoda\s*(?:samay|time|waqt|sa\s+samay)\b",
    r"\b(?:samay|time|waqt)\s*(?:dijiye|chahiye|de\s+dijiye)\b",
    # English
    r"\blet\s+me\s+(?:think|gather|collect|consider)\b",
    r"\bgive\s+me\s+(?:a|one)\s+(?:moment|minute|second|sec)\b",
    r"\b(?:can|could|may)\s+i\s+(?:take|have|get)\s+a\s+(?:moment|minute|second|little time)\b",
    r"\b(?:just|one|a)\s+(?:moment|minute|second|sec)\b(?:,?\s*please)?",
    r"\bi\s+(?:need|would like)\s+(?:a|some)\s+(?:moment|minute|time)\b",
    # Devanagari
    rf"(?:एक|१)\s*(?:मिनट|सेकंड|सेकेंड|पल|क्षण){_NOT_A_PAUSE}",
    r"सोचने\s*(?:दीजिए|दीजिये|दें|दो)",
    r"सोचने\s*(?:का|की)\s*(?:समय|मौका|वक़्त|वक्त)",
    r"सोच\s*(?:कर|के)\s*(?:बताता|बताती|बताऊं|बताऊँ|बोलता|बोलती)",
    r"थोड़ा\s*(?:समय|वक़्त|वक्त)",
    r"समय\s*(?:दीजिए|दीजिये|चाहिए)",
    # Tamil script and romanised Tanglish
    r"ஒரு\s*(?:நிமிடம்|நிமிஷம்|நொடி)",
    r"யோசி(?:க்க|த்து|ச்சு)",
    r"கொஞ்சம்\s*(?:நேரம்|டைம்)",
    r"நேரம்\s*(?:கொடுங்கள்|கொடுங்க|வேண்டும்)",
    r"\boru\s*(?:nimisham|nimidam|nodi)\b",
    r"\byosi(?:kka|chu|ththu|ttu)\b",
    r"\bkonjam\s*(?:neram|time)\b",
]
_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)
_NEGATION = re.compile(
    r"(?<![a-z])(?:not|no|never|don't|dont|nahi|nahin|na|mat)(?![a-z])"
    r"|नहीं|नही|मत|இல்லை|வேண்டாம்|வேணாம்",
    re.IGNORECASE,
)


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if re.search(r"\w", w)])


def is_think_request(text: str) -> bool:
    """True for a short turn that asks for a moment to think. Pure."""
    text = unicodedata.normalize("NFC", text or "").strip()
    if not text or _words(text) > MAX_WORDS:
        return False
    if _NEGATION.search(text):
        return False
    return _RE.search(text) is not None
