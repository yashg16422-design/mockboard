"""Study partner strategist: Mitra's next move, by PURE deterministic rules.

Same contract as the UPSC and debate strategists: no LLM, no I/O, first matching rule wins, every
move carries its rule id. This table is the specification; tests/test_partner_strategist.py checks
every row. The partner is low pressure: it never scores, never times the speaker out, and asks one
question per line.

| Rule        | Condition (this turn)                                        | Move               |
|-------------|--------------------------------------------------------------|--------------------|
| P-END       | a clear yes to "stop here?" (`hooks`, `is_clear_yes`)        | move_on (close)    |
| P-RESUME    | anything else after "stop here?" (`hooks`)                   | reassure (resume)  |
| P-CONFIRM   | the speaker asks to end (the request detectors, `hooks`)     | reassure (confirm) |
| P-REPEAT    | the speaker asks to hear the question again (`hooks`)        | reassure (repeat)  |
| P-SKIP      | the speaker asks to leave this point (`hooks`)               | move_on (skip)     |
| P-THINK     | the speaker asks for a moment to think (`hooks`)             | reassure (think)   |
| P-SILENCE   | nothing said                                                 | reassure           |
| P-CLARIFY   | garbled transcript (and not an honest "I don't know")        | clarify            |
| P-STUCK     | honest "I don't know", or under `SHORT_WORDS` words          | reassure           |
| P-CLOSE     | counted turns reach the soft cap (this turn included)        | move_on (close)    |
| P-STEELMAN  | answering the counterpoint just raised, and granting it      | steelman           |
| P-ANSWERED  | answering a counterpoint (or the steelman after it)          | move_on (recap)    |
| P-RECAP     | `recap_every` counted turns since the last recap             | move_on (recap)    |
| P-WHY       | the point has no reason yet (unless the last move asked why) | probe              |
| P-EXAMPLE   | the point has no example yet (unless just asked for one)     | pressure           |
| P-COUNTER   | otherwise (a reason and an example, or both already asked)   | counter            |

The request rules (P-END to P-THINK) need the raw transcript, so they live in the session hooks
(`decide_turn`), which call `decide_partner` with the full partner state. The profile's `decide`
(the graph's strategist node) sees the listener's analysis only; the hooks' decision, made with the
raw transcript, is the one the session uses. "Counted" turns are the substantive ones: not silence,
garbled, stuck, or a request. A point runs from its first counted turn to the next recap; a reason
or example given in any of its turns counts for the whole point.

Reason and example markers are read from the raw transcript in Latin, Devanagari and Tamil script.
They are our own lists, unmeasured on real speech. UNVERIFIED: the Tamil markers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from mockboard.brain.numbers import nfc
from mockboard.brain.schemas import Analysis, BoardMove, Claim, Move, StrategyContext
from mockboard.brain.strategist import StrategyConfig, merge_analyses
from mockboard.modes.partner.lines import (
    CLOSE_TARGET,
    CONFIRM_END_TARGET,
    RECAP_TARGET,
    REPEAT_TARGET,
    RESUME_TARGET,
    SKIP_TARGET,
    THINK_TARGET,
)

PARTNER = "mitra"  # the one persona id (the profile's chairman)
SHORT_WORDS = 4  # fewer words than this is "very short": a gentle start, not a probe
SOFT_CAP = 12  # counted turns; then the partner closes with a recap
RECAP_EVERY = 4  # counted turns between recaps

PARTNER_MOVES: frozenset[Move] = frozenset(
    {
        Move.probe,
        Move.pressure,
        Move.counter,
        Move.steelman,
        Move.reassure,
        Move.clarify,
        Move.move_on,
    }
)

# The partner never presses hard: everything is 0 or 1 on the board's 0-3 pressure scale.
PARTNER_PRESSURE: dict[Move, int] = {
    Move.probe: 1,
    Move.pressure: 1,
    Move.counter: 1,
    Move.steelman: 1,
    Move.reassure: 0,
    Move.clarify: 0,
    Move.move_on: 0,
}

_REASON = re.compile(
    r"\b(?:because|since|so that|therefore|that is why|that's why|the reason|due to|as a result"
    r"|kyunki|kyonki|kyuki|kyoki|kyunke|isliye|is liye|iski wajah|wajah se|kaaran|karan se"
    r"|taaki|taki|chunki|enenral|enendral|adhanaala|athanala|kaaranam)\b"
    r"|क्योंकि|इसलिए|इस लिए|कारण|वजह|ताकि|चूंकि|चूँकि"
    r"|ஏனென்றால்|ஏனெனில்|அதனால்|காரணம்",
    re.IGNORECASE,
)
_EXAMPLE = re.compile(
    r"\b(?:for example|for instance|e\.g\.|such as|example|take the case|in the case of"
    r"|jaise ki|jaise(?! hi)|misaal|misal|udaharan|udaaharan|maslan|udharanam|udaharanam)\b"
    r"|उदाहरण|मिसाल|जैसे(?! ही)"
    r"|உதாரணமாக|உதாரணம்|எடுத்துக்காட்டாக",
    re.IGNORECASE,
)
_CONCEDE = re.compile(
    r"\b(?:you are right|you're right|fair point|good point|i agree|i accept|that is true"
    r"|aap sahi hain|aap sahi ho|sahi baat hai|maanta hoon|maanti hoon|maan leta|maan leti"
    r"|theek kaha|baat sahi hai)\b"
    r"|आप सही|सही बात|मानता हूँ|मानती हूँ|मानता हूं|मानती हूं"
    r"|நீங்கள் சொல்வது சரி|சரிதான்",
    re.IGNORECASE,
)
_WORD = re.compile(r"[\wऀ-ॿ஀-௿]+", re.UNICODE)


def word_count(text: str) -> int:
    return len(_WORD.findall(nfc(text or "")))


def reason_marker(text: str) -> re.Match[str] | None:
    return _REASON.search(nfc(text or ""))


def example_marker(text: str) -> re.Match[str] | None:
    return _EXAMPLE.search(nfc(text or ""))


@dataclass(frozen=True)
class PartnerSignals:
    """What the rules read about one turn."""

    words: int = 0
    garbled: bool = False
    honest_unknown: bool = False
    has_reason: bool = False
    has_example: bool = False
    concedes: bool = False
    listener_failed: bool = False

    @property
    def silent(self) -> bool:
        return self.words == 0

    @property
    def stuck(self) -> bool:
        return self.honest_unknown or 0 < self.words < SHORT_WORDS

    @property
    def counted(self) -> bool:
        """A substantive turn: it counts toward recaps and the soft cap."""
        return not (self.silent or self.garbled or self.stuck)


@dataclass(frozen=True)
class PartnerState:
    """The partner's memory between turns (kept by the session hooks)."""

    counted: int = 0  # substantive turns so far
    since_recap: int = 0  # substantive turns since the last recap
    last_move: Move | None = None
    awaiting_answer: bool = False  # a counterpoint (or its steelman) is not yet answered
    pending_end: bool = False  # Mitra has asked "stop here?" and waits for the reply
    # The point in progress (since the opener or the last recap) already has a reason / example.
    point_reason: bool = False
    point_example: bool = False
    soft_cap: int = SOFT_CAP
    recap_every: int = RECAP_EVERY


