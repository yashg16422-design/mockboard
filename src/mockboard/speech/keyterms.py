"""Per-topic speech context for Prisma v3.0 (ruling 27): `keyterms` and `topic`.

The docs (stt-websocket.md, Oct 30, 2026) say both are "Prisma v3.0 only": `keyterms` are up
to 200 words or phrases to favour, `topic` a free-text description fixed per connection;
v3.0 keyterms can change mid-session through `update_settings`. On v2.5 nothing here is
sent (`PrismaSTTService.set_keyterms` is a no-op there).

The lists are ours, hand-written for the panel's segments and built from the session's own
data: the application form (UPSC) or the motion's case-file keywords (debate). No names or
identifiers are ever added (the form has none). Duck-typed on the session, so this module
needs neither Pipecat nor the debate package.
UNVERIFIED: whether keyterms help Hinglish recognition at all; decide on the owner's A/B.
"""

from __future__ import annotations

from typing import Any

from mockboard.brain.affairs import words_heard

# Words a UPSC board and candidate use whatever the topic.
UPSC_COMMON = [
    "UPSC", "IAS", "IPS", "IFS", "civil services", "Collector", "District Magistrate",
    "panchayat", "governance", "policy", "Constitution",
]
# Per agenda segment (the panel's segment ids, `brain/agenda.py`): the vocabulary of what that
# member asks about. Nothing here is a fact; the interview's topics come from the candidate's form.
UPSC_TOPICS: dict[str, list[str]] = {
    "form_hr": ["motivation", "civil services", "graduation", "service", "career"],
    "home_district": ["district", "home state", "local administration", "panchayat", "block"],
    "degree": ["degree subject", "graduation", "project", "technology", "research"],
    "interests": ["hobbies", "optional subject", "sports", "reading", "teamwork"],
    "current_affairs": [
        "current affairs", "District Magistrate", "Superintendent of Police", "law and order",
        "disaster relief", "public service delivery",
    ],
}
UPSC_TOPIC_TEXT = (
    "A UPSC civil services personality test interview in Hinglish, with a panel of five"
)


def speech_context(session: Any) -> tuple[list[str], str]:
    """(keyterms, topic) for the session's current round, at most 200 terms."""
    options = getattr(session, "mode_options", None)
    motion = getattr(options, "motion", None)
    if motion is not None:  # debate: the motion's own vocabulary
        terms: list[str] = []
        case = getattr(motion, "case", None)
        for side in ("for_", "against"):
            for arg in getattr(case, side, []) or []:
                terms.extend(getattr(arg, "keywords", []))
        title = getattr(motion, "title", "")
        if title:
            terms.insert(0, title)
        return _clean(terms), f"A friendly debate in Hinglish on the motion: {motion.text}"
    state = getattr(session, "state", None)
    topic = getattr(state, "topic", "") or ""
    terms = list(UPSC_COMMON) + UPSC_TOPICS.get(topic, [])
    # Prisma v3.0 favours a briefing word only after the candidate has said it, on any
    # member's turn. v2.5 never receives the list. UNVERIFIED: whether the terms help Hinglish.
    terms.extend(words_heard(_candidate_text(session)))
    form = getattr(session, "form", None)
    if form is not None:
        for name in ("home_state", "district", "degree_subject", "optional_subject"):
            value = getattr(form, name, None)
            if value:
                terms.append(str(value))
        terms.extend(getattr(form, "hobbies", []) or [])
    return _clean(terms), UPSC_TOPIC_TEXT


def _candidate_text(session: Any) -> str:
    """What the candidate has said, so Prisma can follow their issue and not the whole paper."""
    hooks = getattr(session, "hooks", None)
    parts: list[str] = []
    raw = getattr(hooks, "_raw", "") if hooks is not None else ""
    if raw:
        parts.append(str(raw))
    director = getattr(hooks, "director", None) if hooks is not None else None
    memory = getattr(director, "memory", None) if director is not None else None
    for note in getattr(memory, "notes", ()) or ():
        gist = getattr(note, "gist", "") or ""
        if gist:
            parts.append(str(gist))
    return " ".join(parts)


def _clean(terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        t = " ".join(str(term).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:200]
