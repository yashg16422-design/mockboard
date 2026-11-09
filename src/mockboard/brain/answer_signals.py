"""What code can tell about a spoken answer, for the board's candour (owner, Nov 9: "if the student
feels under-confident or gives a less satisfactory answer, the board should be blunt").

`unsure` (hedging words), `thin` (under fifteen words), `no_reason` (no "because"). Read by the
panel actor (actor.v12), the director (a hedge at the end of a segment) and the MCP guidance.
Word lists are ours and unmeasured on real speech.
"""

from __future__ import annotations

import re

__all__ = ["THIN_ANSWER_WORDS", "answer_signals"]

_HEDGES = re.compile(
    r"\b(?:shayad|shaayad|lagta hai|mujhe lagta|pata nahi|pakka nahi|pakka pata nahi|ho sakta hai|"
    r"i think|i guess|maybe|perhaps|probably|not sure|i am not sure|kind of|sort of)\b"
    r"|शायद|लगता है|पता नहीं|पक्का नहीं|हो सकता है",
    re.IGNORECASE,
)
_REASONS = re.compile(
    r"\b(?:kyunki|kyonki|kyuki|isliye|islie|taaki|because|since|so that|therefore|hence|"
    r"isse|iski wajah)\b|क्योंकि|इसलिए|ताकि|इसकी वजह",
    re.IGNORECASE,
)
THIN_ANSWER_WORDS = 15


def answer_signals(answer: str) -> list[str]:
    """`unsure`, `thin` and `no_reason` for a spoken answer; empty for silence. Pure."""
    words = len(answer.split())
    if words == 0:
        return []
    out = []
    if _HEDGES.search(answer):
        out.append("unsure")
    if words < THIN_ANSWER_WORDS:
        out.append("thin")
    if words >= 6 and not _REASONS.search(answer):
        out.append("no_reason")
    return out
