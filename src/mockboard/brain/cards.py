"""Load fact cards from `data/cards/<topic>.json`, validate them, and render the cards block
that goes into the cached system prompt.

The card-set version is a short hash of the cards' canonical JSON. It is part of every
librarian cache key, so editing a card automatically invalidates cached verdicts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from mockboard.brain.schemas import FactCard


@dataclass(frozen=True)
class CardSet:
    cards: dict[str, FactCard]
    version: str

    def for_topic(self, topic: str) -> list[FactCard]:
        return [c for c in self.cards.values() if c.topic == topic]

    def get(self, card_id: str | None) -> FactCard | None:
        return self.cards.get(card_id) if card_id else None

    @property
    def all_synthetic(self) -> bool:
        return all(c.synthetic_example for c in self.cards.values())


def card_set_version(cards: Iterable[FactCard]) -> str:
    canonical = json.dumps(
        [c.model_dump(mode="json") for c in sorted(cards, key=lambda c: c.id)],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _schema(cards_dir: Path) -> dict | None:
    path = cards_dir / "schema.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_cards(cards_dir: Path, topics: Sequence[str]) -> CardSet:
    """Load and validate every topic file. Raises on schema errors or duplicate ids."""
    schema = _schema(cards_dir)
    cards: dict[str, FactCard] = {}
    for topic in topics:
        path = cards_dir / f"{topic}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        if schema is not None:
            jsonschema.validate(doc, schema)
        if doc.get("topic") != topic:
            raise ValueError(f"{path}: topic field {doc.get('topic')!r} != {topic!r}")
        for raw in doc["cards"]:
            card = FactCard.model_validate({**raw, "topic": topic})
            if card.id in cards:
                raise ValueError(f"duplicate card id {card.id!r} in {path}")
            cards[card.id] = card
    return CardSet(cards=cards, version=card_set_version(cards.values()))


# Built-in cards for tests and for `--fake` runs when data/cards/ has no topic files.
# They are about the fictional "Testland", are flagged synthetic_example, and have no source.
_BUILTIN_RAW: tuple[dict, ...] = (
    {"id": "builtin_tourism_gdp_share", "topic": "tourism", "kind": "numeric",
     "statement": "BUILT-IN TEST CARD: Testland tourism share of GDP is 9 percent in 2023.",
     "aliases": ["Testland tourism GDP share"], "value": 9, "unit": "percent", "year": 2023},
    {"id": "builtin_tourism_arrivals", "topic": "tourism", "kind": "numeric",
     "statement": "BUILT-IN TEST CARD: Testland received 15 lakh foreign tourist arrivals in 2022.",
     "aliases": ["Testland foreign tourist arrivals"], "value": 15, "unit": "lakh visitors",
     "year": 2022},
    {"id": "builtin_tourism_policy_year", "topic": "tourism", "kind": "date",
     "statement": "BUILT-IN TEST CARD: Testland adopted its Test Tourism Policy in the year 2018.",
     "aliases": ["Testland tourism policy year"], "value": 2018, "unit": "year", "year": 2018},
    {"id": "builtin_security_districts", "topic": "internal_security", "kind": "numeric",
     "statement": "BUILT-IN TEST CARD: Testland lists 20 test-affected districts in 2024.",
     "aliases": ["Testland affected districts"], "value": 20, "unit": "districts",
     "year": 2024},
    {"id": "builtin_security_budget", "topic": "internal_security", "kind": "numeric",
     "statement": "BUILT-IN TEST CARD: Testland test security budget is 500 crore rupees in 2025.",
     "aliases": ["Testland security budget"], "value": 500, "unit": "crore inr", "year": 2025},
    {"id": "builtin_security_reform_note", "topic": "internal_security", "kind": "qualitative",
     "statement": "BUILT-IN TEST CARD: Testland created a test community policing scheme.",
     "aliases": ["Testland community policing"]},
)


def builtin_test_cards(topics: Sequence[str] | None = None) -> CardSet:
    """Clearly fake cards (synthetic_example=true, source_url=null) for offline runs."""
    cards = {
        raw["id"]: FactCard.model_validate(
            {**raw, "synthetic_example": True, "source_url": None,
             "source_title": "SYNTHETIC EXAMPLE (built-in, no source)"}
        )
        for raw in _BUILTIN_RAW
        if topics is None or raw["topic"] in topics
    }
    return CardSet(cards=cards, version=card_set_version(cards.values()))


def load_cards_or_builtin(cards_dir: Path, topics: Sequence[str]) -> tuple[CardSet, str]:
    """Load data/cards/<topic>.json when every topic file exists, else the built-in cards.

    Returns (card set, origin label) so callers can say which set they are using.
    """
    if all((cards_dir / f"{t}.json").exists() for t in topics):
        return load_cards(cards_dir, topics), f"data:{cards_dir}"
    return builtin_test_cards(topics), "builtin-test-cards"


def render_card(card: FactCard) -> str:
    parts = [f"[{card.id}] ({card.topic}, {card.kind.value}) {card.statement}"]
    if card.value is not None:
        parts.append(f"value={card.value:g}")
    if card.unit:
        parts.append(f"unit={card.unit}")
    if card.year is not None:
        parts.append(f"year={card.year}")
    if card.aliases:
        parts.append("aliases=" + "; ".join(card.aliases))
    if card.synthetic_example:
        parts.append("SYNTHETIC_TEST_CARD")
    return " | ".join(parts)


def render_cards_block(card_set: CardSet) -> str:
    """The fact-card section of the shared, cached system prompt (stable ordering)."""
    lines = [
        "## Fact cards",
        f"card_set_version: {card_set.version}",
        "These are the ONLY facts you may treat as verified. Each line is one card.",
    ]
    for card in sorted(card_set.cards.values(), key=lambda c: (c.topic, c.id)):
        lines.append(render_card(card))
    return "\n".join(lines)
