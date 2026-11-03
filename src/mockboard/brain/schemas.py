"""Typed data models for the brain. Pydantic models only: no I/O, no project imports.

Every LLM output is parsed into one of these models, so a malformed answer fails loudly
at the boundary instead of leaking into the strategist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    """Final, gated verdict for a claim.

    `contradicted` can only be produced when the librarian model AND the deterministic
    numeric check agree (ADR-003). `possible_conflict` is a model-only contradiction on a
    non-numeric claim: it is shown in the report but is never challenged live.
    """

    pending = "pending"
    supported = "supported"
    contradicted = "contradicted"
    possible_conflict = "possible_conflict"
    unverifiable = "unverifiable"


class ModelVerdict(StrEnum):
    """What the librarian model said, before the deterministic gate."""

    supported = "supported"
    contradicted = "contradicted"
    unverifiable = "unverifiable"


class ClaimKind(StrEnum):
    numeric = "numeric"  # a quantity: "tourism is 30% of GDP"
    date = "date"  # a year or date: "the Act came in 2005"
    qualitative = "qualitative"  # a checkable non-numeric fact


class Balance(StrEnum):
    balanced = "balanced"
    one_sided = "one_sided"
    unclear = "unclear"


class Move(StrEnum):
    """Every board move. The UPSC board uses the first nine (PLAN_v2_DELTA §7) plus the three
    panel moves at the end, which only the director emits (CTO_NOTES_06 §7); debate mode adds
    six (CTO_NOTES_04 §2). Each mode's profile lists the moves its rule table may emit
    (`ModeProfile.allowed_moves`), and a test checks every table against its own set."""

    probe = "probe"
    counter = "counter"
    steelman = "steelman"
    fact_challenge = "fact_challenge"
    pressure = "pressure"
    reassure = "reassure"
    move_on = "move_on"
    clarify = "clarify"
    interrupt = "interrupt"
    # Debate mode only.
    opening_statement = "opening_statement"
    closing_statement = "closing_statement"
    rebut = "rebut"
    cross_question = "cross_question"
    concede_point = "concede_point"
    redirect = "redirect"
    # Panel director only (`brain/director.py`).
    repeat_question = "repeat_question"
    rephrase_question = "rephrase_question"
    next_question = "next_question"


class PointStrength(StrEnum):
    """Debate listener: how well a user's point landed (CTO_NOTES_04 §2)."""

    strong = "strong"
    adequate = "adequate"
    weak = "weak"


class Dimension(StrEnum):
    """Judge rubric (orchestrator ruling 6). There is deliberately NO accent, dialect,
    grammar or fluency dimension.

    The first seven follow the personal qualities UPSC publishes for the Personality Test.
    The last two are Mock Board's own practice indicators and are labelled as such in the
    report, never presented as UPSC criteria.
    """

    mental_alertness = "mental_alertness"
    critical_assimilation = "critical_assimilation"
    clear_logical_exposition = "clear_logical_exposition"
    balance_of_judgement = "balance_of_judgement"
    variety_depth_of_interest = "variety_depth_of_interest"
    social_cohesion_leadership = "social_cohesion_leadership"
    intellectual_moral_integrity = "intellectual_moral_integrity"
    factual_accuracy = "factual_accuracy"
    composure = "composure"


UPSC_QUALITIES: tuple[Dimension, ...] = tuple(Dimension)[:7]
PRACTICE_INDICATORS: tuple[Dimension, ...] = (Dimension.factual_accuracy, Dimension.composure)
INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ReportLanguage(StrEnum):
    en = "en"
    hi = "hi"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


class FactCard(_Model):
    """One curated, sourced fact. The librarian may only cite these."""

    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    topic: str
    statement: str
    aliases: list[str] = Field(default_factory=list)
    kind: ClaimKind = ClaimKind.numeric
    value: float | None = None
    unit: str | None = None
    year: int | None = None
    source_title: str | None = None
    source_url: str | None = None
    # Optional short source name the board says aloud ("India Tourism Statistics"), because
    # `source_title` with edition, table and page is too long to speak (ruling 11).
    source_short: str | None = Field(default=None, min_length=1, max_length=60)
    as_of: str | None = None
    synthetic_example: bool = False

    @model_validator(mode="after")
    def _sourced_or_synthetic(self) -> FactCard:
        # Integrity rule: an unsourced card must be flagged as a synthetic test example.
        if not self.synthetic_example and not (self.source_url and self.source_title):
            raise ValueError(
                f"card {self.id!r}: real cards need source_title and source_url; "
                "otherwise mark synthetic_example=true"
            )
        if self.synthetic_example and self.source_url is not None:
            raise ValueError(f"card {self.id!r}: synthetic examples must have source_url null")
        return self


