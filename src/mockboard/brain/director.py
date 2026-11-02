"""The panel director: one model-free decision per candidate turn (CTO_NOTES_06 §2, §3;
CTO_NOTES_12 §9; CTO_NOTES_14 §7).

    START -> commit_last -> understand_turn -> handle_request | handoff | next_question | close

The strategist's move is computed during speech, as before. The director is the last, instant
step on top of it: it honours candidate requests, spends budgets and picks seeds. Nothing here
calls a model, so it cannot add model latency.

`decide_panel` is the pure policy and the graph runs the same pieces, so `PanelDirector` can fall
back to it when the graph breaks and a request is still honoured.

Route order (CTO_NOTES_12 §9.4): (1) the `pending_end` reply, then `end`; (2) wrap-up; (3) a
free-kind turn with a free turn left; (4) `skip`, `change_topic`; (5) budget spent on any counted
turn, which overrides every strategist move; (6) `next_question`. The hard turn cap comes before
all but a confirmed `end`, since no turn may follow it.

What can change:
- Nothing in the decide path writes anything but `decision`. A reopened turn (Ruling 31) simply
  re-decides. `memory`, `requests` and `decisions` change only in `commit_last`, once per turn id.
- `answers_used`, `free_used` and the position in `ctx` belong to the session side. They arrive
  here as inputs; `decision.counted` and `decision.free` say how `hooks.apply` should move them.

A declined `end` that counts, after a counted `end` on a segment's last unit, overspends that
segment by one (Ruling 52 keeps the confirmation there, Ruling 66 covers only the last unit of the
whole agenda). The cap guard then closes one answer early, so `answers_used <= max_answers` and
`turns <= max_answers + free_turns` still hold, and `must_ask` is timed against the cap too. A
counted decline with the situation due asks it. Known cost: a candidate who has used every free
turn and asks to stop on the last available question can still lose it, since adding a turn would
break the cap (Ruling 62's "should be asked" holds as far as the cap allows, CTO_NOTES_15 §4).
"""

from __future__ import annotations

import logging
import operator
import re
import unicodedata
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from mockboard.brain.affairs import AffairsNote, words_heard
from mockboard.brain.graphs import make_serde
from mockboard.brain.panel_lines import (
    CAP_CLOSE_LINES,
    CHANGE_TOPIC_ACK_LINES,
    CLARIFY_PANEL_LINES,
    CLOSE_LINES,
    END_CLOSE_LINES,
    END_CONFIRM_LINES,
    END_DECLINED_LINES,
    HANDOFF_LINES,
    REGISTER_LINES,
    REPEAT_PREFIX_LINES,
    SKIP_ACK_LINES,
    WRAP_UP_CLOSE_LINES,
    LineSet,
    handoff_line,
    line_for,
)
from mockboard.brain.register import register_hit
from mockboard.brain.requests import ChangeStrength, RequestKind, detect_request, word_count
from mockboard.brain.schemas import (
    PANEL_FREE_TURNS,
    Agenda,
    AgendaSeed,
    AgendaSegment,
    Analysis,
    BoardMove,
    CloseReason,
    CompletedTurn,
    DirectorNode,
    DirectorTurn,
    Move,
    PanelContext,
    PanelDecision,
    PanelMemory,
    PanelRequest,
    PanelSettings,
    PrefacePart,
    TurnNote,
    TurnUnderstanding,
)
from mockboard.brain.strategist import CLOSE_TARGET, PRESSURE

logger = logging.getLogger("mockboard.brain.director")

# The notes for this sitting, set by PanelDirector.decide_turn. None means the file.
_SITTING_BRIEFING: ContextVar[tuple[AffairsNote, ...] | None] = ContextVar(
    "sitting_briefing", default=None
)

__all__ = [
    "PANEL_FREE_TURNS",
    "PanelDirector",
    "build_director_graph",
    "compile_director_graph",
    "decide_panel",
    "fold_turns",
    "is_clear_yes",
    "next_seed",
    "opening_memory",
]

CAP_RULE_ID = "CAP"  # `upsc_cap_move`'s rule id: the session's hard turn cap
MAX_GIST_WORDS = 30
MAX_GIST_REASONS = 2
FIXED_LINE_MOVES = frozenset({Move.repeat_question, Move.rephrase_question, Move.clarify})

CLOSE_LINE_BY_REASON: dict[str, LineSet] = {
    "agenda": CLOSE_LINES,
    "end_request": END_CLOSE_LINES,
    "time": WRAP_UP_CLOSE_LINES,
    "answers": CAP_CLOSE_LINES,
    "ceiling": CLOSE_LINES,
}


# --- The clear yes (Ruling 52). The detector has no `yes` kind (CTO_NOTES_11 §8). --------------


def _norm(word: str) -> str:
    return unicodedata.normalize("NFC", word.casefold())


