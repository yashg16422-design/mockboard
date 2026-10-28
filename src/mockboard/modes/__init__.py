"""Modes are profiles, not forks (ADR-009).

A `ModeProfile` bundles everything that differs between Mock Board's modes: the strategist's
rule table, the moves it may emit, the prompt ids, the persona pack, the rubric, the rounds
(topics) and per-move word caps. The session stores `mode`; the brain looks the profile up;
the voice loop never asks which mode it is in.

    from mockboard.modes import get_profile
    profile = get_profile("upsc_interview")
"""

from __future__ import annotations

from mockboard.modes.registry import (
    DEFAULT_MODE,
    ModeProfile,
    get_profile,
    mode_ids,
    register,
)

__all__ = ["DEFAULT_MODE", "ModeProfile", "get_profile", "mode_ids", "register"]
