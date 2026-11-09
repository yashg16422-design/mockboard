"""Over-familiar or abusive address in a UPSC interview. Code decides, not the model.

A hit is one courteous sentence from the member who heard it. The question still comes.
Ordinary Hinglish is not a hit: "tum" about other people, a story about "bhai", district
talk. Only a closed list, matched as whole words.
"""

from __future__ import annotations

import re

__all__ = ["register_hit"]

# Whole words only. "yaari" is not "yaar". "mc" and "bc" are the clipped abuses, not initials
# inside a longer word. "bhai" and "tum" are left out: both are normal in an answer about people.
_LATIN = re.compile(
    r"\b(?:bro|bruh|dude|yaar|yaarr|yrr|wtf|fuck|fucking|shit|bullshit|"
    r"bc|mc|chutiya|chutia|madarchod|behenchod|bhosdike|bhosdi)\b",
    re.IGNORECASE,
)
_DEVANAGARI = ("यार", "चुतिया", "मादरचोद", "बहनचोद")


def register_hit(text: str) -> str | None:
    """The matched token, or None. Empty and ordinary answers are None."""
    if not text or not text.strip():
        return None
    found = _LATIN.search(text)
    if found:
        return found.group(0).casefold()
    for word in _DEVANAGARI:
        if word in text:
            return word
    return None