_YES_WORDS = frozenset(
    _norm(w)
    for w in (
        "haan", "han", "haa", "haanji", "hanji", "ji", "jee", "yes", "yeah", "yep", "bilkul",
        "zaroor", "sure", "ok", "okay", "theek", "thik",
        "हाँ", "हां", "जी", "बिल्कुल", "ज़रूर", "ठीक",
    )
)  # fmt: skip
_YES_FILLERS = frozenset(
    _norm(w) for w in ("please", "sir", "madam", "mam", "hai", "kijiye", "है", "कृपया")
)
_NEGATIONS = frozenset(
    _norm(w)
    for w in ("no", "not", "nope", "nahi", "nahin", "na", "mat", "dont", "नहीं", "नही", "मत", "ना")
)
_PUNCTUATION = ".,!?;:'\"-…।"


def is_clear_yes(text: str) -> bool:
    """True only for a plain yes to "end here?". Anything doubtful resumes, as Ruling 52 asks.

    Either the whole reply is yes-words, or it repeats the `end` request without a negation
    ("haan interview khatam karte hain"). Word lists are ours, unmeasured on real transcripts.
    """
    if text.rstrip().endswith(("?", "？")):  # "ji?" is "pardon?", and a close cannot be undone
        return False
    words = [w for w in (_norm(t.strip(_PUNCTUATION)) for t in text.split()) if w]
    if not words or any(w.replace("'", "") in _NEGATIONS for w in words):
        return False
    if all(w in _YES_WORDS or w in _YES_FILLERS for w in words):
        return any(w in _YES_WORDS for w in words)
    hit = detect_request(text)
    return hit is not None and hit.kind is RequestKind.END


# --- Agenda helpers ----------------------------------------------------------------------------


def opening_memory(agenda: Agenda, question: str | None = None) -> PanelMemory:
    """The memory a session starts with. The opener consumes the first segment's seeds[0], so the
    same person cannot ask it twice (CTO_NOTES_14 §7.4). `question` is the line really spoken."""
    first = agenda.segments[0]
    return PanelMemory(
        asked_seeds={first.id: [0]},
        last_question=question if question is not None else first.seeds[0].text,
    )


def _following(agenda: Agenda, segment_id: str) -> AgendaSegment | None:
    agenda.segment(segment_id)  # a bad id raises, and reaches the `director_error` fallback
    ids = agenda.topics
    index = ids.index(segment_id) + 1
    return agenda.segments[index] if index < len(ids) else None


def _unasked(segment: AgendaSegment, memory: PanelMemory) -> list[tuple[int, AgendaSeed]]:
    asked = set(memory.asked_seeds.get(segment.id, ()))
    return [(i, seed) for i, seed in enumerate(segment.seeds) if i not in asked]


def next_seed(
    segment: AgendaSegment, memory: PanelMemory, questions_left: int
) -> tuple[int, AgendaSeed] | None:
    """The one place a seed is chosen: by questions left, never by index (CTO_NOTES_14 §7.3).

    `must_ask` waits for the segment's last question, so it stays inside the budget.
    """
    if questions_left < 1:
        return None
    unasked = _unasked(segment, memory)
    must = next((pair for pair in unasked if pair[1].must_ask), None)
    plain = next((pair for pair in unasked if not pair[1].must_ask), None)
    if must is not None and (questions_left == 1 or plain is None):
        return must
    return plain


def _must_ask_unasked(segment: AgendaSegment, memory: PanelMemory) -> bool:
    return any(seed.must_ask for _, seed in _unasked(segment, memory))


def _cap_left(turn: DirectorTurn, cfg: PanelSettings) -> int | None:
    """Answers left for the session after this one, or None when uncapped."""
    if cfg.max_answers is None:
        return None
    return cfg.max_answers - turn.answers_used - 1


def _briefing_probe_due(
    turn: DirectorTurn, segment: AgendaSegment, memory: PanelMemory, cfg: PanelSettings
) -> bool:
    """One follow-up when the answer names a prepared subject and a later question remains."""
    if memory.probes_on_seed >= 1 or _questions_left(segment, turn, cfg) <= 1:
        return False
    return bool(words_heard(turn.raw_text, _SITTING_BRIEFING.get()))


def _questions_left(segment: AgendaSegment, turn: DirectorTurn, cfg: PanelSettings) -> int:
    """Questions this segment can still put after a counted turn."""
    left = segment.budget - turn.ctx.turns_in_topic - 1
    cap_left = _cap_left(turn, cfg)
    return left if cap_left is None else min(left, cap_left)


def _cap_hit(turn: DirectorTurn, cfg: PanelSettings) -> bool:
    cap_left = _cap_left(turn, cfg)
    return cap_left is not None and cap_left <= 0


VETO_OVER_WORDS = 8  # Ruling 53: a weak hit on a turn longer than this can be vetoed
LLM_REQUEST_MAX_WORDS = 30  # CTO_NOTES_06 §3: a longer turn takes no request from the listener