class Claim(_Model):
    """A checkable factual statement extracted from a candidate segment."""

    id: str
    text: str  # as transcribed (may be Hinglish)
    claim_en: str  # normalised English paraphrase
    kind: ClaimKind = ClaimKind.qualitative
    value: float | None = None  # absolute number after lakh/crore scaling
    unit: str | None = None  # canonical unit, see numbers.CANONICAL_UNITS
    year: int | None = None  # year the figure refers to, if stated
    verdict: Verdict = Verdict.pending
    card_id: str | None = None
    # Provenance and audit trail.
    turn_id: str = ""
    segment_id: str = ""
    turn_index: int = 0
    model_verdict: ModelVerdict | None = None
    numeric_check: str | None = None  # "match" | "mismatch" | "incomparable" | None
    verdict_note: str = ""

    @property
    def is_numeric_or_date(self) -> bool:
        return self.kind in (ClaimKind.numeric, ClaimKind.date) and self.value is not None


# ---------------------------------------------------------------------------
# Candidate turn
# ---------------------------------------------------------------------------


class Segment(_Model):
    """One final transcript segment (Prisma sends one per server-VAD segment)."""

    segment_id: str
    segment_index: int
    text: str
    audio_duration_ms: int | None = None


class VoiceSignals(_Model):
    """Side-channel signals (PLAN_v2_DELTA §8). All optional in phase 1. No pitch."""

    response_latency_s: float | None = None  # client playback-ended -> VAD onset
    articulation_rate: float | None = None  # syllables per voiced second
    articulation_rate_vs_baseline: float | None = None  # ratio to the candidate's own baseline
    filler_count: int | None = None  # counted "as transcribed", per-script lists
    long_pause_count: int | None = None
    self_corrections: int | None = None
    was_interrupted_by_board: bool = False


class DebateArgument(_Model):
    """Debate listener: one argument the user made, as a claim and its warrant (the reason it
    follows). An empty warrant means the user asserted without a reason."""

    claim: str
    warrant: str = ""


class Analysis(_Model):
    """Listener output for one segment, stamped with its turn and segment ids.

    The fields after `garbled` are debate-mode only (CTO_NOTES_04 §2). They are optional with
    neutral defaults, so UPSC parsing and behaviour are unchanged.
    """

    turn_id: str = ""
    segment_id: str = ""
    stance: str = ""
    reasons: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    balance: Balance = Balance.unclear
    honest_unknown: bool = False
    # The candidate admits an earlier statement or figure was wrong ("shayad mera figure
    # galat tha"). The board acknowledges this before its next move (ruling 11).
    concedes_error: bool = False
    garbled: bool = False
    word_count: int = 0
    is_fallback: bool = False
    # Debate mode only.
    arguments: list[DebateArgument] = Field(default_factory=list)
    rebuts: list[str] = Field(default_factory=list)  # ids of opponent arguments answered
    question_to_opponent: str = ""
    concedes_point: bool = False  # the user grants one of the opponent's points
    point_strength: PointStrength | None = None
    off_limits: bool = False  # parties, leaders, elections, or attacks on groups
    # Panel mode only (P5, CTO_NOTES_06 §3): what the panel listener heard the candidate ask of the
    # board, one of `tools.CANDIDATE_REQUESTS`. A vote, never a decision: the director weighs it.
    candidate_request: str = "none"
    # True when any segment of the turn got no analysis (a merged turn is `is_fallback` only when
    # all did). A listener that failed on a segment has not said "none" about it, so the weak-
    # pattern veto must not count its silence (Ruling 56).
    any_fallback: bool = False

    @property
    def is_strong(self) -> bool:
        """A deterministic definition of a strong answer, used by the strategist."""
        return (
            self.balance == Balance.balanced
            and len(self.reasons) >= 2
            and not self.garbled
            and not self.honest_unknown
        )


