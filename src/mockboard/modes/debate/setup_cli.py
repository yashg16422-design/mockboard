"""The debate setup form for the text CLI (CTO_NOTES_04 §1): motion, side, length, language
and opponent style. No names or identifiers are asked.

A motion is chosen by number (1-13) or id. Anything else is a custom motion: it is refused
unless FEATURE_CUSTOM_MOTIONS is on, and then it must pass the fail-closed safety screen; a
reframed motion is used only if the user accepts the rewording.
"""

from __future__ import annotations

from collections.abc import Callable

from mockboard.brain.llm import LLMClient
from mockboard.config import Settings
from mockboard.modes.debate.case_file import build_custom_case, generic_case
from mockboard.modes.debate.motions import (
    CaseFile,
    Motion,
    Side,
    find_motion,
    load_curated_motions,
)
from mockboard.modes.debate.screen import ScreenDecision, screen_motion
from mockboard.modes.debate.session import DebateSetup, Length, OpponentStyle

DEFAULT_FAKE_SETUP = {
    "motion": "1",
    "side": "for",
    "length": "quick",
    "language": "hinglish",
    "opponent": "logician",
}
MAX_ATTEMPTS = 3

Ask = Callable[[str], "str | None"]


def _choice(value: str, options: list[str], default: str) -> str | None:
    v = (value or "").strip().lower() or default
    return v if v in options else None


def _ask(ask: Ask, prompt: str, use_defaults: bool, key: str) -> str | None:
    value = ask(prompt)
    if value is None and use_defaults:
        return DEFAULT_FAKE_SETUP[key]
    return value


async def _custom_motion(
    text: str, *, ask: Ask, llm: LLMClient, settings: Settings
) -> str | None:
    res = await screen_motion(text, llm=llm, enabled=settings.feature_custom_motions)
    if res.decision == ScreenDecision.allow:
        return res.motion
    if res.decision == ScreenDecision.reframe and res.reframed:
        answer = ask(f"  Suggested neutral wording: {res.reframed}\n  Use it? [y/n]: ")
        if (answer or "").strip().lower() in ("y", "yes", "haan"):
            return res.reframed
        return None
    print(f"  Not available: {res.reason}")
    return None


def custom_motion(text: str) -> Motion:
    case = CaseFile.model_validate(
        {"for": generic_case(Side.for_), "against": generic_case(Side.against)}
    )
    return Motion(id="custom", text=text, title="Custom motion", category="custom", case=case,
                  curated=False)


async def read_debate_setup(
    ask: Ask, *, llm: LLMClient, settings: Settings, use_defaults: bool
) -> DebateSetup:
    motions = load_curated_motions()
    print("\n== Debate setup (no names or identifiers) ==")
    for m in motions.values():
        print(f"  {m.number:>2}. {m.text}")
    motion: Motion | None = None
    custom_text: str | None = None
    for _ in range(MAX_ATTEMPTS):
        value = _ask(ask, "Motion number (1-13): ", use_defaults, "motion")
        if value is None:
            raise SystemExit("input ended during the debate setup")
        try:
            motion = find_motion(motions, value)
            break
        except KeyError:
            custom_text = await _custom_motion(value, ask=ask, llm=llm, settings=settings)
            if custom_text:
                motion = custom_motion(custom_text)
                break
    if motion is None:
        raise SystemExit("no usable motion chosen")

    side = None
    while side is None:
        value = _ask(ask, "Your side [for/against]: ", use_defaults, "side")
        if value is None:
            raise SystemExit("input ended during the debate setup")
        side = _choice(value, ["for", "against"], "")
    length = _choice(
        _ask(ask, "Length [quick/standard] (default quick): ", use_defaults, "length") or "",
        ["quick", "standard"], "quick",
    ) or "quick"
    language = _choice(
        _ask(ask, "Language [hinglish/hindi/english] (default hinglish): ", use_defaults,
             "language") or "",
        ["hinglish", "hindi", "english"], "hinglish",
    ) or "hinglish"
    style = _choice(
        _ask(ask, "Opponent [logician/wit] (default logician): ", use_defaults, "opponent")
        or "",
        ["logician", "wit"], "logician",
    ) or "logician"
    user_side = Side(side)
    case, case_source = None, "curated"
    if not motion.curated:
        case, case_source = await build_custom_case(llm, motion.text, user_side.other)
    return DebateSetup(
        motion=motion,
        user_side=user_side,
        length=Length(length),
        language=language,
        opponent_style=OpponentStyle(style),
        case_source=case_source,
        case=case,
    )
