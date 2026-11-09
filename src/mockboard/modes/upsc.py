"""`upsc_interview`: the UPSC Personality Test panel (the default mode).

Since P7 the interview is always the panel: five members, an agenda built from the candidate's
form, a director that understands requests, hand-offs and a close (ADR-010). The rule table is still
`strategist.decide_move`, the move policy inside a segment; the director (`brain/director.py`) has
the last word on each turn. The deadline fallback and the turn cap below are the rules the session
applied before modes existed.
"""

from __future__ import annotations

from mockboard.brain.personas import PERSONA_DIR
from mockboard.brain.schemas import BoardMove, Dimension, Move, StrategyContext
from mockboard.brain.strategist import CLOSE_TARGET, PRESSURE, StrategyConfig, decide_move
from mockboard.brain.tools import PANEL_LISTENER_TOOL, PanelListenerOutput
from mockboard.modes.registry import ModeProfile, register

# The nine UPSC board moves (PLAN_v2_DELTA §7), plus the three the panel director emits and the
# rule table never does (CTO_NOTES_06 §7). Debate moves are never emitted here.
UPSC_MOVES: frozenset[Move] = frozenset(
    {
        Move.probe,
        Move.counter,
        Move.steelman,
        Move.fact_challenge,
        Move.pressure,
        Move.reassure,
        Move.move_on,
        Move.clarify,
        Move.interrupt,
        Move.repeat_question,
        Move.rephrase_question,
        Move.next_question,
    }
)


def upsc_fallback_move(
    ctx: StrategyContext, spoke: bool, reason: str, cfg: StrategyConfig
) -> BoardMove:
    """Deterministic fast rule when no analysis of this turn finished in time
    (CTO_NOTES_02 M4.4): nothing said -> reassure (chairman), otherwise a neutral probe."""
    if not spoke:
        move, member = Move.reassure, cfg.chairman
    else:
        move, member = Move.probe, cfg.member_for(ctx.topic)
    return BoardMove(
        move=move,
        member=member,
        pressure=PRESSURE[move],
        reason=f"FALLBACK: {reason}",
        rule_id="FALLBACK",
        for_turn=ctx.turn_id,
    )


def upsc_cap_move(
    ctx: StrategyContext, move: BoardMove, max_turns: int, cfg: StrategyConfig
) -> BoardMove:
    """The session's hard turn cap: the chairperson closes the interview."""
    if move.move == Move.move_on and move.target == CLOSE_TARGET:
        return move
    return BoardMove(
        move=Move.move_on,
        target=CLOSE_TARGET,
        member=cfg.chairman,
        pressure=PRESSURE[Move.move_on],
        reason=f"CAP: session turn cap ({max_turns}) reached",
        rule_id="CAP",
        for_turn=ctx.turn_id,
        preface=move.preface,  # a conceded error is still acknowledged at the close
    )


def panel_session_hooks(session, options):
    """The panel's hooks for every UPSC session. No options means a default `PanelSetup`, which is
    uncapped: a caller that must cap answers (the server, for a test user) passes its own. Imported
    here because the panel imports the graphs, which import this registry."""
    from mockboard.modes.upsc_panel.hooks import PanelHooks
    from mockboard.modes.upsc_panel.setup import PanelSetup

    if options is None:
        options = PanelSetup()
    if not isinstance(options, PanelSetup):
        raise TypeError(f"the UPSC panel takes a PanelSetup, not {type(options).__name__}")
    return PanelHooks(session, options)


UPSC_PROFILE = register(
    ModeProfile(
        id="upsc_interview",
        title="UPSC Personality Test board",
        decide=decide_move,
        fallback_move=upsc_fallback_move,
        cap_move=upsc_cap_move,
        allowed_moves=UPSC_MOVES,
        prompt_ids={
            "shared": "shared",
            "listener": "listener",
            "actor": "actor",
            "judge": "judge",
            "opener": "opener",
        },
        persona_dir=PERSONA_DIR,
        persona_heading="## The interview board (persona pack)",
        rubric=tuple(d.value for d in Dimension),
        listener_tool=(PANEL_LISTENER_TOOL, PanelListenerOutput),
        session_hooks=panel_session_hooks,
    )
)


