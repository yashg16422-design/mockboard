"""Hinglish words that share a spelling with an English word, rewritten for Timbre only.

Found by the owner on Nov 6 (spoken second attempt, voice Poorvi, `timbre-v2.5`, language `auto`):
"aur aap use kaise sambhalenge" was read with the English "use" (yooz), not उसे ("it"). Romanised
Hinglish has a handful of such words: use, is, main, do, the, hi. The board's caption keeps the
Latin wording; only the text sent to Timbre changes, and only when a neighbouring word shows the
Hindi sense. Anything doubtful is left as written, because "technology ka use" is English "use".

The word lists are ours. UNVERIFIED: that Timbre `auto` reads a Devanagari word inside a Latin
line the Hindi way; the owner's ear decides, as with the voices (Ruling 39).
"""

from __future__ import annotations

import re

__all__ = ["hinglish_for_tts"]

# Hindi sense when the NEXT word is one of these (whole word, or a stem for verbs).
_NEXT_HINDI = {
    "use": (
        "kaise", "kya", "kyun", "kyon", "kab", "kahan", "aap", "hum", "ham", "koi", "sab", "phir",
        "door", "dur", "theek", "thik", "hal", "aage", "kis", "kaun", "bhi", "pehle",
    ),
    "is": (
        "baat", "par", "pe", "mudde", "mamle", "vishay", "sawal", "sawaal", "prashn", "samay",
        "tarah", "liye", "kshetra", "sthiti", "soch", "faisle", "kaam", "disha", "baare", "cheez",
        "yojana", "niti", "samasya", "chunauti", "desh", "zile", "gaon", "shehar", "paksh",
        "badlav", "vichar", "jawab", "uttar", "pad", "naukri", "seva", "anubhav",
    ),
    "main": (
        "aap", "aapse", "aapko", "yeh", "ye", "ise", "use", "bhi", "kya", "kaise", "pehle", "kuch",
        "ek", "apne", "apni", "isse", "nahi", "nahin",
    ),
    "do": (
        "pehlu", "baatein", "taraf", "paksh", "saal", "kaaran", "karan", "udaharan", "sawal",
        "vikalp", "raaste", "logon",
    ),
}
_NEXT_HINDI_STEMS = {
    "use": ("sambhal", "samajh", "samjh", "bata", "suljha", "rok", "sudhar", "badal", "dekh",
            "sun", "maan", "chun", "kaise"),
    "main": ("samajh", "samjh", "soch", "maan", "chah", "kah", "keh", "pooch", "puch", "jaan"),
}
# Hindi sense when the PREVIOUS word is one of these ("kar rahe the", "aap hi").
_PREV_HINDI = {
    "the": ("rahe", "rahi", "gaye", "gayi", "hue", "huye", "kehte", "sochte", "karte", "chahte",
            "the", "dete", "lete", "jaate", "aate"),
    "hi": ("aap", "yahi", "wahi", "ek", "sirf", "abhi", "tabhi", "kabhi", "isi", "usi", "yeh",
           "woh", "main", "hum"),
}
# English sense when the PREVIOUS word is one of these ("AI ka use kaise", "the main point").
_PREV_ENGLISH = {
    "use": ("ka", "ki", "ke", "ko", "iska", "uska", "sahi", "galat", "the", "a", "to", "can",
            "will", "we", "i", "you", "they", "its", "my", "our", "proper", "zyada", "kam"),
    "main": ("the", "a", "our", "my", "its", "their"),
    "is": ("it", "this", "that", "there", "what", "which", "he", "she"),
}
DEVANAGARI = {"use": "उसे", "is": "इस", "main": "मैं", "do": "दो", "the": "थे", "hi": "ही"}

_WORD = re.compile(r"[A-Za-z']+|[^A-Za-z']+")


def _hindi_sense(word: str, prev: str, nxt: str) -> bool:
    if prev in _PREV_ENGLISH.get(word, ()):
        return False
    if word in _PREV_HINDI:
        return prev in _PREV_HINDI[word]
    if nxt in _NEXT_HINDI.get(word, ()):
        return True
    return any(nxt.startswith(stem) for stem in _NEXT_HINDI_STEMS.get(word, ()))


def hinglish_for_tts(text: str) -> str:
    """The line as Timbre should read it. Pure; changes only the words listed above."""
    parts = _WORD.findall(text)
    words = [i for i, p in enumerate(parts) if p[0].isalpha() or p[0] == "'"]
    out = list(parts)
    for n, i in enumerate(words):
        word = parts[i]
        low = word.lower()
        if low not in DEVANAGARI:
            continue
        if word[0].isupper() and n > 0:  # "Is" mid-sentence is likely a name or English
            continue
        prev = parts[words[n - 1]].lower() if n > 0 else ""
        nxt = parts[words[n + 1]].lower() if n + 1 < len(words) else ""
        between_prev = "".join(parts[words[n - 1] + 1 : i]) if n > 0 else ""
        between_next = "".join(parts[i + 1 : words[n + 1]]) if n + 1 < len(words) else ""
        # Only inside one clause: punctuation between the words breaks the context.
        prev = prev if not re.search(r"[.,;:!?।]", between_prev) else ""
        nxt = nxt if not re.search(r"[.,;:!?।]", between_next) else ""
        if _hindi_sense(low, prev, nxt):
            out[i] = DEVANAGARI[low]
    return "".join(out)
