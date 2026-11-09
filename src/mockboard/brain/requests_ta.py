"""Tamil-script and romanised Tanglish candidate requests: repeat, clarify, skip, topic, end.

Why this exists: a Tanglish sitting routes Prisma with `ta-IN`, so a request often arrives in
Tamil script ("வேறு தலைப்புக்கு போகலாமா?"). The frozen rule detector (`requests.py`, Ruling 57)
has no Tamil patterns, and the director accepts `end` only on a rule hit. Without this layer a
Tanglish candidate could not end the interview by voice, and every other request depended on
the model's vote alone, while the form promised a phrase that works.

This module is additive. `requests.py` is untouched; the director consults this layer only when
the frozen detector finds nothing. It returns the same `RequestHit`, so the rules-versus-listener
table in `director._resolve_request` is unchanged.

The phrase families are ours and unmeasured. UNVERIFIED: a fluent Tamil speaker has not
reviewed them, and Prisma's `ta-IN` spelling of spoken requests has not been seen on real
speech. They are deliberately narrow: a missed request falls back to the listener's vote, while
a false hit would interrupt a real answer.
"""

from __future__ import annotations

import re

from mockboard.brain.requests import (
    END_MAX_WORDS,
    SHORT_ANSWER_WORDS,
    TAIL_WORDS,
    ChangeStrength,
    RequestHit,
    RequestKind,
    _tokenise,
)

__all__ = ["detect_tamil_request"]

# A token is a run of non-space characters; `\S*` lets Tamil suffixes attach to a stem.
_T = r"\S*"

# Tokens right after a match that turn the request into something else: a negation
# ("don't repeat") or a quotative ("... he said"). Romanised spellings included.
_BLOCK_AFTER = re.compile(
    r"^(?:வேண்டாம்|வேணாம்|இல்லை|இல்ல|கூடாது|என்று|என்றார்|என்றேன்|னு|ன்னு|"
    r"vendam|venam|illai|illa|koodathu|endru|nu|nnu)$"
)
# A verb that already carries the negative ("சொல்லாதீர்கள்", "don't say").
_NEGATIVE_VERB = re.compile(r"(?:ாதீர்கள்|ாதீங்க|ாதே|aatheenga|aatheergal)$")

