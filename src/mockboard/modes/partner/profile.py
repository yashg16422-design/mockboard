"""`study_partner`: a low-pressure study conversation with one friend, Mitra (ADR-009 profile).

The speaker picks a topic they are studying and explains it aloud; Mitra restates each point in
fresh words and asks one question that makes it stronger (why, an example, a counterpoint framed
as "some would argue"). No scores, no timer pressure. The listener is the UPSC panel listener,
unchanged (prompt `listener`, tool `record_panel_analysis`), because its prompt names that tool.
"""

from __future__ import annotations

from mockboard.brain.personas import PERSONA_DIR
from mockboard.brain.tools import PANEL_LISTENER_TOOL, PanelListenerOutput
from mockboard.modes.partner.session import PartnerHooks, PartnerSetup
from mockboard.modes.partner.strategist import (
    PARTNER_MOVES,
    decide_partner_move,
    partner_cap_move,
    partner_fallback_move,
)
from mockboard.modes.registry import ModeProfile, register

MODE_ID = "study_partner"
TITLE = "Study partner"
# Every line: one restating line, then one question; about 40 words at most (owner's brief).
PARTNER_WORD_CAPS: dict[str, int] = {m.value: 40 for m in PARTNER_MOVES}

PARTNER_PROFILE = register(
    ModeProfile(
        id=MODE_ID,
        title=TITLE,
        decide=decide_partner_move,
        fallback_move=partner_fallback_move,
        cap_move=partner_cap_move,
        allowed_moves=PARTNER_MOVES,
        prompt_ids={
            "shared": "shared_partner",
            "listener": "listener",
            "actor": "partner_actor",
            "summary": "partner_summary",
        },
        persona_dir=PERSONA_DIR / "partner",
        persona_heading="## The study partner (persona)",
        rubric=(),  # no scores in this mode
        word_caps=PARTNER_WORD_CAPS,
        listener_tool=(PANEL_LISTENER_TOOL, PanelListenerOutput),
        session_hooks=lambda session, options: PartnerHooks(session, _setup(options)),
    )
)


def _setup(options: object) -> PartnerSetup:
    if not isinstance(options, PartnerSetup):
        raise ValueError("a study partner session needs a PartnerSetup as mode_options")
    return options
