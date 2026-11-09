"""A turn that is only a greeting ("hello sir", "हेलो सर", "Good morning, ma'am").

The owner's live sitting (Nov 9): the board opened with its question, the candidate said "hello
sir", and the board took that as a thin answer and pressed ("Aapne kaha 'hello sir'..."). A real
board greets back and asks again. The director now answers such a turn with one fixed greeting and
the same question (`D-GREET`); it costs no answer. Additive, like `think_time`: the frozen request
detector (Ruling 57) is not touched and goes first.

A turn counts only when every word is a greeting or a courtesy word ("sir", "ji", "everyone") and
at least one is a greeting. Anything else ("Good morning sir, main Lucknow se hoon") is an answer.
The word lists are ours and unmeasured on real speech. UNVERIFIED: Tamil wording.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["MAX_WORDS", "is_greeting_only"]

MAX_WORDS = 8

_GREETINGS = (
    "hello", "helo", "hallo", "hi", "hey", "namaste", "namastey", "namaskar", "namaskaar",
    "pranam", "pranaam", "vanakkam", "good morning", "good afternoon", "good evening",
    "good day", "नमस्ते", "नमस्कार", "प्रणाम", "हेलो", "हैलो", "हलो", "हाय", "गुड मॉर्निंग",
    "गुड मोर्निंग", "गुड आफ्टरनून", "गुड इवनिंग", "வணக்கம்", "ஹலோ", "குட் மார்னிங்",
)  # fmt: skip
_COURTESY = (
    "sir", "sirji", "ma'am", "maam", "mam", "madam", "ji", "jee", "everyone", "all", "to",
    "sabko", "aap", "sabhi", "and", "aur", "respected", "members", "board", "the", "dear",
    "सर", "मैम", "मैडम", "जी", "सबको", "आप", "सभी", "और", "சார்", "மேடம்", "எல்லோருக்கும்",
)  # fmt: skip


def _phrases(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_GREETING_RE = re.compile(rf"(?<!\w)(?:{_phrases(_GREETINGS)})(?!\w)", re.IGNORECASE)
_COURTESY_RE = re.compile(rf"(?<!\w)(?:{_phrases(_COURTESY)})(?!\w)", re.IGNORECASE)


def is_greeting_only(text: str) -> bool:
    """True for a short turn made of a greeting and courtesy words only. Pure."""
    text = unicodedata.normalize("NFC", text or "").strip()
    words = [w for w in re.split(r"\s+", text) if re.search(r"\w", w)]
    if not words or len(words) > MAX_WORDS or _GREETING_RE.search(text) is None:
        return False
    rest = _COURTESY_RE.sub(" ", _GREETING_RE.sub(" ", text))
    return re.search(r"\w", rest) is None
