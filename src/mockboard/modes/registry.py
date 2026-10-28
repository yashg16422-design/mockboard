"""The mode registry: mode id -> `ModeProfile` (ADR-009).

Profiles are registered once at import time and never change afterwards. `upsc_interview`
is the default and wraps today's behaviour exactly; other modes add their own profile module
and register it here, without touching the UPSC code paths.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mockboard.brain.schemas import Analysis, BoardMove, Claim, Move, StrategyContext

DEFAULT_MODE = "upsc_interview"

# decide(ctx, analyses, ledger, cfg) -> move. Pure rules, no I/O (the strategist contract).
DecideFn = Callable[[StrategyContext, Sequence[Analysis], Sequence[Claim], Any], BoardMove]
# fallback(ctx, spoke, reason, cfg) -> move, when no analysis of the turn finished in time.
FallbackFn = Callable[[StrategyContext, bool, str, Any], BoardMove]
# cap(ctx, move, max_turns, cfg) -> move, when the session's hard turn cap is reached.
CapFn = Callable[[StrategyContext, BoardMove, int, Any], BoardMove]


@dataclass(frozen=True)
class ModeProfile:
    """Everything that makes a mode different, as data plus three pure functions."""

    id: str
    title: str
    decide: DecideFn
    fallback_move: FallbackFn
    cap_move: CapFn
    # The moves this mode's rule table may emit. A test checks each table against it.
    allowed_moves: frozenset[Move]
    # role -> prompt id. Roles: shared (the cached prefix), listener, actor, judge, opener.
    prompt_ids: Mapping[str, str]
    # Folder of persona YAML files (non-recursive), and the persona pack's heading.
    persona_dir: Path
    persona_heading: str
    # Rubric dimension names, in report order.
    rubric: tuple[str, ...]
    # Rounds (the session's "topics"). Empty: the settings' topics after the opener turn.
    rounds: tuple[str, ...] = ()
    # Move value -> word cap for the actor. A move not listed uses `actor_max_words`.
    word_caps: Mapping[str, int] = field(default_factory=dict)
    # The listener's forced tool and output model; None means the UPSC listener's.
    listener_tool: tuple[Any, Any] | None = None
    # Extra, already-tagged context for the listener's user message; None means none.
    listener_context: Callable[[StrategyContext], str] | None = None

    def prompt_id(self, role: str) -> str:
        return self.prompt_ids[role]

    def word_cap(self, move: Move, default: int) -> int:
        return self.word_caps.get(move.value, default)


_REGISTRY: dict[str, ModeProfile] = {}


def register(profile: ModeProfile) -> ModeProfile:
    if profile.id in _REGISTRY and _REGISTRY[profile.id] is not profile:
        raise ValueError(f"mode {profile.id!r} is already registered")
    _REGISTRY[profile.id] = profile
    return profile


def _load_builtin() -> None:
    # Imported for their side effect (registration); kept here so importing the registry
    # alone gives every built-in mode.
    from mockboard.modes import upsc  # noqa: F401
    from mockboard.modes.debate import profile  # noqa: F401


def get_profile(mode: str | None) -> ModeProfile:
    _load_builtin()
    key = mode or DEFAULT_MODE
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ValueError(f"unknown mode {key!r}; known modes: {sorted(_REGISTRY)}") from None


def mode_ids() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)
