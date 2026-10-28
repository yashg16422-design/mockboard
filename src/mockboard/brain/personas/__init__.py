"""Board personas are data (YAML files in this folder), not code.

Each persona has a Timbre voice placeholder; the owner fills the real voice id after
running `probes/timbre_probe.py` and listening to the candidates.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from mockboard.brain.schemas import Persona

PERSONA_DIR = Path(__file__).parent


def load_personas(directory: Path = PERSONA_DIR) -> dict[str, Persona]:
    personas: dict[str, Persona] = {}
    for path in sorted(directory.glob("*.yaml")):
        persona = Persona.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        personas[persona.id] = persona
    if not any(p.role == "chairman" for p in personas.values()):
        raise ValueError("persona pack needs exactly one chairman")
    return personas


def chairman(personas: dict[str, Persona]) -> Persona:
    return next(p for p in personas.values() if p.role == "chairman")


def member_for_topic(personas: dict[str, Persona], topic: str) -> Persona:
    """The member who owns a topic; the chairman if nobody does."""
    for p in personas.values():
        if p.role != "chairman" and topic in p.topics:
            return p
    return chairman(personas)


def render_persona_pack(
    personas: dict[str, Persona], heading: str = "## The interview board (persona pack)"
) -> str:
    lines = [heading]
    for p in personas.values():
        lines.append(
            f"- id={p.id} | {p.name}, {p.title} | role={p.role} | topics={', '.join(p.topics)}"
            f" | style: {p.style}"
        )
    return "\n".join(lines)
