"""`debate`: the board argues the other side of a motion the user picks (ruling 19,
CTO_NOTES_04). Registered as a profile on the same engine (ADR-009)."""

from __future__ import annotations

from mockboard.brain.personas import PERSONA_DIR
from mockboard.brain.tools import DEBATE_LISTENER_TOOL, DebateListenerOutput
from mockboard.modes.debate.listener import debate_listener_context
from mockboard.modes.debate.strategist import (
    DEBATE_MOVES,
    ROUNDS,
    debate_cap_move,
    debate_fallback_move,
    decide_debate_move,
)
from mockboard.modes.registry import ModeProfile, register

# Per-move word caps (CTO_NOTES_04 §1: opening at most 70 words, closing at most 60).
# UNVERIFIED: the others are our choice, to be tuned against Timbre's speaking time.
DEBATE_WORD_CAPS: dict[str, int] = {
    "opening_statement": 70,
    "closing_statement": 60,
    "rebut": 45,
    "concede_point": 45,
    "cross_question": 35,
    "steelman": 30,
    "fact_challenge": 30,
    "redirect": 30,
    "reassure": 30,
    "move_on": 35,
    "clarify": 25,
    "interrupt": 25,
}

# The six rubric dimensions (CTO_NOTES_04 §4). No accent, grammar or fluency score.
DEBATE_RUBRIC: tuple[str, ...] = (
    "clarity_structure",
    "evidence_use",
    "rebuttal_quality",
    "responsiveness",
    "fairness_to_other_side",
    "composure",
)

DEBATE_PROFILE = register(
    ModeProfile(
        id="debate",
        title="Debate: argue a motion against the board",
        decide=decide_debate_move,
        fallback_move=debate_fallback_move,
        cap_move=debate_cap_move,
        allowed_moves=DEBATE_MOVES,
        prompt_ids={
            "shared": "shared_debate",
            "listener": "listener_debate",
            "actor": "actor_debate",
            "judge": "judge_debate",
        },
        persona_dir=PERSONA_DIR / "debate",
        persona_heading="## The debate board (persona pack)",
        rubric=DEBATE_RUBRIC,
        rounds=ROUNDS,
        word_caps=DEBATE_WORD_CAPS,
        listener_tool=(DEBATE_LISTENER_TOOL, DebateListenerOutput),
        listener_context=debate_listener_context,
    )
)