class CandidateTurn(_Model):
    turn_id: str
    turn_index: int
    topic: str
    question: str
    segments: list[Segment] = Field(default_factory=list)
    voice_signals: VoiceSignals = Field(default_factory=VoiceSignals)
    analysis: Analysis | None = None

    @property
    def transcript(self) -> str:
        return " ".join(s.text for s in self.segments).strip()


class LibrarianVote(_Model):
    """The librarian model's opinion on one claim, keyed by content (not by turn).

    It carries no turn or segment ids on purpose: that makes it safe to cache by the
    normalised claim hash plus card-set version.
    """

    claim_key: str
    model_verdict: ModelVerdict
    card_id: str | None = None
    rationale: str = ""
    is_fallback: bool = False
    note: str = ""


class OpponentArg(_Model):
    """One argument in the opponent's case file, as the brain tracks it (debate mode)."""

    id: str
    claim: str
    keywords: list[str] = Field(default_factory=list)


class DebateContext(_Model):
    """Debate-mode state the strategist and listener need, frozen per turn like the rest of
    `StrategyContext`. Everything here is plain data; the rules stay pure."""

    motion: str
    user_side: Literal["for", "against"]
    opponent_args: list[OpponentArg]  # the case file, strongest first
    conceded_ids: list[str] = Field(default_factory=list)  # opponent args conceded (D8)
    answered_ids: list[str] = Field(default_factory=list)  # opponent args the user rebutted
    pressed_counts: dict[str, int] = Field(default_factory=dict)  # times each arg was used
    last_opponent_arg: str | None = None  # the arg the opponent's last move pressed
    cross_questioned_ids: list[str] = Field(default_factory=list)  # at most once per point
    card_topics: list[str] = Field(default_factory=list)  # the motion's fact-card topics
    steelman_used: bool = False  # "make the best case for MY side": once per debate

    @property
    def standing(self) -> list[OpponentArg]:
        return [a for a in self.opponent_args if a.id not in self.conceded_ids]


class StrategyContext(_Model):
    """A snapshot of session counters, taken by the controller at the start of each turn."""

    # The session's mode (ADR-009): the brain picks the rule table and prompts from it.
    mode: str = "upsc_interview"
    turn_id: str
    turn_index: int
    topic: str
    next_topic: str | None = None
    turns_in_topic: int = 0
    max_turns_in_topic: int = 4
    strong_streak: int = 0
    challenged_claim_ids: list[str] = Field(default_factory=list)
    last_move: Move | None = None
    # Honesty and anti-whiplash counters for the current topic (ruling 11).
    honest_unknowns_in_topic: int = 0  # earlier honest "I don't know" turns in this topic
    pressure_in_topic: int = 0  # `pressure` moves already used in this topic
    voice_signals: VoiceSignals = Field(default_factory=VoiceSignals)
    # Debate mode only (None in UPSC mode).
    debate: DebateContext | None = None


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


Preface = Literal["acknowledge"]


class BoardMove(_Model):
    move: Move
    target: str | None = None  # claim id for fact_challenge, else a short focus string
    member: str  # persona id
    pressure: int = Field(default=1, ge=0, le=3)
    # Something the actor says BEFORE the move. "acknowledge" = a short, kind recognition
    # that the candidate conceded an error. A field on the move, not a tenth move, so the
    # nine moves stay as PLAN_v2_DELTA §7 lists them (ruling 11).
    preface: Preface | None = None
    reason: str  # "<rule id>: <why>", shown in the 'show your work' panel
    rule_id: str = ""
    for_turn: str = ""  # turn id this move was computed for (stale-result guard)


