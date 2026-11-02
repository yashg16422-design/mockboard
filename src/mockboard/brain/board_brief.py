"""The sitting plan is the board's material. Not a fact card, and not a web search.

The moment the form becomes an agenda, each member holds their questions. As answers
are folded into memory, what they heard and what is still ahead are updated from those
notes and from the seeds not yet asked. The closing discussion uses only that. It does
not add a fact, a number, or a line nobody heard.
"""

from __future__ import annotations

from dataclasses import dataclass

from mockboard.brain.schemas import Agenda, PanelMemory

__all__ = ["MemberPacket", "brief_from", "discussion_lines"]


@dataclass(frozen=True)
class MemberPacket:
    """One member's folder: the questions they were given, what they heard, what is still ahead."""

    member: str
    name: str
    segment: str
    questions: tuple[str, ...]
    heard: tuple[str, ...]
    ahead: tuple[str, ...]


def brief_from(agenda: Agenda, memory: PanelMemory, names: dict[str, str]) -> list[MemberPacket]:
    """Each member's folder from the plan and the notes so far. Empty memory means nothing heard
    and every seed still ahead."""
    heard: dict[str, list[str]] = {}
    for note in memory.notes:
        if note.gist.strip():
            heard.setdefault(note.segment_id, []).append(note.gist.strip())
    packets: list[MemberPacket] = []
    for segment in agenda.segments:
        asked = set(memory.asked_seeds.get(segment.id, []))
        questions = tuple(seed.text for seed in segment.seeds)
        ahead = tuple(seed.text for index, seed in enumerate(segment.seeds) if index not in asked)
        packets.append(MemberPacket(
            member=segment.member,
            name=names.get(segment.member, segment.member),
            segment=segment.id,
            questions=questions,
            heard=tuple(heard.get(segment.id, ())),
            ahead=ahead,
        ))
    return packets


def discussion_lines(
    agenda: Agenda, memory: PanelMemory, names: dict[str, str], weakest: str | None
) -> list[str]:
    """What the members can say to each other at the end. Only their notes and their plan."""
    packets = brief_from(agenda, memory, names)
    lines = [
        "Chairperson: The plan came from the form. Each of us held our questions, "
        "and we did not chase a number."
    ]
    for packet in packets:
        if packet.heard:
            lines.append(f"{packet.name}: I heard this. {packet.heard[-1]}")
        else:
            lines.append(f"{packet.name}: I have no clear note from the answers in my part.")
        if packet.ahead:
            lines.append(f"{packet.name} still had ready: {packet.ahead[0]}")
    if weakest:
        lines.append(f"Chairperson: The thin moment to practise is this question. {weakest}")
    else:
        lines.append("Chairperson: We did not mark a thin moment.")
    return lines
