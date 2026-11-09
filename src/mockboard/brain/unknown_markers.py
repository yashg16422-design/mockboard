"""Honest "I don't know" phrases, read from raw transcript text. No model code.

Moved here unchanged from `listener.py` (which still uses them through its own name) so that the
director, which must import no model code, can tell an honest answer from a request.
Our own lists; UNVERIFIED against real Prisma output except one Tamil phrase ("சரியாக தெரியாது"),
seen on generated speech on Nov 6, 2026.
"""

from __future__ import annotations

import re

__all__ = ["UNKNOWN_STRONG", "says_dont_know"]

UNKNOWN_STRONG = re.compile(
    r"\b(?:pata nahi[n]?|nahi[n]? pata|maloom nahi[n]?|malum nahi[n]?|nahi[n]? maloom|"
    r"yaad nahi[n]?|nahi[n]? yaad|(?:koi )?idea nahi[n]?|i (?:do not|don'?t) know|"
    r"not sure|no idea)\b"
    r"|पता नहीं|पता नही|नहीं पता|नही पता|मालूम नहीं|नहीं मालूम|याद नहीं|आइडिया नहीं"
    r"|தெரியாது|எனக்குத் தெரியாது|எனக்கு தெரியாது",
    re.IGNORECASE,
)


def says_dont_know(text: str) -> bool:
    """True when the raw text holds a strong honest "I don't know" phrase."""
    import unicodedata

    return bool(UNKNOWN_STRONG.search(unicodedata.normalize("NFC", text)))