@dataclass(frozen=True)
class ResolvedRequest:
    """What the turn asked of the board, after the rules and the listener have both been heard.

    `source` is "rules", "rules+llm", "llm", "llm_ignored" (the listener voted, the code did not
    follow it), "rules_vetoed" (a weak rule hit the healthy listener denied) or "none".
    `disagree` is True when a healthy listener said something else than the rules did, whether
    or not the rules won. `weak_hit` is whether a rule hit was one of the seven weak patterns
    (before any veto)."""

    kind: RequestKind | None = None
    source: str = "none"
    pattern: str = ""
    avoid_terms: tuple[str, ...] = ()
    weak_hit: bool = False
    disagree: bool = False


def _listener_vote(analysis: Analysis) -> str | None:
    """The listener's vote, or None when it cannot be counted: a listener that failed on any
    segment has not said "none" about it, so its silence is no evidence (Ruling 56)."""
    if analysis.is_fallback or analysis.any_fallback:
        return None
    return analysis.candidate_request or "none"


def _resolve_request(turn: DirectorTurn) -> ResolvedRequest:
    """The one place that weighs the rules against the listener (CTO_NOTES_06 §3, Rulings 53, 56).

    | rules hit X | listener (healthy)  | result                                                   |
    | X           | X                   | X, "rules+llm"                                           |
    | X           | anything else       | X, "rules", `disagree`; EXCEPT a weak change_topic and   |
    |             | including none      | none on a turn over 8 words: "rules_vetoed", no request  |
    | X           | failed or absent    | X, "rules"                                               |
    | none        | Y, 30 words or less | Y, "llm"; except `end`, which needs a rule hit           |
    | none        | Y, over 30 words    | none, "llm_ignored"                                      |
    | none        | none or failed      | none                                                     |

    Strong patterns and a failed listener never change a rule hit. The detector itself is frozen
    (Ruling 57): everything wider than its patterns is the listener's to find.
    """
    text = turn.raw_text
    hit = detect_request(text)
    vote = _listener_vote(turn.analysis)
    words = word_count(text)
    if hit is not None:
        weak = hit.strength is ChangeStrength.WEAK
        if vote == hit.kind.value:
            return ResolvedRequest(hit.kind, "rules+llm", hit.pattern_id, hit.avoid_terms, weak)
        disagree = vote is not None
        if weak and vote == "none" and words > VETO_OVER_WORDS:
            return ResolvedRequest(None, "rules_vetoed", hit.pattern_id, (), True, True)
        return ResolvedRequest(hit.kind, "rules", hit.pattern_id, hit.avoid_terms, weak, disagree)
    if vote in (None, "none"):
        return ResolvedRequest()
    if vote == RequestKind.END.value or words > LLM_REQUEST_MAX_WORDS:
        return ResolvedRequest(source="llm_ignored")
    try:
        return ResolvedRequest(RequestKind(vote), "llm")
    except ValueError:  # not one of the five kinds
        return ResolvedRequest()


def _is_silence(turn: DirectorTurn) -> bool:
    """No words at all. Not `rule_id == "R3"`, which also means a long pause and a first honest
    "pata nahi", and both of those count. A fallback analysis also has `word_count` 0, so the
    transcript is checked too: a candidate who spoke is not silent because the listener failed."""
    return turn.analysis.word_count == 0 and word_count(turn.raw_text) == 0


