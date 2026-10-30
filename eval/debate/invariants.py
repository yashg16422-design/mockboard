"""Debate-board invariants (CTO_NOTES_04 §7, DB7), checked over WHOLE debates.

Used by `tests/test_debate_invariants.py` (scripted and seeded random debates) and by the
debate eval runner (`python -m eval.debate.runner`). Each check returns a list of
human-readable violations; an empty list means the debate kept the invariant.

1. `concedes_strong_points`: when a strong point answers a STANDING opponent argument and no
   earlier rule in the table applies (D1-D7), the opponent concedes that argument (D8); it
   never concedes an argument that was not standing.
2. `no_uncarded_numbers`: no board line states a figure that is not on the motion's cards or
   in the motion itself; a fact challenge may also say the user's own figure.
3. `no_repeated_cross_question`: never the same point cross-questioned twice, never two
   cross-questions in a row, never one straight after the user concedes.
4. `off_limits_redirected`: off-limits speech gets the Moderator's `redirect`, and `redirect`
   is used for nothing else.
5. `honest_unknown_not_pounced_on`: an honest "I don't know" is acknowledged and never met
   with a cross-question or a fact challenge.
6. `no_challenge_from_fallback`: a fact challenge only ever follows a REAL librarian verdict
   of "contradicted" (checked against the record of which librarian calls really answered,
   when the caller has one), and a deadline-fallback move never challenges or concedes.
7. `moves_in_allowed_set`: every move is in the debate profile's allowed set.

Plus two cheap checks of the design: `closing_concedes` (the closing statement concedes
something, CTO_NOTES_04 §3) and `steelman_at_most_once`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import (
    Analysis,
    BoardLine,
    BoardMove,
    Claim,
    Move,
    PointStrength,
)
from mockboard.modes import get_profile
from mockboard.modes.debate.guards import has_concession, split_sentences
from mockboard.modes.debate.motions import Side, load_curated_motions
from mockboard.modes.debate.session import DebateSetup, Length, OpponentStyle
from mockboard.modes.debate.strategist import CLASH, WARRANT_TARGET, user_text_off_limits
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession, split_segments

MODERATOR = "moderator"
ALLOWED_MOVES = get_profile("debate").allowed_moves


@dataclass
class PlayedDebate:
    """A whole debate, as the invariants need it."""

    motion_id: str
    analyses: list[Analysis]
    moves: list[BoardMove]
    lines: list[BoardLine]  # the board's reply to each user turn (the intro excluded)
    intro: str
    rounds: list[str]  # the round of each user turn
    last_in_round: list[bool]  # the round's turn budget is used up by this turn
    standing_before: list[list[str]]  # opponent arguments standing when each turn opened
    ledger: list[Claim]
    allowed: frozenset[float]
    rows: list[dict[str, Any]]
    # Claim texts (claim_en) a librarian call REALLY answered "contradicted"; None when the
    # caller does not track it (then the ledger's own record is used).
    real_contradicted: set[str] | None = None
    report: Any = None
    transcripts: dict[str, str] = field(default_factory=dict)  # turn id -> the user's words


async def answer(session: InterviewSession, text: str, *, deadline_s: float = 30.0,
                 think_s: float = 5.0):
    """One typed answer through the voice path's session API (as `tests/conftest.answer`)."""
    if think_s:
        await session.settle_fact_checks(think_s)
    for segment in split_segments(text, "-"):
        if segment.text:
            session.submit_segment(segment.text, segment_index=segment.segment_index)
    await session.end_of_turn(deadline_s)
    return await session.speak()


def make_setup(motion_id: str, side: str = "for", length: str = "quick",
               style: str = "logician", language: str = "hinglish") -> DebateSetup:
    return DebateSetup(
        motion=load_curated_motions()[motion_id], user_side=Side(side), length=Length(length),
        language=language, opponent_style=OpponentStyle(style),
    )