class Persona(_Model):
    id: str
    name: str
    title: str
    role: str  # "chairman" | "member"
    topics: list[str] = Field(default_factory=list)
    voice_id: str  # placeholder until the Timbre probe picks one
    voice_candidates: list[str] = Field(default_factory=list)
    language: str = "hi-en"
    style: str
    # The member's field, in plain words, for the panel actor's agenda item (CTO_NOTES_06 §6).
    # Empty for the old UPSC board and debate personas; `render_persona_pack` does not print it.
    domain: str = ""
    # How this member holds the room (actor.v7). Empty on the old board and on debate.
    # quiet: few words, view stays hidden. pressing: one consequence, then stop.
    # personal: what this means for the candidate. steady: the district, nothing else.
    # chair: open, hand over, close. Does not ask a member's question.
    manner: str = ""
    # Short acknowledgements for the voice loop's masking filler (played if the actor's first
    # sentence is late). Not used in text mode yet.
    filler_lines: list[str] = Field(default_factory=list)
    # Ruling 14: whole lines this member says INSTEAD of the actor's line when the actor call
    # fails or misses its budget ("Ek minute... aap apni baat jaari rakhiye"). Data, so they
    # can be synthesised once and cached as audio.
    fallback_lines: list[str] = Field(default_factory=list)

    @field_validator("manner")
    @classmethod
    def _manner(cls, v: str) -> str:
        allowed = {"", "quiet", "pressing", "personal", "steady", "chair"}
        if v not in allowed:
            raise ValueError(f"manner must be one of {sorted(allowed)}")
        return v


class CandidateForm(_Model):
    """The mini application form (orchestrator ruling 5), DAF-style but minimal.

    It collects NO names or identifiers (no name, roll number, phone, email, college). Free
    text fields are length-limited so they cannot carry a CV.
    """

    home_state: str = Field(min_length=2, max_length=60)
    district: str | None = Field(default=None, max_length=60)
    degree_subject: str = Field(min_length=2, max_length=80)
    hobbies: list[str] = Field(default_factory=list, max_length=3)
    optional_subject: str | None = Field(default=None, max_length=80)
    work_experience: str | None = Field(default=None, max_length=200)
    interview_language: str = Field(default="hinglish", pattern=r"^(hinglish|hindi|english)$")

    @field_validator("hobbies")
    @classmethod
    def _short_hobbies(cls, v: list[str]) -> list[str]:
        cleaned = [h.strip() for h in v if h.strip()]
        if any(len(h) > 60 for h in cleaned):
            raise ValueError("each hobby must be at most 60 characters")
        return cleaned

    def as_prompt_lines(self) -> str:
        lines = [f"home_state: {self.home_state}", f"degree_subject: {self.degree_subject}"]
        if self.district:
            lines.append(f"district: {self.district}")
        if self.hobbies:
            lines.append("hobbies: " + ", ".join(self.hobbies))
        if self.optional_subject:
            lines.append(f"optional_subject: {self.optional_subject}")
        if self.work_experience:
            lines.append(f"work_experience: {self.work_experience}")
        lines.append(f"interview_language: {self.interview_language}")
        return "\n".join(lines)


class AgendaSeed(_Model):
    """One open-ended question a member can open from (CTO_NOTES_06 §4). `must_ask` marks the
    DM/SP situation, which must be asked once unless a request or wrap-up says otherwise."""

    text: str = Field(min_length=1)
    must_ask: bool = False


class AgendaSegment(_Model):
    """One part of the panel. Named `AgendaSegment` because `Segment` already means a transcript
    segment in this module (CTO_NOTES_09 §9 calls this type `Segment`).

    `budget` is data on purpose: a capped session passes smaller budgets (Rulings 47, 49), so no
    code may assume the design's numbers. `aliases` are other spoken forms of the topic, in any
    script, for the cross-script avoid list (P4); missing aliases are allowed.
    """

    id: str = Field(min_length=1)
    member: str = Field(min_length=1)  # persona id
    title: str = Field(min_length=1)
    budget: int = Field(ge=1)  # questions, before the panel moves on
    seeds: list[AgendaSeed] = Field(min_length=1)
    aliases: tuple[str, ...] = Field(default=(), max_length=5)


class Agenda(_Model):
    """The ordered panel for one session. Stored in order, and replaced whole.

    One segment is legal (the replay agenda of CTO_NOTES_06 §8), so this type does not insist on
    five. `brain/agenda.py` holds the content rules.
    """

    segments: list[AgendaSegment] = Field(min_length=1)
    source: Literal["template", "nova"] = "template"

    @field_validator("segments")
    @classmethod
    def _unique_ids(cls, v: list[AgendaSegment]) -> list[AgendaSegment]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate segment id in agenda")
        return v

    @property
    def topics(self) -> list[str]:
        """The value for `SessionState.topics`: segment ids, in order."""
        return [s.id for s in self.segments]

    @property
    def total_budget(self) -> int:
        return sum(s.budget for s in self.segments)

    def segment(self, segment_id: str) -> AgendaSegment:
        for segment in self.segments:
            if segment.id == segment_id:
                return segment
        raise KeyError(segment_id)