def understand(
    turn: DirectorTurn, agenda: Agenda, memory: PanelMemory, cfg: PanelSettings
) -> TurnUnderstanding:
    """Classify the turn, price it, and choose the node. The route table of the module docstring."""
    ctx = turn.ctx
    segment = agenda.segment(ctx.topic)
    budget_spent = ctx.turns_in_topic + 1 >= segment.budget
    last_segment = _following(agenda, segment.id) is None

    hit = _resolve_request(turn)
    kind = hit.kind
    known: dict[str, Any] = {
        "request_kind": kind.value if kind else None,
        "request_source": hit.source,
        "request_pattern": hit.pattern,
        "avoid_terms": hit.avoid_terms,
        "change_is_weak": hit.weak_hit,
        "request_disagree": hit.disagree,
    }

    # Step 1, the reply: a clear yes neither counts nor uses a free turn (CTO_NOTES_12 §9.3).
    if memory.pending_end and is_clear_yes(turn.raw_text):
        return TurnUnderstanding(node="close", close_reason="end_request", **known)

    free_kind: str | None = None
    if memory.pending_end:  # any request in a declining reply is not acted on, so it is not logged
        free_kind = "end_declined"
        known = {}
    elif kind in (RequestKind.END, RequestKind.REPEAT, RequestKind.CLARIFY_QUESTION):
        free_kind = kind.value
    elif kind is None and (turn.base_move.rule_id == "R1" or turn.analysis.garbled):
        # `analysis` too: on the last allowed turn the session's cap has replaced R1's move.
        free_kind = "garbled"
    elif kind is None and _is_silence(turn):
        free_kind = "silence"
    free = free_kind is not None and turn.free_used < cfg.free_turns
    priced: dict[str, Any] = {**known, "free_kind": free_kind, "counted": not free, "free": free}

    def spend_out(reason: CloseReason = "agenda") -> TurnUnderstanding:
        if last_segment or _cap_hit(turn, cfg):
            return TurnUnderstanding(node="close", close_reason=reason, **priced)
        return TurnUnderstanding(node="handoff", **priced)

    spent_reason: CloseReason = "answers" if free_kind else "agenda"
    spent = not free and (budget_spent or _cap_hit(turn, cfg))

    # The session's cap rewrites the move on the last turn it allows, and that turn is usually a
    # legal last unit (`max_turns` is the budget total plus free turns), which closes by its own
    # reason below. `ceiling` is for a close nothing else would have made (CTO_NOTES_14 §7.9).
    natural_end = spent and (last_segment or _cap_hit(turn, cfg))
    if turn.base_move.rule_id == CAP_RULE_ID and not natural_end:
        return TurnUnderstanding(node="close", close_reason="ceiling", **priced)
    if memory.pending_end:
        return spend_out(spent_reason) if spent else _handle(priced)
    if kind is RequestKind.END:
        # Ruling 66: nothing is left to resume, so asking would need a turn past the limit.
        last_unit = not free and (_cap_hit(turn, cfg) or (budget_spent and last_segment))
        if last_unit:
            return TurnUnderstanding(node="close", close_reason="end_request", **priced)
        return _handle(priced)
    if turn.wrap_up_due:  # step 2
        return TurnUnderstanding(node="close", close_reason="time", **priced)
    if free:  # step 3
        return _handle(priced)
    if kind in (RequestKind.SKIP, RequestKind.CHANGE_TOPIC):  # step 4
        return _handle(priced)
    if spent:  # step 5: overrides R2, R3 latency and R4, which sit before R5 in the rule table
        return spend_out(spent_reason)
    if free_kind:  # free turns have run out: an answer now, with the same response
        return _handle(priced)
    # Step 6. A `move_on` with budget left still lets the situation be asked (§9.4).
    # One briefing follow-up stays, when the candidate has named a prepared subject and
    # this segment still has a later question. It does not replace the last question.
    briefing = _briefing_probe_due(turn, segment, memory, cfg)
    leaving = turn.base_move.move == Move.move_on and not _must_ask_unasked(segment, memory)
    if leaving and not briefing:
        return spend_out()
    return TurnUnderstanding(node="next_question", **priced)


def _handle(priced: dict[str, Any]) -> TurnUnderstanding:
    return TurnUnderstanding(node="handle_request", **priced)


# --- Planners. Each builds the whole decision for one node. ------------------------------------