# (pattern id, kind, regex over the token text, strength for change_topic)
_PATTERNS: tuple[tuple[str, RequestKind, re.Pattern[str], ChangeStrength | None], ...] = (
    (
        "ta_end_interview",
        RequestKind.END,
        re.compile(
            rf"(?:நேர்காணல{_T}|இன்டர்வியூ{_T}|இண்டர்வியூ{_T}|interview{_T}|nerkaanal{_T}|"
            rf"nerkanal{_T})(?: \S+){{0,2}} "
            rf"(?:முடிக்க{_T}|முடித்து{_T}|முடிச்சு{_T}|நிறுத்த{_T}|நிறுத்து{_T}|"
            rf"mudikka{_T}|mudichu{_T}|mudithu{_T}|nirutha{_T}|niruthu{_T})"
        ),
        None,
    ),
    (
        "ta_end_here",
        RequestKind.END,
        re.compile(
            rf"(?:இத்துடன்|இதோடு|இதோட|இங்கேயே|இப்போதே|ithudan|ithoda|ingeye)(?: \S+){{0,1}} "
            rf"(?:முடிக்க{_T}|முடித்து{_T}|முடிச்சு{_T}|நிறுத்த{_T}|நிறுத்து{_T}|"
            rf"mudikka{_T}|mudichu{_T}|nirutha{_T}|niruthu{_T})"
        ),
        None,
    ),
    (
        "ta_change_topic_verb",
        RequestKind.CHANGE_TOPIC,
        re.compile(
            # "வேர்" is how Prisma ta-IN wrote a spoken "வேறு" on Nov 6 (generated speech); it means
            # "root", so it is accepted only before a topic word and a verb of moving or talking.
            rf"(?:வேறு|வேற|வேறொரு|மற்ற|வேர்|veru|vera|vere)\s"
            rf"(?:தலைப்பு{_T}|topic{_T}|டாபிக்{_T}|டாப்பிக்{_T}|விஷய{_T}|விடய{_T}|"
            rf"thalaippu{_T}|thalaipu{_T}|vishay{_T})(?: \S+){{0,1}} "
            rf"(?:போக{_T}|போவோம்|பேச{_T}|பேசுவோம்|மாற{_T}|செல்ல{_T}|செல்வோம்|வர{_T}|"
            rf"pog{_T}|pes{_T}|maar{_T}|maath{_T}|sell{_T})"
        ),
        ChangeStrength.STRONG,
    ),
    (
        "ta_change_topic_swap",
        RequestKind.CHANGE_TOPIC,
        re.compile(
            rf"(?:தலைப்பை|தலைப்ப|டாபிக்கை|டாபிக்க|topic{_T}|thalaippai|thalaippa)\s"
            rf"(?:மாற்ற{_T}|மாத்த{_T}|மாற்று{_T}|மாத்து{_T}|maatha{_T}|maathu{_T}|"
            rf"maatra{_T}|matha{_T})"
        ),
        ChangeStrength.STRONG,
    ),
    (
        "ta_skip_next",
        RequestKind.SKIP,
        re.compile(
            rf"(?:அடுத்த|adutha|aduththa)\s(?:கேள்வி{_T}|question{_T}|kelvi{_T}|kaelvi{_T})"
        ),
        None,
    ),
    (
        "ta_skip_this",
        RequestKind.SKIP,
        re.compile(
            rf"(?:இந்த{_T}|indha{_T}|intha{_T})\s(?:கேள்வி{_T}|question{_T}|kelvi{_T})\s"
            rf"(?:விட்டு{_T}|விடலாம்|விட்டுடலாம்|வேண்டாம்|வேணாம்|skip{_T}|vittu{_T}|vidalam|"
            rf"vendam|venam)"
        ),
        None,
    ),
    (
        "ta_repeat",
        RequestKind.REPEAT,
        re.compile(
            rf"(?:மீண்டும்|மறுபடியும்|மறுபடி|திரும்ப|திருப்பி|இன்னொரு\sமுறை|இன்னொருமுறை|"
            rf"meendum|marupadiyum|marupadi|thirumba|thiruppi|innoru\smurai)(?: \S+){{0,1}} "
            rf"(?:சொல்{_T}|கேள{_T}|கேட்{_T}|soll{_T}|kel{_T}|ket{_T})"
        ),
        None,
    ),
    (
        "ta_repeat_english_verb",
        RequestKind.REPEAT,
        re.compile(rf"(?:ரிப்பீட்|repeat)\s(?:பண்ண{_T}|பண்ணு{_T}|pann{_T})"),
        None,
    ),
    (
        "ta_clarify_question",
        RequestKind.CLARIFY_QUESTION,
        re.compile(
            rf"(?:கேள்வி{_T}|question{_T}|kelvi{_T})(?: \S+){{0,1}} "
            rf"(?:புரிய(?:வில்லை|ல|லை|வில்ல){_T}|puriya(?:la|lai|villai){_T})"
        ),
        None,
    ),
    (
        "ta_clarify_explain",
        RequestKind.CLARIFY_QUESTION,
        re.compile(
            rf"(?:விளக்க{_T}|vilakka{_T})\s(?:முடியுமா|சொல்ல{_T}|முடியும்|mudiyuma|solla{_T})"
        ),
        None,
    ),
)
# "எனக்குப் புரியவில்லை" alone is a request to clarify; inside a long answer it is about a policy.
_SHORT_PURIYALA = re.compile(r"^(?:புரிய(?:வில்லை|ல|லை|வில்ல)|puriya(?:la|lai|villai))\S*$")
_SHORT_PURIYALA_MAX = 4


def _token_index(text: str, offset: int) -> int:
    return text.count(" ", 0, offset)


def _blocked_after(tokens: tuple[str, ...], end: int) -> bool:
    following = tokens[end : end + 2]
    return any(_BLOCK_AFTER.match(tok) for tok in following)


def _position_ok(start: int, total: int) -> bool:
    return total <= SHORT_ANSWER_WORDS or start >= total - TAIL_WORDS


def detect_tamil_request(text: str) -> RequestHit | None:
    """The candidate's request in Tamil script or romanised Tanglish, or None.

    Same contract as `requests.detect_request`: short turns or the tail of a long one, an `end`
    only within END_MAX_WORDS, no hit when a negation or a quotative follows the phrase.
    """
    tokens = _tokenise(text)
    words = tokens.raw
    total = len(words)
    if total == 0:
        return None
    joined = " ".join(words)
    for pattern_id, kind, pattern, strength in _PATTERNS:
        if kind is RequestKind.END and total > END_MAX_WORDS:
            continue
        for m in pattern.finditer(joined):
            if m.start() and joined[m.start() - 1] != " ":
                continue  # the match must begin on a token boundary
            start = _token_index(joined, m.start())
            end = _token_index(joined, m.end()) + 1
            if not _position_ok(start, total):
                continue
            if _blocked_after(words, end) or _NEGATIVE_VERB.search(words[end - 1]):
                continue
            return RequestHit(kind, (), pattern_id, strength)
    short = total <= _SHORT_PURIYALA_MAX and any(_SHORT_PURIYALA.match(w) for w in words)
    if short and not _blocked_after(words, total):
        return RequestHit(RequestKind.CLARIFY_QUESTION, (), "ta_clarify_short")
    return None