async def play_debate(
    deps,
    setup: DebateSetup,
    answers: Sequence[str],
    sid: str,
    *,
    trace_dir: Path | None = None,
    report: bool = False,
    language: str = "en",
    deadline_s: float = 30.0,
    real_contradicted: set[str] | None = None,
) -> PlayedDebate:
    analyzer = TurnAnalyzer(deps)
    session = InterviewSession(
        deps=deps, form=None,
        recorder=TraceRecorder(trace_dir or Path("traces"), sid, enabled=trace_dir is not None),
        analyzer=analyzer, session_id=sid, mode="debate", mode_options=setup,
    )
    await session.open()
    rounds, last, standing = [], [], []
    for text in answers:
        st = session.state
        if st.finished:
            break
        rounds.append(st.topic)
        last.append(st.turns_in_topic + 1 >= session._max_turns_in(st.topic))
        standing.append([a.id for a in session.hooks.state.standing])
        await answer(session, text, deadline_s=deadline_s)
    await session.settle_fact_checks(5.0)
    rep = await session.finish(language) if report else None
    if not report:
        await analyzer.close_session(sid)
    st = session.state
    return PlayedDebate(
        motion_id=setup.motion.id,
        analyses=[t.analysis or Analysis() for t in st.turns],
        moves=list(st.moves),
        lines=list(st.board_lines[1:]),
        intro=st.board_lines[0].text,
        rounds=rounds,
        last_in_round=last,
        standing_before=standing,
        ledger=list(st.claim_ledger),
        allowed=independent_allowed(setup, deps.cards.cards.values()),
        rows=list(session.hooks.rows),
        real_contradicted=real_contradicted,
        report=rep,
        transcripts={t.turn_id: t.transcript for t in st.turns},
    )


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------


def _turns(p: PlayedDebate) -> Iterable[tuple[int, Analysis, BoardMove]]:
    return enumerate(zip(p.analyses, p.moves, strict=True))


def concedes_strong_points(p: PlayedDebate) -> list[str]:
    bad = []
    for i, a, m in ((i, a, m) for i, (a, m) in _turns(p)):
        standing = set(p.standing_before[i])
        if m.move == Move.concede_point and m.target not in standing:
            bad.append(f"turn {i}: conceded {m.target}, which was not standing")
        hits = standing & set(a.rebuts)
        earlier_rule = (
            a.garbled or a.off_limits or bool(user_text_off_limits(a))
            or a.word_count == 0 or a.honest_unknown
            or p.rounds[i] != CLASH or p.last_in_round[i] or a.question_to_opponent
            or m.rule_id in ("FALLBACK", "CAP")
        )
        if a.point_strength == PointStrength.strong and hits and not earlier_rule and not (
            m.move == Move.concede_point and m.target in hits
        ):
            bad.append(f"turn {i}: strong point answered {sorted(hits)} but got {m.move.value}")
    return bad


# Deliberately NOT the production guard (`guards.sentence_allowed`): an independent, simpler
# reading of "a figure", so a broken guard cannot also blind this check.
_DIGITS = re.compile(r"\d+(?:[.,]\d+)?")
_NUMBER_WORDS = re.compile(
    r"\b(?:percent|per cent|pratishat|lakh|crore|million|billion|thousand|hazaar|hazar|"
    r"dozen|half of|twice as|double|triple|majority of)\b",
    re.IGNORECASE,
)


def figures(text: str) -> set[float]:
    return {float(x.replace(",", "")) for x in _DIGITS.findall(text)}


def independent_allowed(setup: DebateSetup, cards: Iterable[Any]) -> frozenset[float]:
    """The motion's own figures and its cards' values and years, recomputed here."""
    topics = set(setup.motion.card_topics)
    out = figures(setup.motion.text)
    for c in cards:
        if c.topic in topics:
            out |= {float(v) for v in (c.value, c.year) if v is not None}
    return frozenset(out)


def _figure_ok(sentence: str, allowed: frozenset[float]) -> bool:
    found = figures(sentence)
    if found - allowed:
        return False
    return not (_NUMBER_WORDS.search(sentence) and not found)