class _Plan:
    """The inputs every planner reads, and the one way to build a decision."""

    def __init__(
        self,
        turn: DirectorTurn,
        agenda: Agenda,
        memory: PanelMemory,
        cfg: PanelSettings,
        u: TurnUnderstanding,
    ) -> None:
        self.turn, self.agenda, self.memory, self.cfg, self.u = turn, agenda, memory, cfg, u
        self.segment = agenda.segment(turn.ctx.topic)
        self.following = _following(agenda, self.segment.id)

    def part(self, member: str, table: LineSet) -> PrefacePart:
        uses = self.memory.line_uses.get(table.id, 0)
        return PrefacePart(member=member, text=line_for(table, self.cfg.mix, uses), kind=table.id)

    def own(self, table: dict[str, str]) -> PrefacePart:
        return self.part(self.segment.member, table)

    def chair(self, table: dict[str, str]) -> PrefacePart:
        return self.part(self.cfg.chairman, table)

    def board_move(
        self, move: Move, member: str, rule: str, why: str, target: str | None = None
    ) -> BoardMove:
        return BoardMove(
            move=move,
            target=target,
            member=member,
            pressure=PRESSURE[move],
            preface=self.turn.base_move.preface,
            reason=f"{rule}: {why}",
            rule_id=rule,
            for_turn=self.turn.ctx.turn_id,
        )

    def decision(
        self,
        move: BoardMove,
        node: DirectorNode,
        *,
        preface: Sequence[PrefacePart] = (),
        seed: tuple[int, AgendaSeed] | None = None,
        target: AgendaSegment | None = None,
        **fields: Any,
    ) -> PanelDecision:
        now = target or self.segment
        avoid = dict.fromkeys((*self.memory.avoid_terms, *self.u.avoid_terms))
        parts = tuple(preface)
        # A close is the last line. On every other reply, clear slang gets one sentence
        # from the member who heard it, and the planned question still follows.
        if node != "close" and register_hit(self.turn.raw_text):
            parts = (self.own(REGISTER_LINES), *parts)
        return PanelDecision(
            turn_id=self.turn.ctx.turn_id,
            move=move,
            node=node,
            preface=parts,
            seed=seed[1].text if seed else None,
            seed_segment=now.id if seed else None,
            seed_index=seed[0] if seed else None,
            panel=PanelContext(
                segment_id=now.id,
                segment_title=now.title,
                member=now.member,
                seed=seed[1].text if seed else None,
                avoid_terms=tuple(avoid),
                first_handoff=node == "handoff" and not self.memory.line_uses.get(HANDOFF_LINES.id),
            ),
            answered_segment=self.segment.id,
            answered_member=self.segment.member,
            counted=self.u.counted,
            free=self.u.free,
            request_kind=self.u.request_kind,
            request_source=self.u.request_source,
            request_pattern=self.u.request_pattern,
            request_disagree=self.u.request_disagree,
            avoid_terms=self.u.avoid_terms,
            **fields,
        )

    def close(self, reason: CloseReason, before: Sequence[PrefacePart] = ()) -> PanelDecision:
        return self.decision(
            self.board_move(
                Move.move_on, self.cfg.chairman, "D-CLOSE", f"close: {reason}", CLOSE_TARGET
            ),
            "close",
            preface=(*before, self.chair(CLOSE_LINE_BY_REASON[reason])),
            line_complete=True,
            close_reason=reason,
            finished=True,
            director_error="ceiling: the turn cap fired" if reason == "ceiling" else None,
        )

    def handoff(self, before: Sequence[PrefacePart] = ()) -> PanelDecision:
        target = self.following
        assert target is not None  # `advance` and `understand` route the last segment to close
        cap_left = _cap_left(self.turn, self.cfg)
        left = target.budget if cap_left is None else min(target.budget, cap_left)
        name = self.cfg.names[target.member]
        uses = self.memory.line_uses.get(HANDOFF_LINES.id, 0)
        chair = PrefacePart(
            member=self.cfg.chairman,
            text=handoff_line(name, self.cfg.mix, uses),
            kind=HANDOFF_LINES.id,
        )
        return self.decision(
            self.board_move(Move.move_on, target.member, "D-HANDOFF", "next member", target.id),
            "handoff",
            preface=(*before, chair),
            seed=next_seed(target, self.memory, left),
            target=target,
        )

    def advance(self, before: Sequence[PrefacePart], reason: CloseReason) -> PanelDecision:
        """Leave this segment: the next member, or the close."""
        if self.following is None or _cap_hit(self.turn, self.cfg):
            return self.close(reason, before)
        return self.handoff(before)

    def ask_seed(
        self, seed: tuple[int, AgendaSeed], why: str, before: Sequence[PrefacePart] = ()
    ) -> PanelDecision:
        return self.decision(
            self.board_move(Move.next_question, self.segment.member, "D-SEED", why),
            "next_question",
            preface=before,
            seed=seed,
        )

    def forced_seed(self) -> tuple[int, AgendaSeed] | None:
        """The situation, when this counted turn leaves it the segment's last question, or when
        a strategist `move_on` would otherwise leave it unasked (§9.4)."""
        if not self.u.counted or not _must_ask_unasked(self.segment, self.memory):
            return None
        left = _questions_left(self.segment, self.turn, self.cfg)
        if left == 1 or self.turn.base_move.move == Move.move_on:
            return next_seed(self.segment, self.memory, 1)
        return None

    def repeat(self, before: Sequence[PrefacePart], rule: str, why: str) -> PanelDecision:
        return self.decision(
            self.board_move(Move.repeat_question, self.segment.member, rule, why),
            "handle_request",
            preface=before,
            repeat_text=self.memory.last_question,
            line_complete=True,
        )

    def keep_base(self, node: DirectorNode) -> PanelDecision:
        return self.decision(
            self.turn.base_move.model_copy(update={"for_turn": self.turn.ctx.turn_id}), node
        )


def plan_handle_request(p: _Plan) -> PanelDecision:
    kind = p.u.free_kind or p.u.request_kind
    if kind == "end_declined":
        resume = (p.chair(END_DECLINED_LINES),)
        forced = p.forced_seed()
        if forced is not None:
            return p.ask_seed(forced, "the situation must be asked within the budget", resume)
        return p.repeat(resume, "D-RESUME", "end not confirmed; resume")
    if kind == RequestKind.END:
        move = p.board_move(Move.clarify, p.cfg.chairman, "D-END-CONFIRM", "Ruling 52")
        return p.decision(
            move,
            "handle_request",
            preface=(p.chair(END_CONFIRM_LINES),),
            line_complete=True,
            pending_end=True,
        )
    if kind == RequestKind.CHANGE_TOPIC:
        return p.advance((p.own(CHANGE_TOPIC_ACK_LINES),), "agenda")
    if kind == RequestKind.SKIP:
        ack = (p.own(SKIP_ACK_LINES),)
        seed = next_seed(p.segment, p.memory, _questions_left(p.segment, p.turn, p.cfg))
        if seed is None:  # budget spent or no seed left; what is left is forfeited
            return p.advance(ack, "agenda")
        return p.ask_seed(seed, "candidate skipped the question", ack)
    forced = p.forced_seed()
    if forced is not None:
        return p.ask_seed(forced, "the situation must be asked within the budget")
    if kind == RequestKind.REPEAT:
        return p.repeat((p.own(REPEAT_PREFIX_LINES),), "D-REPEAT", "candidate asked for a repeat")
    if kind == RequestKind.CLARIFY_QUESTION:
        move = p.board_move(Move.rephrase_question, p.segment.member, "D-REPHRASE", "asked meaning")
        return p.decision(move, "handle_request")
    return p.keep_base("handle_request")  # garbled or silence: R1's clarify, R3's reassure


