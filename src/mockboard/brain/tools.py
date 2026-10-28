"""Forced-tool definitions for Nova, plus the Pydantic models that validate their inputs.

The JSON schemas are written by hand, not generated from Pydantic, because generated
schemas use `$defs` and `anyOf` for optional fields, which breaks the flat-schema rule.
A unit test checks that every schema and its model agree on field names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mockboard.brain.llm import ToolSpec
from mockboard.brain.schemas import Balance, ClaimKind, Dimension, ModelVerdict


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
    "librarian": (LIBRARIAN_TOOL, LibrarianOutput),
    "judge": (JUDGE_TOOL, JudgeOutput),
    "motion_screen": (MOTION_SCREEN_TOOL, MotionScreenOutput),
    "case_file": (CASE_FILE_TOOL, CaseFileOutput),
}