class BoardLine(_Model):
    turn_id: str
    member: str
    move: Move
    text: str
    is_fallback: bool = False


class SessionState(_Model):
    """Everything the session controller knows. Lives in memory during a session."""

    session_id: str
    form: CandidateForm | None  # None in debate mode, which has its own setup form
    topics: list[str]
    mode: str = "upsc_interview"  # ADR-009: a session-level setting
    topic_index: int = 0
    turn_index: int = 0
    turns_in_topic: int = 0
    strong_streak: int = 0
    honest_unknowns_in_topic: int = 0
    pressure_in_topic: int = 0
    turns: list[CandidateTurn] = Field(default_factory=list)
    board_lines: list[BoardLine] = Field(default_factory=list)
    moves: list[BoardMove] = Field(default_factory=list)
    claim_ledger: list[Claim] = Field(default_factory=list)
    challenged_claim_ids: list[str] = Field(default_factory=list)
    finished: bool = False

    @property
    def topic(self) -> str:
        return self.topics[min(self.topic_index, len(self.topics) - 1)]


# ---------------------------------------------------------------------------
# Panel director (`brain/director.py`). These live here so `_serde_types` allow-lists them.
# ---------------------------------------------------------------------------

# Requests and transcription faults a session forgives before they count as answers
# (Ruling 61, CTO_NOTES_12 §9.3). 0 gives the strict reading of "messages", with no other change.
PANEL_FREE_TURNS = 4

CloseReason = Literal["agenda", "end_request", "time", "answers", "ceiling"]
DirectorNode = Literal["handle_request", "handoff", "next_question", "close"]


class PrefacePart(_Model):
    """One fixed line and who says it. A preface can have two speakers (CTO_NOTES_14 §7.6)."""

    member: str  # persona id
    text: str
    # The fixed-line table this text came from (`panel_lines.LineSet.id`), so a fold can count how
    # often each table has been used and pick the next wording (Ruling 73 item 3). "" for any text
    # that is not from a table, a question for instance.
    kind: str = ""


class TurnNote(_Model):
    """One answered question, for the panel's memory. `gist` comes from the listener's English
    stance and reasons, never from raw text, and is empty for a fallback (CTO_NOTES_06 §5)."""

    segment_id: str
    member: str
    question: str
    gist: str = ""


class PanelRequest(_Model):
    """One detected candidate request, for the session report (Ruling 51)."""

    turn_id: str
    kind: str
    source: str
    pattern_id: str = ""
    avoid_terms: tuple[str, ...] = ()


class PanelMemory(_Model):
    """What the director remembers. Changes only when a finished turn is folded, so re-deciding
    a reopened turn cannot move any of it (Ruling 31, CTO_NOTES_06 §2)."""

    asked_seeds: dict[str, list[int]] = Field(default_factory=dict)  # segment id -> seed indexes
    last_question: str = ""  # the question itself: no preface, ack, repeat prefix or fallback
    pending_end: bool = False  # the board has asked "end here?" (Ruling 52)
    probes_on_seed: int = 0  # R8 probes since the last seed was asked
    # R1 "please say that again" on this question. A second unclear hearing asks the next
    # question instead of another repeat. Any other folded turn resets it.
    unclear_hearings: int = 0
    # As spoken and unfiltered. P4 applies CTO_NOTES_09 §9 before any of it reaches a prompt.
    avoid_terms: list[str] = Field(default_factory=list)
    notes: list[TurnNote] = Field(default_factory=list)
    committed_turns: list[str] = Field(default_factory=list)  # what makes the fold idempotent
    # How often each fixed-line table has been used (`PrefacePart.kind`), counted when a turn is
    # folded. The next use takes wording number `uses % len(variants)`, so no table repeats a
    # wording back to back. `line_uses["handoff"] == 0` means the first hand-off is still to come.
    line_uses: dict[str, int] = Field(default_factory=dict)


