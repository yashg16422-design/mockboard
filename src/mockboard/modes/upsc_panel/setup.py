"""What a panel session needs beyond the candidate's form (CTO_NOTES_12 §9.5)."""

from __future__ import annotations

from dataclasses import dataclass

from mockboard.brain.schemas import PANEL_FREE_TURNS


@dataclass(frozen=True)
class PanelSetup:
    """`max_answers` is the test-user cap (Ruling 49): None means uncapped, and a cap spends itself
    as the agenda, so the panel still reaches a proper close. It comes from the server's admission
    decision in P7, never from the page. `free_turns` is `PANEL_FREE_TURNS` (Ruling 61): requests
    and transcription faults a session forgives before they count as answers."""

    max_answers: int | None = None
    free_turns: int = PANEL_FREE_TURNS