def signals_from(analysis: Analysis | None, text: str | None = None) -> PartnerSignals:
    """Signals from the listener's analysis and, when given, the raw transcript. Without the
    listener (a failed call), the raw transcript alone decides: never garbled, markers only."""
    a = analysis or Analysis()
    if text is None:  # the graph's strategist node: listener only
        return PartnerSignals(
            words=a.word_count,
            garbled=a.garbled and not a.honest_unknown,
            honest_unknown=a.honest_unknown,
            has_reason=len(a.reasons) >= 2,
            has_example=bool(a.claims),
            concedes=a.concedes_error,
            listener_failed=a.is_fallback,
        )
    from mockboard.brain.listener import honest_unknown_marker  # the UPSC listener's own markers

    words = word_count(text)
    honest = honest_unknown_marker(text) or (a.honest_unknown and not a.is_fallback)
    if a.is_fallback:
        return PartnerSignals(
            words=words,
            honest_unknown=honest,
            has_reason=reason_marker(text) is not None,
            has_example=example_marker(text) is not None,
            concedes=_CONCEDE.search(nfc(text)) is not None,
            listener_failed=True,
        )
    return PartnerSignals(
        words=words,
        garbled=a.garbled and not honest,
        honest_unknown=honest,
        has_reason=reason_marker(text) is not None or len(a.reasons) >= 2,
        has_example=example_marker(text) is not None or bool(a.claims),
        concedes=a.concedes_error or _CONCEDE.search(nfc(text)) is not None,
    )


def _move(move: Move, rule: str, why: str, turn_id: str, target: str | None = None) -> BoardMove:
    return BoardMove(
        move=move,
        target=target,
        member=PARTNER,
        pressure=PARTNER_PRESSURE[move],
        reason=f"{rule}: {why}",
        rule_id=rule,
        for_turn=turn_id,
    )


def end_move(turn_id: str) -> BoardMove:
    return _move(Move.move_on, "P-END", "the speaker confirmed the end; recap and close", turn_id,
                 CLOSE_TARGET)


def confirm_end_move(turn_id: str) -> BoardMove:
    return _move(Move.reassure, "P-CONFIRM", "the speaker asked to end; check once before closing",
                 turn_id, CONFIRM_END_TARGET)