def no_uncarded_numbers(p: PlayedDebate) -> list[str]:
    bad = []
    claims = {c.id: c for c in p.ledger}
    lines = [("intro", None, p.intro)] + [
        (f"turn {i}", m, line.text) for i, (m, line) in enumerate(zip(p.moves, p.lines,
                                                                      strict=True))
    ]
    for where, move, text in lines:
        allowed = p.allowed
        if move is not None and move.move == Move.fact_challenge:
            c = claims.get(move.target or "")
            own = {float(x) for x in (c.value if c else None, c.year if c else None)
                   if x is not None}
            allowed = allowed | own | figures(c.text if c else "")
        for sentence in split_sentences(text):
            if not _figure_ok(sentence, allowed):
                bad.append(f"{where}: uncarded figure in {sentence!r}")
    return bad


def no_repeated_cross_question(p: PlayedDebate) -> list[str]:
    bad, seen = [], set()
    for i, (a, m) in _turns(p):
        if m.move != Move.cross_question:
            continue
        if i and p.moves[i - 1].move == Move.cross_question:
            bad.append(f"turn {i}: two cross-questions in a row")
        if a.concedes_point or a.concedes_error:
            bad.append(f"turn {i}: cross-question straight after the user conceded")
        if m.target and m.target != WARRANT_TARGET:
            if m.target in seen:
                bad.append(f"turn {i}: point {m.target} cross-questioned twice")
            seen.add(m.target)
    return bad


def off_limits_redirected(p: PlayedDebate) -> list[str]:
    bad = []
    for i, (a, m) in _turns(p):
        off = a.off_limits or bool(user_text_off_limits(a))  # listener flag or rule backstop
        if off and not a.garbled and m.rule_id not in ("FALLBACK", "CAP") and (
            m.move != Move.redirect or m.member != MODERATOR
        ):
            bad.append(f"turn {i}: off-limits speech got {m.move.value} from {m.member}")
        if m.move == Move.redirect and not off:
            bad.append(f"turn {i}: redirect without off-limits speech")
    return bad


def honest_unknown_not_pounced_on(p: PlayedDebate) -> list[str]:
    bad = []
    for i, (a, m) in _turns(p):
        if (not a.honest_unknown or a.garbled or a.off_limits or user_text_off_limits(a)
                or a.word_count == 0):
            continue
        if m.move in (Move.cross_question, Move.fact_challenge):
            bad.append(f"turn {i}: honest 'I don't know' got {m.move.value}")
        if m.rule_id not in ("FALLBACK", "CAP") and m.preface != "acknowledge":
            bad.append(f"turn {i}: honest 'I don't know' was not acknowledged")
    return bad


def no_challenge_from_fallback(p: PlayedDebate) -> list[str]:
    bad = []
    claims = {c.id: c for c in p.ledger}
    for i, (a, m) in _turns(p):
        if m.rule_id == "FALLBACK" and m.move in (
            Move.fact_challenge, Move.cross_question, Move.concede_point
        ):
            bad.append(f"turn {i}: deadline fallback chose {m.move.value}")
        if m.move != Move.fact_challenge:
            continue
        c = claims.get(m.target or "")
        if c is None or c.verdict.value != "contradicted":
            bad.append(f"turn {i}: fact challenge on {m.target} without a contradicted verdict")
        elif p.real_contradicted is not None and c.claim_en not in p.real_contradicted:
            bad.append(f"turn {i}: fact challenge on {m.target} came from a fallback verdict")
        if a.is_fallback:
            bad.append(f"turn {i}: fact challenge on a turn whose analysis is a fallback")
    return bad


def moves_in_allowed_set(p: PlayedDebate) -> list[str]:
    return [
        f"turn {i}: move {m.move.value} is not in the debate's allowed set"
        for i, m in enumerate(p.moves) if m.move not in ALLOWED_MOVES
    ]


def closing_concedes(p: PlayedDebate) -> list[str]:
    return [
        f"turn {i}: closing statement concedes nothing: {line.text!r}"
        for i, (m, line) in enumerate(zip(p.moves, p.lines, strict=True))
        if m.move == Move.closing_statement and not has_concession(line.text)
    ]


def steelman_at_most_once(p: PlayedDebate) -> list[str]:
    n = sum(m.move == Move.steelman for m in p.moves)
    return [f"steelman used {n} times"] if n > 1 else []


