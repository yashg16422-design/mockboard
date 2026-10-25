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
    """The nine board moves (PLAN_v2_DELTA §7)."""

    probe = "probe"
    counter = "counter"
    steelman = "steelman"
    fact_challenge = "fact_challenge"
    pressure = "pressure"
    reassure = "reassure"
    move_on = "move_on"
    clarify = "clarify"
    interrupt = "interrupt"


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


class Analysis(_Model):
    """Listener output for one segment, stamped with its turn and segment ids."""

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


class StrategyContext(_Model):
    """A snapshot of session counters, taken by the controller at the start of each turn."""

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
    filler_lines: list[str] = Field(default_factory=list)


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


class BoardLine(_Model):
    turn_id: str
    member: str
    move: Move
    text: str
    is_fallback: bool = False


class SessionState(_Model):
    """Everything the session controller knows. Lives in memory during a session."""

    session_id: str
    form: CandidateForm
    topics: list[str]
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
