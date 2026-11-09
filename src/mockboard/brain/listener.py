"""Listener: turns one transcript segment into a typed `Analysis` (stance, reasons, claims).

It is one forced-tool Nova call. Claims get stable ids stamped with the turn and segment,
and numbers are re-normalised deterministically where the model left gaps.
"""

from __future__ import annotations

import re

from mockboard.brain.llm import LLMClient, ToolSpec
from mockboard.brain.numbers import nfc, parse_number_phrase
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.schemas import Analysis, Claim, ClaimKind, DebateArgument, Segment
from mockboard.brain.tools import (
    LISTENER_TOOL,
    ListenerClaim,
    ListenerOutput,
)

# Devanagari vowel signs are not `\w` in Python, so the script's block is listed too:
# "मुझे नहीं पता" is three words, not seven fragments.
_WORD = re.compile(r"[\w\u0900-\u097f]+", re.UNICODE)


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def build_listener_user(segment: Segment, question: str, context: str = "") -> str:
    """Per-call content goes in the user message so the system prompt stays cacheable.

    `context` is an extra, already-tagged block (debate mode: `<debate_context>`); UPSC mode
    passes none, so its message is unchanged."""
    return (
        f"Analyse segment {segment.segment_index} now and call the tool.\n"
        + (context + "\n" if context else "")
        + tag("question", question)
        + "\n"
        + tag("candidate_speech", segment.text)
    )


def _to_claim(
    raw: ListenerClaim, index: int, turn_id: str, turn_index: int, segment_id: str
) -> Claim:
    value = raw.value
    kind = raw.kind
    if kind in (ClaimKind.numeric, ClaimKind.date) and value is None:
        # Deterministic fallback: try to read the number from the transcribed text.
        value = parse_number_phrase(raw.text)
    if kind in (ClaimKind.numeric, ClaimKind.date) and value is None:
        kind = ClaimKind.qualitative  # a "number claim" with no number cannot be checked as one
    return Claim(
        id=f"{turn_id}.{segment_id}.c{index}",
        text=raw.text.strip(),
        claim_en=raw.claim_en.strip(),
        kind=kind,
        value=value,
        unit=raw.unit,
        year=raw.year,
        turn_id=turn_id,
        segment_id=segment_id,
        turn_index=turn_index,
    )


# Ruling 37: honest "I don't know" markers, read from the RAW segment text in both scripts,
# so a garbled or mis-labelled transcript of an honest answer still gets reassure or move_on
# (R3/R3b, debate D4), never pressure. Our own lists; UNVERIFIED against real Prisma output.
# The strong markers live in a model-free module so the director can read them too.
from mockboard.brain.unknown_markers import UNKNOWN_STRONG as _UNKNOWN_STRONG  # noqa: E402

# "baad mein" ("later") is a deflection only in a short segment, or next to "I will tell /
# think / see". "badh mein" is the form Prisma wrote in the owner's run (Defect H).
_UNKNOWN_LATER = re.compile(r"\b(?:baad|badh|bad) me(?:in|n)?\b|बाद में", re.IGNORECASE)
_LATER_VERB = re.compile(
    r"\b(?:baad|badh|bad) me(?:in|n)?\s+(?:\S+\s+)?(?:bata|soch|dekh)\w*"
    r"|बाद में\s+(?:\S+\s+)?(?:बता|सोच|देख)",
    re.IGNORECASE,
)
SHORT_SEGMENT_WORDS = 8


def honest_unknown_marker(text: str) -> bool:
    """True when the raw transcript says honestly that the speaker does not know (or will
    answer later), in Latin or Devanagari script."""
    text = nfc(text)
    if _UNKNOWN_STRONG.search(text):
        return True
    if _UNKNOWN_LATER.search(text):
        return word_count(text) <= SHORT_SEGMENT_WORDS or bool(_LATER_VERB.search(text))
    return False


def _raw_text_off_limits(out: ListenerOutput, text: str) -> bool:
    """Ruling 32: a mode's output model may screen the RAW segment text itself
    (`off_limits_in_raw_text`), so a listener that paraphrases an off-limits word away cannot
    hide it. The UPSC model has no such hook, so UPSC analyses are unchanged."""
    check = getattr(out, "off_limits_in_raw_text", None)
    return bool(check(text)) if callable(check) else False


def to_analysis(
    out: ListenerOutput, *, turn_id: str, turn_index: int, segment: Segment
) -> Analysis:
    claims = [
        _to_claim(c, i, turn_id, turn_index, segment.segment_id)
        for i, c in enumerate(out.claims)
        if c.text.strip() and c.claim_en.strip()
    ]
    debate: dict = {}
    if hasattr(out, "rebuts"):  # the debate listener's optional fields (CTO_NOTES_04 §2)
        debate = {
            "arguments": [
                DebateArgument(claim=a.claim.strip(), warrant=a.warrant.strip())
                for a in out.arguments
                if a.claim.strip()
            ],
            "rebuts": [r.strip() for r in out.rebuts if r.strip()],
            "question_to_opponent": out.question_to_opponent.strip(),
            "concedes_point": out.concedes_point,
            "point_strength": out.point_strength,
            "off_limits": out.off_limits or _raw_text_off_limits(out, segment.text),
        }
    honest = out.honest_unknown or honest_unknown_marker(segment.text)
    if hasattr(out, "candidate_request"):  # the panel listener's field (P5)
        debate["candidate_request"] = out.candidate_request
    return Analysis(
        turn_id=turn_id,
        segment_id=segment.segment_id,
        stance=out.stance,
        reasons=[r for r in out.reasons if r.strip()],
        claims=claims,
        balance=out.balance,
        honest_unknown=honest,
        concedes_error=out.concedes_error,
        # Ruling 37: an honest "I don't know" in a garbled transcript is still honest; it
        # gets reassure or move_on, not a request to repeat or pressure.
        garbled=out.garbled and not honest,
        word_count=word_count(segment.text),
        **debate,
    )


def fallback_analysis(*, turn_id: str, segment: Segment, note: str = "") -> Analysis:
    """Safe default when the listener fails: no claims, so nothing can be challenged."""
    return Analysis(
        turn_id=turn_id,
        segment_id=segment.segment_id,
        stance=note,
        word_count=word_count(segment.text),
        is_fallback=True,
    )


async def analyse_segment(
    llm: LLMClient,
    kit: PromptKit,
    *,
    segment: Segment,
    question: str,
    turn_id: str,
    turn_index: int,
    prompt_id: str = "listener",
    tool: ToolSpec = LISTENER_TOOL,
    output_model: type[ListenerOutput] = ListenerOutput,
    context: str = "",
) -> Analysis:
    """One listener call. Raises `LLMError` subclasses; the graph node decides the fallback.

    The defaults are the UPSC listener; a mode passes its own prompt id, tool, output model
    and context block (ADR-009). The call name stays "listener" so it runs on the fast lane."""
    out = await llm.structured(
        name="listener",
        system=kit.system(prompt_id),
        user=build_listener_user(segment, question, context),
        tool=tool,
        output_model=output_model,
        tag=turn_id,
    )
    return to_analysis(out, turn_id=turn_id, turn_index=turn_index, segment=segment)