def resume_move(turn_id: str) -> BoardMove:
    return _move(Move.reassure, "P-RESUME", "no clear yes to stopping; the question stands",
                 turn_id, RESUME_TARGET)


def repeat_move(turn_id: str) -> BoardMove:
    return _move(Move.reassure, "P-REPEAT", "the speaker asked to hear the question again",
                 turn_id, REPEAT_TARGET)


def skip_move(turn_id: str) -> BoardMove:
    return _move(Move.move_on, "P-SKIP", "the speaker asked to leave this point", turn_id,
                 SKIP_TARGET)


def think_move(turn_id: str) -> BoardMove:
    return _move(Move.reassure, "P-THINK", "the speaker asked for a moment to think", turn_id,
                 THINK_TARGET)


def decide_partner(sig: PartnerSignals, st: PartnerState, *, turn_id: str) -> BoardMove:
    """The rule table (module docstring), from P-SILENCE down. Pure."""
    if sig.silent:
        return _move(Move.reassure, "P-SILENCE", "nothing said; offer a gentle way in", turn_id)
    if sig.garbled:
        return _move(Move.clarify, "P-CLARIFY", "the transcript is unclear", turn_id)
    if sig.stuck:
        why = "an honest 'I don't know'" if sig.honest_unknown else "a very short turn"
        return _move(Move.reassure, "P-STUCK", f"{why}; offer one starting angle", turn_id)
    if st.counted + 1 >= st.soft_cap:
        return _move(Move.move_on, "P-CLOSE", f"soft cap of {st.soft_cap} turns reached", turn_id,
                     CLOSE_TARGET)
    if st.awaiting_answer and sig.concedes and st.last_move == Move.counter:
        return _move(Move.steelman, "P-STEELMAN",
                     "the speaker granted the counterpoint; rebuild the point", turn_id)
    if st.awaiting_answer:
        return _move(Move.move_on, "P-ANSWERED", "the counterpoint was answered; recap", turn_id,
                     RECAP_TARGET)
    if st.since_recap + 1 >= st.recap_every:
        return _move(Move.move_on, "P-RECAP", f"{st.recap_every} turns since the last recap",
                     turn_id, RECAP_TARGET)
    if not (sig.has_reason or st.point_reason) and st.last_move != Move.probe:
        return _move(Move.probe, "P-WHY", "a point with no reason yet", turn_id)
    if not (sig.has_example or st.point_example) and st.last_move != Move.pressure:
        return _move(Move.pressure, "P-EXAMPLE", "a reason but no example yet", turn_id)
    return _move(Move.counter, "P-COUNTER", "reason and example given; test the point", turn_id)


# ---------------------------------------------------------------------------
# The profile's three functions (ModeProfile contract)
# ---------------------------------------------------------------------------


def decide_partner_move(
    ctx: StrategyContext, analyses: Sequence[Analysis], ledger: Sequence[Claim],
    cfg: StrategyConfig,
) -> BoardMove:
    """The graph's strategist node: listener-only signals. The session hooks re-decide with the
    raw transcript and the full state (soft cap, requests); see the module docstring."""
    del ledger, cfg  # the partner never fact-checks
    sig = signals_from(merge_analyses(analyses) if analyses else None)
    st = PartnerState(
        since_recap=ctx.turns_in_topic,
        last_move=ctx.last_move,
        awaiting_answer=ctx.last_move in (Move.counter, Move.steelman),
        soft_cap=10**6,  # not known here; the hooks apply it
    )
    return decide_partner(sig, st, turn_id=ctx.turn_id)


def partner_fallback_move(
    ctx: StrategyContext, spoke: bool, reason: str, cfg: StrategyConfig
) -> BoardMove:
    """No analysis finished in time: nothing said -> reassure, otherwise ask for the reason."""
    del cfg
    move = Move.probe if spoke else Move.reassure
    return BoardMove(
        move=move, member=PARTNER, pressure=PARTNER_PRESSURE[move],
        reason=f"FALLBACK: {reason}", rule_id="FALLBACK", for_turn=ctx.turn_id,
    )


def partner_cap_move(
    ctx: StrategyContext, move: BoardMove, max_turns: int, cfg: StrategyConfig
) -> BoardMove:
    """The session's hard turn cap: recap and close."""
    del cfg
    if move.move == Move.move_on and move.target == CLOSE_TARGET:
        return move
    return BoardMove(
        move=Move.move_on, target=CLOSE_TARGET, member=PARTNER,
        pressure=PARTNER_PRESSURE[Move.move_on],
        reason=f"CAP: session turn cap ({max_turns}) reached", rule_id="CAP",
        for_turn=ctx.turn_id,
    )
