"""The debate listener's context block (CTO_NOTES_04 §2).

The listener needs the Opponent's case (argument ids and claims) to say which arguments the
user answered (`rebuts`), and the point the Opponent pressed last. It travels in the user
message inside `<debate_context>`, so the cached system prefix stays the same for every motion.
"""

from __future__ import annotations

import json

from mockboard.brain.prompts import tag
from mockboard.brain.schemas import StrategyContext


def debate_listener_context(ctx: StrategyContext) -> str:
    d = ctx.debate
    if d is None:
        return ""
    last = next((a for a in d.opponent_args if a.id == d.last_opponent_arg), None)
    payload = {
        "motion": d.motion,
        "user_side": d.user_side,
        "round": ctx.topic,
        "opponent_arguments": [
            {"id": a.id, "claim": a.claim, "keywords": a.keywords} for a in d.opponent_args
        ],
        "opponent_last_point": {"id": last.id, "claim": last.claim} if last else None,
    }
    return tag("debate_context", json.dumps(payload, ensure_ascii=False))
