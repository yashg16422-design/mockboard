"""Per-topic speech context for Prisma v3.0 (ruling 27): `keyterms` and `topic`.

The docs (stt-websocket.md, Oct 30, 2026) say both are "Prisma v3.0 only": `keyterms` are up
to 200 words or phrases to favour, `topic` a free-text description fixed per connection;
v3.0 keyterms can change mid-session through `update_settings`. On v2.5 nothing here is
sent (`PrismaSTTService.set_keyterms` is a no-op there).

The lists are ours, hand-written for the interview topics and built from the session's own
data: the application form (UPSC) or the motion's case-file keywords (debate). No names or
identifiers are ever added (the form has none). Duck-typed on the session, so this module
needs neither Pipecat nor the debate package.
UNVERIFIED: whether keyterms help Hinglish recognition at all; decide on the owner's A/B.
"""

from __future__ import annotations

from typing import Any

# Words a UPSC board and candidate use whatever the topic.
UPSC_COMMON = [
    "UPSC", "IAS", "IPS", "IFS", "civil services", "Collector", "District Magistrate",
    "panchayat", "governance", "policy", "Constitution",
]
# Per interview topic (Settings.topics plus the opener's background round).
UPSC_TOPICS: dict[str, list[str]] = {
    "background": ["graduation", "optional subject", "hobbies", "district", "home state"],
    "tourism": [
        "tourism", "heritage", "ecotourism", "homestay", "foreign tourist arrivals", "GDP",
        "hospitality", "pilgrimage", "carrying capacity", "Swadesh Darshan",
    ],
    "internal_security": [
        "internal security", "Naxalism", "left wing extremism", "insurgency", "CRPF",
        "NIA", "police reforms", "cyber security", "radicalisation", "border management",
    ],
}
UPSC_TOPIC_TEXT = "A UPSC civil services personality test interview in Hinglish"


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
    form = getattr(session, "form", None)
    if form is not None:
        for name in ("home_state", "district", "degree_subject", "optional_subject"):
            value = getattr(form, name, None)
            if value:
                terms.append(str(value))
        terms.extend(getattr(form, "hobbies", []) or [])
    topics = getattr(state, "topics", []) or []
    named = ", ".join(t.replace("_", " ") for t in topics if t != "background")
    return _clean(terms), f"{UPSC_TOPIC_TEXT} about {named}." if named else UPSC_TOPIC_TEXT


def _clean(terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        t = " ".join(str(term).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:200]
