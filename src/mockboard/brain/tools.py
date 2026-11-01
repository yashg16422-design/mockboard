"""Forced-tool definitions for Nova, plus the Pydantic models that validate their inputs.

The JSON schemas are written by hand, not generated from Pydantic, because generated
schemas use `$defs` and `anyOf` for optional fields, which breaks the flat-schema rule.
A unit test checks that every schema and its model agree on field names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mockboard.brain.llm import ToolSpec
from mockboard.brain.schemas import (
    Balance,
    ClaimKind,
    DebateDimension,
    Dimension,
    ModelVerdict,
    PointStrength,
)


class _ToolModel(BaseModel):
    # Ignore unknown keys: the model sometimes adds harmless extras; the fields we rely on
    # are still strictly typed.
    model_config = ConfigDict(extra="ignore")


def _blank_to_none(v: object) -> object:
    return None if isinstance(v, str) and not v.strip() else v


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class ListenerClaim(_ToolModel):
    text: str
    claim_en: str
    kind: ClaimKind = ClaimKind.qualitative
    value: float | None = None
    unit: str | None = None
    year: int | None = None

    _blank = field_validator("value", "unit", "year", mode="before")(_blank_to_none)


class ListenerOutput(_ToolModel):
    stance: str = ""
    reasons: list[str] = Field(default_factory=list)
    claims: list[ListenerClaim] = Field(default_factory=list)
    balance: Balance = Balance.unclear
    honest_unknown: bool = False
    concedes_error: bool = False
    garbled: bool = False


LISTENER_TOOL = ToolSpec(
    name="record_analysis",
    description="Record the structured analysis of one segment of the candidate's answer.",
    input_schema={
        "type": "object",
        "properties": {
            "stance": {"type": "string", "description": "Candidate's position, one sentence."},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "claim_en": {"type": "string"},
                        "kind": {"type": "string", "enum": [k.value for k in ClaimKind]},
                        "value": {"type": "number"},
                        "unit": {"type": "string"},
                        "year": {"type": "integer"},
                    },
                    "required": ["text", "claim_en", "kind"],
                },
            },
            "balance": {"type": "string", "enum": [b.value for b in Balance]},
            "honest_unknown": {"type": "boolean"},
            "concedes_error": {"type": "boolean"},
            "garbled": {"type": "boolean"},
        },
        "required": [
            "stance",
            "reasons",
            "claims",
            "balance",
            "honest_unknown",
            "concedes_error",
            "garbled",
        ],
    },
)


# Panel listener (P5, CTO_NOTES_06 §3): the UPSC fields plus the candidate's request to the board,
# as the model heard it. A separate tool and model, so `LISTENER_TOOL` (which the debate tool
# spreads) is not touched. The code decides what the vote is worth (`brain/director.py`).

CANDIDATE_REQUESTS = ("none", "change_topic", "repeat", "clarify_question", "skip", "end")


class PanelListenerOutput(ListenerOutput):
    candidate_request: str = "none"

    @field_validator("candidate_request", mode="before")
    @classmethod
    def _known_request(cls, v: object) -> str:
        """An unknown or odd value is no request, never an error: a request must be one of the five
        kinds the director knows, and everything else is `none`."""
        word = v.strip().lower().replace("-", "_").replace(" ", "_") if isinstance(v, str) else ""
        return word if word in CANDIDATE_REQUESTS else "none"


PANEL_LISTENER_TOOL = ToolSpec(
    name="record_panel_analysis",
    description=(
        "Record the structured analysis of one segment of the candidate's answer, and whether "
        "the candidate is asking the board to do something."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **LISTENER_TOOL.input_schema["properties"],
            "candidate_request": {
                "type": "string",
                "enum": list(CANDIDATE_REQUESTS),
                "description": "What the candidate asks the board to do, or 'none'.",
            },
        },
        "required": [*LISTENER_TOOL.input_schema["required"], "candidate_request"],
    },
)


# Debate listener (CTO_NOTES_04 §2): the UPSC fields plus optional debate fields. A separate
# tool and model, so the UPSC listener's schema and parsing are untouched.


class DebateListenerArgument(_ToolModel):
    claim: str
    warrant: str = ""


class DebateListenerOutput(ListenerOutput):
    arguments: list[DebateListenerArgument] = Field(default_factory=list)
    rebuts: list[str] = Field(default_factory=list)
    question_to_opponent: str = ""
    concedes_point: bool = False
    point_strength: PointStrength | None = None
    off_limits: bool = False

    _blank_strength = field_validator("point_strength", mode="before")(_blank_to_none)


DEBATE_LISTENER_TOOL = ToolSpec(
    name="record_debate_analysis",
    description="Record the structured analysis of one segment of the user's debate speech.",
    input_schema={
        "type": "object",
        "properties": {
            **LISTENER_TOOL.input_schema["properties"],
            "arguments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "warrant": {"type": "string", "description": "Why it follows; ''."},
                    },
                    "required": ["claim", "warrant"],
                },
            },
            "rebuts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ids of the opponent's arguments this segment answers.",
            },
            "question_to_opponent": {"type": "string"},
            "concedes_point": {"type": "boolean"},
            "point_strength": {"type": "string", "enum": [p.value for p in PointStrength]},
            "off_limits": {"type": "boolean"},
        },
        "required": [
            *LISTENER_TOOL.input_schema["required"],
            "arguments",
            "rebuts",
            "question_to_opponent",
            "concedes_point",
            "point_strength",
            "off_limits",
        ],
    },
)


# ---------------------------------------------------------------------------
# Librarian
# ---------------------------------------------------------------------------


class LibrarianOutput(_ToolModel):
    card_id: str | None = None
    verdict: ModelVerdict
    rationale: str = ""

    _blank = field_validator("card_id", mode="before")(_blank_to_none)


LIBRARIAN_TOOL = ToolSpec(
    name="record_verdict",
    description="Record whether one claim is supported, contradicted or unverifiable by the cards.",
    input_schema={
        "type": "object",
        "properties": {
            "card_id": {"type": "string", "description": "Id of the matching card, or ''."},
            "verdict": {"type": "string", "enum": [v.value for v in ModelVerdict]},
            "rationale": {"type": "string"},
        },
        "required": ["card_id", "verdict", "rationale"],
    },
)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


class JudgeScore(_ToolModel):
    dimension: Dimension
    # Flat schema: the tool sends a string ("1".."5" or "insufficient_evidence"), because a
    # union type would break the flat-schema rule. It is converted to int here.
    score: int | Literal["insufficient_evidence"]
    rationale: str

    @field_validator("score", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("score")
    @classmethod
    def _range(cls, v: int | str) -> int | str:
        if isinstance(v, int) and not 1 <= v <= 5:
            raise ValueError("score must be 1-5 or insufficient_evidence")
        return v


class JudgeEvidence(_ToolModel):
    dimension: Dimension
    turn_id: str
    quote: str


class JudgeMoment(_ToolModel):
    turn_id: str
    note: str


class JudgeOutput(_ToolModel):
    scores: list[JudgeScore]
    evidence: list[JudgeEvidence] = Field(default_factory=list)
    composure_moments: list[JudgeMoment] = Field(default_factory=list)
    model_answer: str = ""

    @model_validator(mode="after")
    def _all_dimensions(self) -> JudgeOutput:
        dims = [s.dimension for s in self.scores]
        if sorted(dims) != sorted(Dimension):
            raise ValueError(
                "scores must contain each dimension exactly once: "
                + ", ".join(d.value for d in Dimension)
            )
        return self


JUDGE_TOOL = ToolSpec(
    name="record_report",
    description=(
        "Record the post-interview rubric (UPSC qualities + practice indicators) "
        "with verbatim evidence."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": "string", "enum": [d.value for d in Dimension]},
                        "score": {
                            "type": "string",
                            "enum": ["1", "2", "3", "4", "5", "insufficient_evidence"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["dimension", "score", "rationale"],
                },
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": "string", "enum": [d.value for d in Dimension]},
                        "turn_id": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["dimension", "turn_id", "quote"],
                },
            },
            "composure_moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"turn_id": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["turn_id", "note"],
                },
            },
            "model_answer": {"type": "string"},
        },
        "required": ["scores", "evidence", "composure_moments", "model_answer"],
    },
)

# Debate judge (CTO_NOTES_04 §4, DB6): six debate dimensions plus one "best line" the judge
# picks from the user's own words. The best line is three flat fields, not a nested object,
# to keep the flat-schema rule. A separate tool and model, so the UPSC judge is untouched.


def _coerce_score(v: object) -> object:
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    if isinstance(v, str):
        return v.strip().lower()
    return v


class DebateJudgeScore(_ToolModel):
    dimension: DebateDimension
    score: int | Literal["insufficient_evidence"]
    rationale: str

    _coerce = field_validator("score", mode="before")(_coerce_score)

    @field_validator("score")
    @classmethod
    def _range(cls, v: int | str) -> int | str:
        if isinstance(v, int) and not 1 <= v <= 5:
            raise ValueError("score must be 1-5 or insufficient_evidence")
        return v


class DebateJudgeEvidence(_ToolModel):
    dimension: DebateDimension
    turn_id: str
    quote: str


class DebateBestLineCitation(BaseModel):
    """The judge's best line, shaped like an evidence item so the shared citation check
    (`judge.check_citations`) can verify it and the revision request can list it."""

    model_config = ConfigDict(frozen=True)
    dimension: Literal["best_line"] = "best_line"
    turn_id: str
    quote: str


class DebateJudgeOutput(_ToolModel):
    scores: list[DebateJudgeScore]
    evidence: list[DebateJudgeEvidence] = Field(default_factory=list)
    best_line_turn_id: str = ""
    best_line_quote: str = ""
    best_line_reason: str = ""

    @model_validator(mode="after")
    def _all_dimensions(self) -> DebateJudgeOutput:
        dims = [s.dimension for s in self.scores]
        if sorted(dims) != sorted(DebateDimension):
            raise ValueError(
                "scores must contain each debate dimension exactly once: "
                + ", ".join(d.value for d in DebateDimension)
            )
        return self

    @property
    def best_line(self) -> DebateBestLineCitation | None:
        if not (self.best_line_turn_id.strip() and self.best_line_quote.strip()):
            return None
        return DebateBestLineCitation(
            turn_id=self.best_line_turn_id.strip(), quote=self.best_line_quote
        )

    def citations(self) -> list[DebateJudgeEvidence | DebateBestLineCitation]:
        """Every quote the citation check must find in its turn: evidence and the best line."""
        best = self.best_line
        return [*self.evidence, *([best] if best is not None else [])]


DEBATE_JUDGE_TOOL = ToolSpec(
    name="record_debate_report",
    description=(
        "Record the post-debate rubric with verbatim evidence, and the user's best line."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {
                            "type": "string",
                            "enum": [d.value for d in DebateDimension],
                        },
                        "score": {
                            "type": "string",
                            "enum": ["1", "2", "3", "4", "5", "insufficient_evidence"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["dimension", "score", "rationale"],
                },
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {
                            "type": "string",
                            "enum": [d.value for d in DebateDimension],
                        },
                        "turn_id": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["dimension", "turn_id", "quote"],
                },
            },
            "best_line_turn_id": {"type": "string", "description": "The quote's turn_id."},
            "best_line_quote": {
                "type": "string",
                "description": "One sentence copied exactly from the user's own speech.",
            },
            "best_line_reason": {"type": "string", "description": "Why, in one sentence."},
        },
        "required": [
            "scores",
            "evidence",
            "best_line_turn_id",
            "best_line_quote",
            "best_line_reason",
        ],
    },
)

# ---------------------------------------------------------------------------
# Debate mode: motion screen and custom case file (CTO_NOTES_04 §5, §2)
# ---------------------------------------------------------------------------

SCREEN_DECISIONS = ("allow", "reframe", "refuse")
SCREEN_CATEGORIES = (
    "none", "loaded", "partisan", "religion", "caste", "communal", "group_targeting",
    "private_individual", "court_case", "violence", "self_harm", "sexual", "illegal", "other",
)


class MotionScreenOutput(_ToolModel):
    decision: Literal["allow", "reframe", "refuse"]
    category: str = "other"
    reason: str = ""
    neutral_rewording: str = ""

    @field_validator("decision", "category", mode="before")
    @classmethod
    def _lower(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v


MOTION_SCREEN_TOOL = ToolSpec(
    name="record_motion_screen",
    description="Record whether a custom debate motion is allowed, must be reframed, or refused.",
    input_schema={
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": list(SCREEN_DECISIONS)},
            "category": {"type": "string", "enum": list(SCREEN_CATEGORIES)},
            "reason": {"type": "string", "description": "One short English sentence."},
            "neutral_rewording": {
                "type": "string",
                "description": "For reframe only: a neutral 'This House ...' motion; else ''.",
            },
        },
        "required": ["decision", "category", "reason", "neutral_rewording"],
    },
)


class CaseFileArgument(_ToolModel):
    claim: str
    warrant: str
    keywords: list[str] = Field(default_factory=list)


class CaseFileOutput(_ToolModel):
    arguments: list[CaseFileArgument] = Field(default_factory=list)


CASE_FILE_TOOL = ToolSpec(
    name="record_case_file",
    description="Record three or four qualitative arguments for one side of a debate motion.",
    input_schema={
        "type": "object",
        "properties": {
            "arguments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "warrant": {"type": "string"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["claim", "warrant", "keywords"],
                },
            },
        },
        "required": ["arguments"],
    },
)

ALL_TOOLS: dict[str, tuple[ToolSpec, type[BaseModel]]] = {
    "listener": (LISTENER_TOOL, ListenerOutput),
    "listener_panel": (PANEL_LISTENER_TOOL, PanelListenerOutput),
    "listener_debate": (DEBATE_LISTENER_TOOL, DebateListenerOutput),
    "librarian": (LIBRARIAN_TOOL, LibrarianOutput),
    "judge": (JUDGE_TOOL, JudgeOutput),
    "judge_debate": (DEBATE_JUDGE_TOOL, DebateJudgeOutput),
    "motion_screen": (MOTION_SCREEN_TOOL, MotionScreenOutput),
    "case_file": (CASE_FILE_TOOL, CaseFileOutput),
}
