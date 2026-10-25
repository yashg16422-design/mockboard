"""Schema for the scripted text set (60 answers: 20 planted errors, 20 one-sided, 20 balanced).

One JSON object per line. Every item says which split it belongs to; the held-out split is
only run at milestones (ENGINEERING.md §5), and the runner refuses it unless asked.

Each expected claim carries a `label`:
* "true": the owner has checked it against a card; challenging it is a FALSE CORRECTION.
* "planted_error": deliberately wrong against `card_id`; catching it counts toward recall.
* "unchecked": a claim the listener should extract but that no card covers (F1 only).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mockboard.brain.schemas import ClaimKind


class ItemClass(StrEnum):
    planted_error = "planted_error"
    one_sided = "one_sided"
    balanced = "balanced"


class Split(StrEnum):
    dev = "dev"
    heldout = "heldout"


class MoveClass(StrEnum):
    """Coarse move classes the eval scores (the nine moves collapse into three)."""

    challenge = "challenge"  # fact_challenge (on the following turn: challenges are delayed)
    push_back = "push_back"  # counter, steelman
    follow_up = "follow_up"  # probe, pressure, move_on, reassure, clarify, interrupt


CLASS_TO_MOVE: dict[ItemClass, MoveClass] = {
    ItemClass.planted_error: MoveClass.challenge,
    ItemClass.one_sided: MoveClass.push_back,
    ItemClass.balanced: MoveClass.follow_up,
}


class ClaimLabel(StrEnum):
    true = "true"
    planted_error = "planted_error"
    unchecked = "unchecked"


class ExpectedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_en: str
    kind: ClaimKind
    value: float | None = None
    unit: str | None = None
    year: int | None = None
    label: ClaimLabel = ClaimLabel.unchecked
    card_id: str | None = None

    @model_validator(mode="after")
    def _card_for_checked(self) -> ExpectedClaim:
        if self.label != ClaimLabel.unchecked and not self.card_id:
            raise ValueError(
                f"a {self.label.value!r} claim needs the card_id it was checked against"
            )
        return self


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    split: Split
    item_class: ItemClass
    topic: str
    question: str
    answer: str
    expected_claims: list[ExpectedClaim] = Field(default_factory=list)
    expected_move_class: MoveClass | None = None  # defaults from item_class
    example: bool = False  # True = illustrative example, not part of the frozen golden set
    notes: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> EvalItem:
        if self.expected_move_class is None:
            self.expected_move_class = CLASS_TO_MOVE[self.item_class]
        planted = [c for c in self.expected_claims if c.label == ClaimLabel.planted_error]
        if self.item_class == ItemClass.planted_error and not planted:
            raise ValueError(f"{self.id}: a planted_error item needs a planted_error claim")
        if self.item_class != ItemClass.planted_error and planted:
            raise ValueError(f"{self.id}: only planted_error items may plant errors")
        return self