def plan_handoff(p: _Plan) -> PanelDecision:
    before = (p.chair(END_DECLINED_LINES),) if p.u.free_kind == "end_declined" else ()
    return p.handoff(before)


def plan_next_question(p: _Plan) -> PanelDecision:
    if _briefing_probe_due(p.turn, p.segment, p.memory, p.cfg):
        move = p.board_move(
            Move.probe, p.segment.member, "D-BRIEF",
            "the answer touches the briefing; one follow-up, then the plan",
        )
        return p.decision(move, "next_question")
    forced = p.forced_seed()
    if forced is not None:
        return p.ask_seed(forced, "the situation must be asked within the budget")
    base = p.turn.base_move
    if base.rule_id == "R8" and base.move == Move.probe and p.memory.probes_on_seed >= 1:
        seed = next_seed(p.segment, p.memory, _questions_left(p.segment, p.turn, p.cfg))
        if seed is not None:
            return p.ask_seed(seed, "second R8 probe on one seed")
    if base.move == Move.fact_challenge:  # no persona owns a card's topic on the panel
        move = base.model_copy(
            update={"member": p.segment.member, "for_turn": p.turn.ctx.turn_id}
        )
        return p.decision(move, "next_question")
    return p.keep_base("next_question")


def plan_close(p: _Plan) -> PanelDecision:
    assert p.u.close_reason is not None
    return p.close(p.u.close_reason)  # "we go on" must not precede the close (CTO_NOTES_15 S1)


_PLANNERS: dict[str, Callable[[_Plan], PanelDecision]] = {
    "handle_request": plan_handle_request,
    "handoff": plan_handoff,
    "next_question": plan_next_question,
    "close": plan_close,
}


def decide_panel(
    turn: DirectorTurn, agenda: Agenda, memory: PanelMemory, cfg: PanelSettings
) -> PanelDecision:
    """The whole policy as one pure function. The graph runs these same pieces."""
    understanding = understand(turn, agenda, memory, cfg)
    return _PLANNERS[understanding.node](_Plan(turn, agenda, memory, cfg, understanding))


# --- Folding finished turns --------------------------------------------------------------------


MAX_NOTE_WORDS = 15  # one stance or reason, in the notes the board remembers
COPY_RUN_WORDS = 6  # this many words in a row from the transcript make a piece a copy of it
_NOTE_WORD = re.compile(r"[\w\u0900-\u097f']+")
_DEVANAGARI = re.compile(r"[\u0900-\u097f]")
UNKNOWN_NOTE = "Said honestly that they did not know."


def _note_words(text: str) -> list[str]:
    return [w.casefold() for w in _NOTE_WORD.findall(text)]


def copies_speech(piece: str, raw: str) -> bool:
    """True when `piece` repeats a run of `COPY_RUN_WORDS` or more words of the transcript.

    A note is a paraphrase by the listener (listener.v4). A copied run may carry a speech
    recognition error, and the board would later say it back as its own memory of the candidate.
    The cost is that an English answer paraphrased very closely loses that note: a thinner memory
    is the safer side of that trade."""
    mine, theirs = _note_words(piece), _note_words(raw)
    if len(mine) < COPY_RUN_WORDS or len(theirs) < COPY_RUN_WORDS:
        return False
    runs = {tuple(theirs[i : i + COPY_RUN_WORDS]) for i in range(len(theirs) - COPY_RUN_WORDS + 1)}
    return any(
        tuple(mine[i : i + COPY_RUN_WORDS]) in runs for i in range(len(mine) - COPY_RUN_WORDS + 1)
    )


def _gist(done: CompletedTurn) -> str:
    """The note the board keeps of one answered question (CTO_NOTES_06 §5, P5 quality rules).

    Empty for a fallback analysis, a garbled one, and a turn that was only a request (a request
    is not what the candidate thinks). Every piece must be English, in the listener's own words
    (no Devanagari, no run copied from the transcript), at most `MAX_NOTE_WORDS` long and new. An
    honest "I do not know" is kept as a note, since it tells the next member what not to press on.
    The whole note is capped at `MAX_GIST_WORDS`. Raw text never gets in."""
    analysis, d = done.analysis, done.decision
    if analysis.is_fallback or analysis.garbled:
        return ""
    if d.request_kind in ("skip", "change_topic") and not analysis.reasons:
        return ""
    kept: list[str] = []
    seen: set[str] = set()
    for piece in [analysis.stance, *analysis.reasons[:MAX_GIST_REASONS]]:
        text = " ".join(" ".join(piece.split()[:MAX_NOTE_WORDS]).split())
        key = text.casefold()
        if not text or key in seen or _DEVANAGARI.search(text) or copies_speech(text, done.raw):
            continue
        seen.add(key)
        kept.append(text)
    if analysis.honest_unknown and not any("know" in k.casefold() for k in kept):
        kept.insert(0, UNKNOWN_NOTE)
    return " ".join(" ".join(kept).split()[:MAX_GIST_WORDS])