class PanelContext(_Model):
    """What the actor needs to know about the agenda for this line."""

    segment_id: str
    segment_title: str
    member: str
    seed: str | None = None
    avoid_terms: tuple[str, ...] = ()
    # True on the first hand-off of the session only: the one place a member says "Namaste" to the
    # candidate (Ruling 73 item 4). Every later hand-off is a plain thanks.
    first_handoff: bool = False


class PanelSettings(_Model):
    """One session's fixed facts for the director. `names` holds `persona.name` by persona id."""

    chairman: str
    names: dict[str, str]
    mix: str = "hi-en"
    max_answers: int | None = None
    free_turns: int = Field(default=PANEL_FREE_TURNS, ge=0)


class DirectorTurn(_Model):
    """The per-invoke input. Counters come from the session side; only `hooks.apply` moves them."""

    ctx: StrategyContext
    base_move: BoardMove
    analysis: Analysis
    raw_text: str = ""  # the turn's joined transcript, punctuation kept (CTO_NOTES_11 §8)
    answers_used: int = 0
    free_used: int = 0
    wrap_up_due: bool = False


class TurnUnderstanding(_Model):
    """What this turn is and what it costs, before any plan is built."""

    node: DirectorNode
    request_kind: str | None = None
    request_source: str = "none"
    request_pattern: str = ""
    avoid_terms: tuple[str, ...] = ()
    free_kind: str | None = None  # why this turn may be free, whether or not one is left
    counted: bool = False
    free: bool = False
    close_reason: CloseReason | None = None
    change_is_weak: bool = False  # a rule hit was one of the seven weak patterns (Ruling 56)
    request_disagree: bool = False  # a healthy listener said something else than the rules


class PanelDecision(_Model):
    """The director's output for one turn. Applying it is the session side's job."""

    turn_id: str
    move: BoardMove
    node: str  # the graph node that planned it, or "base" for the last-resort fallback
    # Fixed lines spoken before the question, in order.
    preface: tuple[PrefacePart, ...] = ()
    repeat_text: str | None = None  # said verbatim, with no model call
    seed: str | None = None  # the seed this line asks
    seed_segment: str | None = None
    seed_index: int | None = None
    # True when the preface and `repeat_text` are the whole line, so the actor must not speak.
    line_complete: bool = False
    panel: PanelContext
    answered_segment: str
    answered_member: str
    counted: bool = False  # `hooks.apply` adds these, never `decide_turn` (CTO_NOTES_12 §9.5)
    free: bool = False
    close_reason: CloseReason | None = None
    finished: bool = False
    pending_end: bool = False  # the value after this turn
    request_kind: str | None = None
    request_source: str = "none"
    request_pattern: str = ""
    request_disagree: bool = False  # trace only (CTO_NOTES_06 §3, Ruling 53)
    avoid_terms: tuple[str, ...] = ()
    director_error: str | None = None


class CompletedTurn(_Model):
    """A turn `hooks.apply` really finished, queued for the next invoke to fold."""

    analysis: Analysis
    decision: PanelDecision
    # The question the board actually put after this turn. None for a fixed line or a fallback
    # line, so `repeat` can never replay one.
    asked: str | None = None
    # The turn's joined transcript, used only to keep a copied run of it out of the memory note
    # (`director.copies_speech`). It is never written into a note.
    raw: str = ""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class Evidence(_Model):
    turn_id: str
    quote: str


class DimensionScore(_Model):
    """One rubric line: a 1-5 score, or "insufficient_evidence" when the session did not
    show this quality (an honest gap is better than an invented score)."""

    dimension: Dimension
    score: int | Literal["insufficient_evidence"]
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("score")
    @classmethod
    def _range(cls, v: int | str) -> int | str:
        if isinstance(v, int) and not 1 <= v <= 5:
            raise ValueError("score must be 1-5 or 'insufficient_evidence'")
        return v

    @model_validator(mode="after")
    def _evidence_for_numeric_scores(self) -> DimensionScore:
        if isinstance(self.score, int) and not self.evidence:
            raise ValueError(
                f"{self.dimension.value}: a numeric score needs at least one evidence quote"
            )
        return self

    @property
    def is_practice_indicator(self) -> bool:
        return self.dimension in PRACTICE_INDICATORS


