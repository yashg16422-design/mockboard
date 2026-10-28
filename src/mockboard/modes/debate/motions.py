"""Debate motions and their case files (CTO_NOTES_04 §2 "Case file", §5).

Curated motions (`data/motions/curated.json`) ship with a hand-written case file for BOTH
sides, because the user picks For or Against and the board argues the other side. Each
argument has an id ("F1".."F4" for the motion, "A1".."A4" against it), so the brain can track
which of the opponent's points are standing, answered or conceded without any model call.

Integrity rules enforced by validation, not by review alone:

* a motion is at most 120 characters and starts with "This House";
* case-file arguments are qualitative: no digit and no number word (`numbers.py`);
* `card_ids` may only point at cards whose topic is one of the motion's `card_topics`.

Custom motions (behind FEATURE_CUSTOM_MOTIONS, default off) are screened in `screen.py` and
get their case file from `case_file.py`.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mockboard.brain.cards import CardSet
from mockboard.config import REPO_ROOT
from mockboard.modes.debate.numbers import mentions_number

CURATED_PATH = REPO_ROOT / "data" / "motions" / "curated.json"
MAX_MOTION_CHARS = 120
_HOUSE = re.compile(r"^this house\b", re.IGNORECASE)


class Side(StrEnum):
    """Which side of the motion the USER argues. The opponent takes the other one."""

    for_ = "for"
    against = "against"

    @property
    def other(self) -> Side:
        return Side.against if self is Side.for_ else Side.for_


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaseArgument(_Frozen):
    """One opponent argument: a claim, the reason it follows (warrant), matching keywords."""

    id: str = Field(pattern=r"^[FA][1-9]$")
    claim: str = Field(min_length=8, max_length=160)
    warrant: str = Field(min_length=8, max_length=220)
    # Words and short phrases (English and Hinglish) that signal the user is talking about
    # this argument. Used by the offline fake listener and the soft restate check only.
    keywords: list[str] = Field(min_length=1, max_length=10)
    # Optional fact cards the opponent may quote a figure from (motions 5 and 6 only).
    card_ids: list[str] = Field(default_factory=list)

    @field_validator("claim", "warrant")
    @classmethod
    def _qualitative(cls, v: str) -> str:
        if mentions_number(v):
            raise ValueError(f"case-file arguments are qualitative; no figures allowed: {v!r}")
        return v


class CaseFile(_Frozen):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    for_: list[CaseArgument] = Field(alias="for", min_length=3, max_length=4)
    against: list[CaseArgument] = Field(min_length=3, max_length=4)

    @model_validator(mode="after")
    def _ids(self) -> CaseFile:
        for prefix, args in (("F", self.for_), ("A", self.against)):
            ids = [a.id for a in args]
            if ids != [f"{prefix}{i}" for i in range(1, len(args) + 1)]:
                raise ValueError(f"case ids must be {prefix}1..{prefix}{len(args)} in order: {ids}")
        return self

    def side(self, side: Side) -> list[CaseArgument]:
        return list(self.for_ if side is Side.for_ else self.against)


class Motion(_Frozen):
    number: int | None = None  # 1-13 for curated motions
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    text: str = Field(max_length=MAX_MOTION_CHARS)
    title: str = Field(max_length=60)
    category: str
    card_topics: list[str] = Field(default_factory=list)
    case: CaseFile
    curated: bool = True

    @field_validator("text")
    @classmethod
    def _house(cls, v: str) -> str:
        if not _HOUSE.match(v.strip()):
            raise ValueError("a motion starts with 'This House'")
        return v.strip()

    def opponent_case(self, user_side: Side) -> list[CaseArgument]:
        """The case the board argues: the side the user did NOT pick."""
        return self.case.side(user_side.other)

    def argument(self, user_side: Side, arg_id: str) -> CaseArgument | None:
        return next((a for a in self.opponent_case(user_side) if a.id == arg_id), None)


# A motion that starts with one of these is a proposal ("This House would ..."); anything
# else is treated as a belief ("This House believes ...").
_PROPOSAL_VERBS = frozenset(
    [
        "ban", "make", "replace", "build", "allow", "abolish", "introduce", "legalise", "legalize",
        "scrap", "end", "give", "require", "fund", "cut", "raise", "lower", "stop", "start",
        "take", "choose", "teach", "close", "open", "limit", "tax", "reward",
    ]
)


def normalise_motion(text: str) -> str:
    """Collapse whitespace and make it a "This House ..." sentence ending with a full stop.

    "ban homework" -> "This House would ban homework."; "Online exams are a disaster" ->
    "This House believes online exams are a disaster." Length is checked by the screen."""
    t = re.sub(r"\s+", " ", text or "").strip().strip("\"'“”")
    if not t:
        return ""
    if not _HOUSE.match(t):
        verb = "would" if t.split()[0].lower() in _PROPOSAL_VERBS else "believes"
        t = f"This House {verb} {t[0].lower()}{t[1:]}"
    else:
        t = "This House" + t[len("this house") :]
    if t[-1] not in ".?!":
        t += "."
    return t


def load_curated_motions(path: Path = CURATED_PATH) -> dict[str, Motion]:
    """The curated motions by id, in their published order (1-13)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    motions = [Motion.model_validate(m) for m in raw["motions"]]
    ids = [m.id for m in motions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate motion id in curated motions")
    return {m.id: m for m in sorted(motions, key=lambda m: m.number or 0)}


def find_motion(motions: dict[str, Motion], key: str) -> Motion:
    """Look a curated motion up by id or by its number ("5")."""
    key = key.strip()
    if key in motions:
        return motions[key]
    if key.isdigit():
        for m in motions.values():
            if m.number == int(key):
                return m
    raise KeyError(f"no curated motion {key!r}; choose 1-{len(motions)} or an id")


def case_card_problems(motion: Motion, cards: CardSet) -> list[str]:
    """Card ids in the case file must exist and belong to the motion's card topics."""
    problems: list[str] = []
    for arg in [*motion.case.for_, *motion.case.against]:
        for cid in arg.card_ids:
            card = cards.get(cid)
            if card is None:
                problems.append(f"{motion.id}/{arg.id}: unknown card {cid}")
            elif card.topic not in motion.card_topics:
                problems.append(
                    f"{motion.id}/{arg.id}: card {cid} is about {card.topic}, "
                    f"not one of {motion.card_topics}"
                )
    return problems