def _keeps_last_question(done: CompletedTurn) -> bool:
    """True when this turn's line is not a new question, so `repeat` must not replay it."""
    d = done.decision
    settling = d.move.move == Move.reassure and done.analysis.word_count == 0  # after silence
    return d.free or d.finished or settling or d.move.move in FIXED_LINE_MOVES


def _count_line_uses(memory: PanelMemory, d: PanelDecision) -> dict[str, int] | None:
    """The use counts after this decision's fixed lines, or None when it spoke none from a table.
    A generic clarify is said by the actor module, not by the director, so it is counted here."""
    kinds = [p.kind for p in d.preface if p.kind]
    if d.move.move == Move.clarify and not d.line_complete:
        kinds.append(CLARIFY_PANEL_LINES.id)
    if not kinds:
        return None
    uses = dict(memory.line_uses)
    for kind in kinds:
        uses[kind] = uses.get(kind, 0) + 1
    return uses


def _fold_one(memory: PanelMemory, done: CompletedTurn) -> PanelMemory:
    d = done.decision
    update: dict[str, Any] = {
        "committed_turns": [*memory.committed_turns, d.turn_id],
        "pending_end": d.pending_end,
    }
    if d.counted:
        note = TurnNote(
            segment_id=d.answered_segment,
            member=d.answered_member,
            question=memory.last_question,
            gist=_gist(done),
        )
        update["notes"] = [*memory.notes, note]
    uses = _count_line_uses(memory, d)
    if uses is not None:
        update["line_uses"] = uses
    if d.avoid_terms:
        update["avoid_terms"] = list(dict.fromkeys((*memory.avoid_terms, *d.avoid_terms)))
    if d.seed is not None and d.seed_segment is not None and d.seed_index is not None:
        asked = dict(memory.asked_seeds)
        asked[d.seed_segment] = [*asked.get(d.seed_segment, []), d.seed_index]
        update["asked_seeds"] = asked
        update["probes_on_seed"] = 0
        update["last_question"] = done.asked or d.seed
    elif done.asked and not _keeps_last_question(done):
        update["last_question"] = done.asked
    if d.move.move == Move.probe and d.move.rule_id in {"R8", "D-BRIEF"}:
        update["probes_on_seed"] = memory.probes_on_seed + 1
    return memory.model_copy(update=update)


def fold_turns(
    memory: PanelMemory, completed: Sequence[CompletedTurn]
) -> tuple[PanelMemory, list[PanelRequest], list[PanelDecision]]:
    """Fold finished turns, each once. Returns the memory and what is new for the two logs."""
    requests: list[PanelRequest] = []
    decisions: list[PanelDecision] = []
    for done in completed:
        d = done.decision
        if d.turn_id in memory.committed_turns:
            continue
        memory = _fold_one(memory, done)
        decisions.append(d)
        if d.request_kind:
            requests.append(
                PanelRequest(
                    turn_id=d.turn_id,
                    kind=d.request_kind,
                    source=d.request_source,
                    pattern_id=d.request_pattern,
                    avoid_terms=d.avoid_terms,
                )
            )
    return memory, requests, decisions


# --- The graph ---------------------------------------------------------------------------------


class DirectorState(TypedDict, total=False):
    agenda: Agenda  # replaced whole
    cfg: PanelSettings
    memory: PanelMemory
    requests: Annotated[list[PanelRequest], operator.add]  # append-only, per finished turn
    decisions: Annotated[list[PanelDecision], operator.add]  # append-only, for audit
    # Per-invoke inputs.
    turn: DirectorTurn | None
    completed: list[CompletedTurn]
    understanding: TurnUnderstanding | None
    # Output.
    decision: PanelDecision | None


async def commit_last(state: DirectorState) -> dict[str, Any]:
    memory, requests, decisions = fold_turns(state["memory"], state.get("completed", []))
    return {"memory": memory, "requests": requests, "decisions": decisions}


async def understand_turn(state: DirectorState) -> dict[str, Any]:
    turn = state["turn"]
    assert turn is not None
    return {"understanding": understand(turn, state["agenda"], state["memory"], state["cfg"])}


def _plan_of(state: DirectorState) -> _Plan:
    turn, u = state["turn"], state["understanding"]
    assert turn is not None and u is not None
    return _Plan(turn, state["agenda"], state["memory"], state["cfg"], u)


def _node(name: str):
    async def run(state: DirectorState) -> dict[str, Any]:
        return {"decision": _PLANNERS[name](_plan_of(state))}

    return run


def _after_commit(state: DirectorState) -> str:
    return "understand_turn" if state.get("turn") is not None else END


def _route(state: DirectorState) -> str:
    understanding = state["understanding"]
    assert understanding is not None
    return understanding.node


def build_director_graph() -> StateGraph:
    builder = StateGraph(DirectorState)
    builder.add_node("commit_last", commit_last)
    builder.add_node("understand_turn", understand_turn)
    for name in _PLANNERS:
        builder.add_node(name, _node(name))
        builder.add_edge(name, END)
    builder.add_edge(START, "commit_last")
    builder.add_conditional_edges("commit_last", _after_commit, ["understand_turn", END])
    builder.add_conditional_edges("understand_turn", _route, list(_PLANNERS))
    return builder