class ComposureMoment(_Model):
    turn_id: str
    note: str


class MemberReport(_Model):
    """One panel member's block in the session report (P10, WEB_REQUESTS.md). Built by code."""

    name: str
    segment: str = ""
    notes: str = ""
    questions: list[str] = Field(default_factory=list)
    weakest_question: str = ""
    outline: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class WeakestMoment(_Model):
    """The turn chosen as weakest, by code, never by the model (CTO_NOTES_06 §8)."""

    turn_id: str
    segment: str
    member: str = ""
    question: str
    answer: str = ""
    outline: list[str] = Field(default_factory=list)
    score: int = 0


class PracticeCompare(_Model):
    """Text-only replay comparison (Ruling 48). No audio."""

    before_words: int
    after_words: int
    before_balance: str = ""
    after_balance: str = ""
    after_reasons: list[str] = Field(default_factory=list)
    word_gain: int = 0


class SessionReport(_Model):
    session_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    report_language: ReportLanguage = ReportLanguage.en
    scores: list[DimensionScore]
    claims: list[Claim] = Field(default_factory=list)
    card_sources: dict[str, str] = Field(default_factory=dict)  # card_id -> source or label
    composure_moments: list[ComposureMoment] = Field(default_factory=list)
    moves: list[BoardMove] = Field(default_factory=list)
    model_answer: str = ""
    citation_check_passed: bool = True
    judge_revisions: int = 0
    is_fallback: bool = False
    notes: list[str] = Field(default_factory=list)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    prompt_versions: dict[str, int] = Field(default_factory=dict)
    # The panel's detected candidate requests and how each was found (Ruling 51). Empty for the
    # old board, so its report is unchanged.
    requests: list[PanelRequest] = Field(default_factory=list)
    # P10: empty defaults keep debate and fallback reports in the old shape.
    by_member: list[MemberReport] = Field(default_factory=list)
    weakest: WeakestMoment | None = None
    practice_plan: list[str] = Field(default_factory=list)
    form: CandidateForm | None = None
    # The members' closing talk, built from their notes and the plan. Empty for debate.
    discussion: list[str] = Field(default_factory=list)

    @field_validator("scores")
    @classmethod
    def _one_score_per_dimension(cls, v: list[DimensionScore]) -> list[DimensionScore]:
        dims = [s.dimension for s in v]
        if len(dims) != len(set(dims)):
            raise ValueError("duplicate dimension in scores")
        return v

    @model_validator(mode="after")
    def _complete_unless_fallback(self) -> SessionReport:
        # A fallback report (judge unavailable) carries no invented scores at all.
        if not self.is_fallback and {s.dimension for s in self.scores} != set(Dimension):
            raise ValueError(f"a non-fallback report must cover all {len(Dimension)} dimensions")
        return self

    def score_for(self, dim: Dimension) -> DimensionScore | None:
        return next((s for s in self.scores if s.dimension == dim), None)


# ---------------------------------------------------------------------------
# Debate report (CTO_NOTES_04 §4, DB6)
# ---------------------------------------------------------------------------
#
# Separate types from the UPSC report on purpose: the UPSC `Dimension`, `DimensionScore` and
# `SessionReport` are unchanged, so the UPSC report cannot change shape by accident. The debate
# profile's `rubric` is `tuple(DebateDimension)`, and a test checks they agree.


class DebateDimension(StrEnum):
    """The debate rubric. Like the UPSC rubric, there is NO accent, grammar or fluency score."""

    clarity_structure = "clarity_structure"
    evidence_use = "evidence_use"  # card-consistent claims and honest uncertainty
    rebuttal_quality = "rebuttal_quality"  # engaging the opponent's ACTUAL points
    responsiveness = "responsiveness"  # answering questions directly
    fairness_to_other_side = "fairness_to_other_side"
    composure = "composure"


class DebateDimensionScore(_Model):
    """One debate rubric line: 1-5 with verified evidence, or "insufficient_evidence"."""

    dimension: DebateDimension
    score: int | Literal["insufficient_evidence"]
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("score")
    @classmethod
    def _range(cls, v: int | str) -> int | str:
        if isinstance(v, int) and not 1 <= v <= 5:
            raise ValueError("score must be 1-5 or 'insufficient_evidence'")
        return v

    @model_validator(mode="after")
    def _evidence_for_numeric_scores(self) -> DebateDimensionScore:
        if isinstance(self.score, int) and not self.evidence:
            raise ValueError(
                f"{self.dimension.value}: a numeric score needs at least one evidence quote"
            )
        return self


