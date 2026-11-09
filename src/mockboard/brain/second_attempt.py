"""Upgrade #3: a second attempt at the weakest question, compared with the first answer.

The comparison is deliberately simple and observable: did the answer give a reason, name who is
affected, state a first step, weigh another side, name a cost or limit, and how long was it. It
never scores the candidate and never judges whether the content is right. It runs in code, with
no model call, so it is free, instant and the same every time.

The cue words are ours, in English, romanised Hindi, Devanagari and Tamil script. They are
UNVERIFIED on real transcripts: a phrase they miss shows as "not heard", which the page says
plainly, and the transcript is always shown so the candidate can judge for themself.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["CHECKS", "AttemptCompare", "compare_attempts", "features"]

MAX_ANSWER_CHARS = 4000

# check id -> (plain label, cue words). Latin cues match whole words; Indic cues match inside a
# word, because Tamil and Hindi attach endings ("முதலில்", "लोगों"). A Tamil stem drops its final
# virama ("மக்கள" matches "மக்கள்" and "மக்களிடம்").
CHECKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "reason": ("gave a reason", (
        "because", "since", "so that", "therefore", "kyunki", "kyonki", "kyuki", "isliye",
        "isiliye", "kyunke", "क्योंकि", "इसलिए", "चूँकि", "ஏனென்றால்", "ஏனெனில்", "அதனால்",
        "காரணம",
    )),
    "affected": ("named who is affected", (
        "people", "farmers", "women", "children", "villagers", "citizens", "families", "workers",
        "students", "log", "logon", "logo", "kisan", "kisaan", "mahila", "mahilaon", "bachche",
        "parivar", "mazdoor", "लोग", "लोगों", "किसान", "महिला", "बच्चे", "परिवार", "मज़दूर",
        "மக்கள", "விவசாயி", "பெண்கள", "குழந்தை", "குடும்ப", "தொழிலாளர",
    )),
    "first_step": ("said what you would do first", (
        "first", "firstly", "to begin", "to start", "pehle", "sabse pehle", "shuruaat",
        "pahla kadam", "pehla qadam", "pehla kadam", "पहले", "सबसे पहले", "शुरुआत", "पहला कदम",
        "முதலில்", "முதல் படி", "தொடக்கமாக",
    )),
    "other_side": ("weighed another side", (
        "however", "on the other hand", "others argue", "critics", "but", "although", "lekin",
        "magar", "parantu", "doosri taraf", "doosra paksh", "दूसरी ओर", "दूसरा पक्ष", "लेकिन",
        "मगर", "परंतु", "ஆனால்", "மறுபக்க", "மறுபுற", "இருப்பினும்",
    )),
    "cost": ("named a cost or a limit", (
        "cost", "trade-off", "tradeoff", "risk", "downside", "limit", "nuksaan", "nuksan",
        "keemat", "kimat", "kharcha", "jokhim", "नुकसान", "कीमत", "खर्च", "जोखिम", "செலவு",
        "இழப்பு", "ஆபத்து", "வரம்பு",
    )),
}


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _has(text: str, cue: str) -> bool:
    if cue.isascii():
        return re.search(rf"(?<![a-z]){re.escape(cue)}(?![a-z])", text) is not None
    return cue in text


def features(answer: str) -> dict[str, Any]:
    """The observable features of one answer. Pure."""
    text = _norm(answer[:MAX_ANSWER_CHARS])
    found = {key: any(_has(text, cue) for cue in cues) for key, (_, cues) in CHECKS.items()}
    return {"words": len(text.split()), **found}


@dataclass
class AttemptCompare:
    before_words: int
    after_words: int
    rows: list[dict[str, Any]] = field(default_factory=list)  # {check, label, before, after}
    added: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    still_missing: list[str] = field(default_factory=list)
    summary: str = ""
    note: str = (
        "Simple checks for the parts of a good answer, not a mark. They look for words such as "
        "\"because\" or \"first\", so read your own words above as well."
    )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def compare_attempts(before: str, after: str) -> AttemptCompare:
    """Compare the first answer with the second attempt. Pure; no model call."""
    one, two = features(before), features(after)
    result = AttemptCompare(before_words=one["words"], after_words=two["words"])
    for key, (label, _) in CHECKS.items():
        result.rows.append({"check": key, "label": label, "before": one[key], "after": two[key]})
        if two[key] and not one[key]:
            result.added.append(label)
        elif two[key] and one[key]:
            result.kept.append(label)
        elif one[key] and not two[key]:
            result.dropped.append(label)
        else:
            result.still_missing.append(label)
    if not two["words"]:
        result.summary = "No second answer was heard, so there is nothing to compare yet."
    elif result.added:
        result.summary = "This time you " + _join(result.added) + "."
    elif result.kept:
        result.summary = "You kept the same parts as before (" + _join(result.kept) + ")."
    else:
        result.summary = "Neither answer had these parts yet."
    if two["words"] and result.still_missing:
        result.summary += " Next time, also try: " + _join(result.still_missing[:2]) + "."
    return result


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