def compile_director_graph(checkpointer: Any = None):
    """A crashed voice session cannot resume, so `InMemorySaver` is all that is needed; it holds
    the type allow-list, and a thread lasts the session (CTO_NOTES_06 §2)."""
    saver = checkpointer if checkpointer is not None else InMemorySaver(serde=make_serde())
    return build_director_graph().compile(checkpointer=saver, name="director")


_SHARED_GRAPH: Any = None


def _shared_graph() -> Any:
    global _SHARED_GRAPH
    if _SHARED_GRAPH is None:
        _SHARED_GRAPH = compile_director_graph()
    return _SHARED_GRAPH


class PanelDirector:
    """One session's director: runs the graph, and survives it breaking.

    `queue_completed` is sync because `hooks.apply` is. Queued turns stay queued until a graph run
    succeeds, and the fold ignores turn ids it has seen, so a failure loses and doubles nothing.
    """

    def __init__(
        self,
        session_id: str,
        agenda: Agenda,
        cfg: PanelSettings,
        *,
        opener_question: str | None = None,
        graph: Any = None,
    ) -> None:
        self.session_id = session_id
        self.agenda = agenda
        self.cfg = cfg
        self.memory = opening_memory(agenda, opener_question)
        self.requests: list[PanelRequest] = []
        self.decisions: list[PanelDecision] = []
        self._graph = graph if graph is not None else _shared_graph()
        self._queued: list[CompletedTurn] = []
        self._seeded = False
        self._config = {"configurable": {"thread_id": session_id}}
        # This sitting's news notes. None keeps the file briefing for a follow-up.
        self.briefing: tuple[AffairsNote, ...] | None = None

    def queue_completed(self, done: CompletedTurn) -> None:
        self._queued.append(done)

    def _input(self, turn: DirectorTurn | None) -> dict[str, Any]:
        values: dict[str, Any] = {
            "agenda": self.agenda,
            "cfg": self.cfg,
            "turn": turn,
            "completed": list(self._queued),
            "understanding": None,
            "decision": None,
        }
        if not self._seeded:
            values["memory"] = self.memory
        return values

    def _accept(self, out: dict[str, Any]) -> None:
        self._seeded = True
        self._queued.clear()
        self.memory = out["memory"]
        self.requests = list(out.get("requests", []))
        self.decisions = list(out.get("decisions", []))

    async def decide_turn(self, turn: DirectorTurn) -> PanelDecision:
        """Never raises. A broken graph falls back to the pure function, so a request is still
        honoured; a broken agenda falls back to the strategist's move. Both are `director_error`."""
        token = _SITTING_BRIEFING.set(self.briefing)
        try:
            return await self._decide(turn)
        finally:
            _SITTING_BRIEFING.reset(token)

    async def _decide(self, turn: DirectorTurn) -> PanelDecision:
        try:
            out = await self._graph.ainvoke(self._input(turn), self._config)
        except Exception as exc:  # the fallback is the point: this must not stop an interview
            return self._fallback(turn, exc)
        self._accept(out)
        decision = out["decision"]
        assert decision is not None
        return decision

    def _fallback(self, turn: DirectorTurn, exc: Exception) -> PanelDecision:
        error = f"{type(exc).__name__}: {exc}"
        memory, _, _ = fold_turns(self.memory, self._queued)  # read only; the queue is kept
        try:
            decision = decide_panel(turn, self.agenda, memory, self.cfg)
        except Exception as pure_exc:  # a bad segment id breaks the pure function too
            return _last_resort(turn, f"{error}; pure: {type(pure_exc).__name__}: {pure_exc}")
        return decision.model_copy(update={"director_error": error})

    async def finish(self) -> PanelMemory:
        """Fold the final turn, then delete the thread (`hooks.report()`, CTO_NOTES_06 §2)."""
        try:
            self._accept(await self._graph.ainvoke(self._input(None), self._config))
        except Exception:  # the report must still come; fold here instead
            self.memory, requests, decisions = fold_turns(self.memory, self._queued)
            self.requests.extend(requests)
            self.decisions.extend(decisions)
            self._queued.clear()
        try:
            await self._graph.checkpointer.adelete_thread(self.session_id)
        except Exception:  # a leaked thread must not cost the candidate the report
            logger.warning("director: thread %s not deleted", self.session_id)
        return self.memory


def _last_resort(turn: DirectorTurn, error: str) -> PanelDecision:
    """No agenda reading works: keep the strategist's move, counted, so the interview goes on."""
    topic = turn.ctx.topic
    member = turn.base_move.member
    return PanelDecision(
        turn_id=turn.ctx.turn_id,
        move=turn.base_move.model_copy(update={"for_turn": turn.ctx.turn_id}),
        node="base",
        panel=PanelContext(segment_id=topic, segment_title=topic, member=member),
        answered_segment=topic,
        answered_member=member,
        counted=True,
        director_error=error,
    )