class RoundScore(_Model):
    """One round of the scoreboard, computed by code from the debate record (never a model)."""

    round: str
    turns: int = Field(ge=0)  # the user's turns in this round
    arguments_made: int = Field(ge=0)  # claim-and-warrant pairs the listener found
    answered: int = Field(ge=0)  # opponent arguments the user answered for the first time
    standing_after: int = Field(ge=0)  # opponent arguments still standing at the round's end
    concessions_won: int = Field(ge=0)  # opponent arguments the opponent conceded (D8)
    figures_questioned: int = Field(ge=0)  # the user's figures the board asked about (D9)


class DebateScoreboard(_Model):
    rounds: list[RoundScore]
    opponent_arguments: int = Field(ge=0)  # the size of the opponent's case file
    arguments_made: int = Field(ge=0)
    answered: int = Field(ge=0)
    standing_at_end: int = Field(ge=0)
    concessions_won: int = Field(ge=0)
    figures_questioned: int = Field(ge=0)


class BestLine(_Model):
    """One verbatim quote from the user's own speech, with the reason it was chosen."""

    turn_id: str
    quote: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    chosen_by: Literal["judge", "code"]


class VerdictLabel(StrEnum):
    carried = "carried_on_argument"
    close = "close_debate"
    opponent_held = "opponent_held_the_floor"
    no_verdict = "no_verdict"  # too little verified evidence, or the judge was unavailable


class VerdictReason(_Model):
    """One reason the Moderator gives. A `strength` or `work_on` reason maps to one rubric
    score; a `scoreboard` reason maps to the code-computed scoreboard."""

    kind: Literal["strength", "work_on", "scoreboard"]
    dimension: DebateDimension | None = None
    score: int | None = None
    text: str

    @model_validator(mode="after")
    def _traceable(self) -> VerdictReason:
        if self.kind != "scoreboard" and (self.dimension is None or self.score is None):
            raise ValueError("a rubric reason must name its dimension and score")
        return self


class DebateVerdict(_Model):
    """The Moderator's verdict, built by template (never written by a model). It judges how
    the debate was argued, never which side of the motion is right."""

    label: VerdictLabel
    headline: str
    reasons: list[VerdictReason] = Field(default_factory=list)
    text: str  # what the Moderator says, in the report language


class DebateReport(_Model):
    mode: Literal["debate"] = "debate"
    session_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    report_language: ReportLanguage = ReportLanguage.en
    motion: str
    user_side: Literal["for", "against"]
    scores: list[DebateDimensionScore]
    scoreboard: DebateScoreboard
    best_line: BestLine | None = None
    verdict: DebateVerdict
    claims: list[Claim] = Field(default_factory=list)
    card_sources: dict[str, str] = Field(default_factory=dict)
    moves: list[BoardMove] = Field(default_factory=list)
    citation_check_passed: bool = True
    judge_revisions: int = 0
    is_fallback: bool = False
    notes: list[str] = Field(default_factory=list)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    prompt_versions: dict[str, int] = Field(default_factory=dict)

    @field_validator("scores")
    @classmethod
    def _one_score_per_dimension(
        cls, v: list[DebateDimensionScore]
    ) -> list[DebateDimensionScore]:
        dims = [s.dimension for s in v]
        if len(dims) != len(set(dims)):
            raise ValueError("duplicate dimension in scores")
        return v

    @model_validator(mode="after")
    def _complete_unless_fallback(self) -> DebateReport:
        if not self.is_fallback and {s.dimension for s in self.scores} != set(DebateDimension):
            raise ValueError(
                f"a non-fallback debate report must cover all {len(DebateDimension)} dimensions"
            )
        if self.is_fallback and self.scores:
            raise ValueError("a fallback debate report carries no scores (none are guessed)")
        return self

    def score_for(self, dim: DebateDimension) -> DebateDimensionScore | None:
        return next((s for s in self.scores if s.dimension == dim), None)