INVARIANTS: dict[str, Callable[[PlayedDebate], list[str]]] = {
    "concedes_strong_points": concedes_strong_points,
    "no_uncarded_numbers": no_uncarded_numbers,
    "no_repeated_cross_question": no_repeated_cross_question,
    "off_limits_redirected": off_limits_redirected,
    "honest_unknown_not_pounced_on": honest_unknown_not_pounced_on,
    "no_challenge_from_fallback": no_challenge_from_fallback,
    "moves_in_allowed_set": moves_in_allowed_set,
    "closing_concedes": closing_concedes,
    "steelman_at_most_once": steelman_at_most_once,
}


def violations(p: PlayedDebate) -> dict[str, list[str]]:
    """Invariant name -> its violations (only the invariants that failed)."""
    return {name: v for name, check in INVARIANTS.items() if (v := check(p))}


# ---------------------------------------------------------------------------
# Report checks (DB6), for debates played with `report=True`
# ---------------------------------------------------------------------------

_DISCLAIMERS = (
    "This verdict judges how the debate was argued, not which side of the motion is right.",
    "यह निर्णय इस पर है कि बहस कैसे की गई, इस पर नहीं कि प्रस्ताव का कौन सा पक्ष सही है।",
)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[\"'“”‘’.,!?;:।()\[\]-]", " ", text.lower()).split())


def report_problems(p: PlayedDebate) -> list[str]:
    """What is wrong with the debate's report, if anything. Independent of the judge module:
    quotes are re-checked against the user's own transcripts, the scoreboard is recomputed,
    and every verdict reason is traced back to a rubric score or the scoreboard."""
    from mockboard.brain.schemas import DebateReport, VerdictLabel
    from mockboard.modes.debate.scoreboard import compute_scoreboard

    rep = p.report
    if not isinstance(rep, DebateReport):
        return [f"not a debate report: {type(rep).__name__}"]
    bad: list[str] = []
    off = {f"t{i}" for i, a in enumerate(p.analyses) if a.off_limits}
    for s in rep.scores:
        if isinstance(s.score, int) and not s.evidence:
            bad.append(f"{s.dimension.value}: numeric score without evidence")
        for e in s.evidence:
            if _norm(e.quote) not in _norm(p.transcripts.get(e.turn_id, "")):
                bad.append(f"{s.dimension.value}: quote not in the user's turn {e.turn_id}")
    if not rep.is_fallback and len(rep.scores) != 6:
        bad.append(f"{len(rep.scores)} rubric scores, expected 6")
    b = rep.best_line
    if b is not None:
        if _norm(b.quote) not in _norm(p.transcripts.get(b.turn_id, "")):
            bad.append("best line is not the user's own words")
        if b.turn_id in off:
            bad.append("best line comes from an off-limits turn")
        if not _figure_ok(b.quote, p.allowed):
            bad.append("best line has an uncarded figure")
    case = sorted({i for ids in p.standing_before[:1] for i in ids})
    if rep.scoreboard != compute_scoreboard(p.rows, case):
        bad.append("scoreboard differs from the one recomputed from the debate record")
    v = rep.verdict
    if not any(v.text.endswith(d) for d in _DISCLAIMERS):
        bad.append("verdict does not end with the judges-the-debating disclaimer")
    by_dim = {s.dimension: s.score for s in rep.scores}
    for r in v.reasons:
        if r.kind != "scoreboard" and by_dim.get(r.dimension) != r.score:
            bad.append(f"verdict reason {r.kind}/{r.dimension} does not match the rubric")
    numeric = [x for x in by_dim.values() if isinstance(x, int)]
    if (v.label == VerdictLabel.no_verdict) != (rep.is_fallback or len(numeric) < 3):
        bad.append(f"verdict label {v.label.value} does not follow the label table")
    body = v.text
    for d in _DISCLAIMERS:
        body = body.replace(d, "")
    if re.search(r"\b(?:motion|side)\b[^.]{0,40}\b(?:is|was)\s+(?:\w+\s+)?"
                 r"(?:right|correct|true|wrong)\b",
                 body, re.IGNORECASE):
        bad.append("verdict takes a side on the motion")
    return bad
