"""Motion safety screen for CUSTOM motions (CTO_NOTES_04 §5, ruling 21). It fails closed.

Curated motions never pass through here: they were reviewed by hand. A custom motion is only
possible with FEATURE_CUSTOM_MOTIONS on (default off, and off in the public demo), and then
passes three layers:

1. **Rules** (this file, no model): normalise to "This House ...", at most 120 characters,
   and a blocklist: political parties, elections and named political leaders; religion;
   caste; communal conflict; violence; self-harm; sexual content; illegal how-to; live court
   cases. Rules cannot list every leader or every way of saying something partisan, which is
   why layer 2 exists.
2. **Classifier**: one forced-tool Nova call (`motion_screen.v1.md`, background lane,
   temperature 0) returns allow / reframe / refuse, a category, a reason and a neutral
   rewording.
3. **Outcome**: refuse stands. A reframe is offered only if its rewording itself passes the
   rules; the user must accept it before it is used. ANY classifier failure (error, throttle,
   timeout, invalid output, an empty rewording) counts as a refusal.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from mockboard.brain.llm import LLMClient, PromptBlock
from mockboard.brain.prompts import load_prompt, tag
from mockboard.brain.tools import MOTION_SCREEN_TOOL, MotionScreenOutput
from mockboard.modes.debate.motions import MAX_MOTION_CHARS, normalise_motion


class ScreenDecision(StrEnum):
    allow = "allow"
    reframe = "reframe"
    refuse = "refuse"


class ScreenLayer(StrEnum):
    feature_off = "feature_off"
    rules = "rules"
    classifier = "classifier"
    fail_closed = "fail_closed"


class ScreenResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ScreenDecision
    layer: ScreenLayer
    category: str
    reason: str
    motion: str  # the normalised motion that was screened
    reframed: str | None = None  # only with decision == reframe; the user must accept it

    @property
    def usable_motion(self) -> str | None:
        """The motion text the debate may use without further consent (allow only)."""
        return self.motion if self.decision == ScreenDecision.allow else None


def _rx(*terms: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


# Layer 1 blocklist, by refusal category. Kept conservative: a false refusal costs the user a
# retry; a false allow of a partisan motion is a public incident.
RULES: dict[str, re.Pattern[str]] = {
    "partisan": _rx(
        # Parties (Indian national and state parties; "aap" alone is Hinglish for "you").
        r"bjp", r"congress", r"aam aadmi party", r"trinamool", r"tmc", r"dmk", r"aiadmk",
        r"shiv sena", r"samajwadi", r"bahujan samaj", r"bsp", r"cpi", r"cpi\(m\)", r"cpm",
        r"ncp", r"jd\(u\)", r"jdu", r"rjd", r"tdp", r"ysrcp", r"brs", r"trs", r"bjd",
        r"akali dal", r"rss", r"vhp", r"political part(?:y|ies)", r"ruling party",
        r"opposition part(?:y|ies)", r"nda", r"upa", r"india bloc",
        # Elections.
        r"elections?", r"electoral", r"chunav", r"polls", r"polling", r"vote (?:for|out|against)",
        r"voting for", r"re-?elect\w*", r"manifesto", r"lok sabha", r"vidhan sabha",
        r"rajya sabha", r"evms?", r"incumbent",
        # Offices and named leaders (a sample; layer 2 covers the rest).
        r"prime minister", r"chief minister", r"pm modi", r"modi", r"narendra modi",
        r"rahul gandhi", r"sonia gandhi", r"priyanka gandhi", r"kejriwal", r"mamata",
        r"banerjee", r"adityanath", r"yogi", r"amit shah", r"owaisi", r"nitish kumar",
        r"akhilesh", r"mayawati", r"lalu", r"tejashwi", r"thackeray", r"sharad pawar",
        r"naveen patnaik", r"siddaramaiah", r"fadnavis", r"kharge", r"nadda", r"himanta",
        # "Has the X government failed" and similar verdicts on a government.
        r"(?:government|govt|sarkar|regime)(?:'s)?\s+(?:has\s+)?(?:failed|failure|is\s+"
        r"(?:corrupt|anti-\w+|incompetent|the best|the worst))",
        r"(?:current|present|ruling|this|modi|congress)\s+(?:government|govt|sarkar)",
    ),
    "religion": _rx(
        r"religions?", r"religious", r"hindus?", r"hinduism", r"hindutva", r"muslims?",
        r"islam\w*", r"christians?", r"christianity", r"sikhs?", r"jains?", r"buddhists?",
        r"parsis?", r"jews?", r"temples?", r"mosques?", r"churche?s?", r"mandirs?", r"masjids?",
        r"gurudwaras?", r"mazhab", r"god", r"gods", r"atheis\w*", r"hijab", r"burqa",
        r"conversions?", r"beef", r"cow slaughter", r"halal",
    ),
    "caste": _rx(
        r"castes?", r"caste-based", r"jaati", r"jati", r"dalits?", r"brahmins?", r"savarna",
        r"obc", r"scheduled castes?", r"scheduled tribes?", r"reservations?", r"varna",
        r"upper caste", r"lower caste",
    ),
    "communal": _rx(
        r"communal", r"riots?", r"love jihad", r"hindu-muslim", r"pogrom", r"mob lynching",
    ),
    "violence": _rx(
        r"kill\w*", r"murder\w*", r"bombs?", r"bombing", r"terror\w*", r"lynch\w*",
        r"shoot\w*", r"weapons?", r"guns?", r"rape\w*", r"assault\w*", r"torture\w*",
    ),
    "self_harm": _rx(
        r"suicid\w*", r"self-?harm", r"kill (?:myself|yourself|themselves)",
        r"end (?:my|your|their) li(?:fe|ves)", r"starv\w*",
    ),
    "sexual": _rx(
        r"sex", r"sexual\w*", r"porn\w*", r"nude\w*", r"nudity", r"prostitut\w*", r"escorts?",
    ),
    "illegal": _rx(
        r"how to (?:make|build|hack|steal|cheat|smuggle|forge|buy drugs)", r"hack\w*",
        r"pirat\w*", r"smuggl\w*", r"bribe\w*", r"drugs?",
    ),
    "court_case": _rx(
        r"court cases?", r"verdicts?", r"trials?", r"sub ?judice", r"accused", r"acquit\w*",
        r"convict\w*", r"high court", r"supreme court", r"bail", r"undertrials?",
    ),
}


def rule_screen(text: str) -> ScreenResult | None:
    """Layer 1. A refusal, or None when the rules see no problem (go to the classifier)."""
    motion = normalise_motion(text)
    if not motion:
        return ScreenResult(
            decision=ScreenDecision.refuse, layer=ScreenLayer.rules, category="empty",
            reason="the motion is empty", motion=motion,
        )
    if len(motion) > MAX_MOTION_CHARS:
        return ScreenResult(
            decision=ScreenDecision.refuse, layer=ScreenLayer.rules, category="too_long",
            reason=f"a motion is at most {MAX_MOTION_CHARS} characters", motion=motion,
        )
    for category, pattern in RULES.items():
        m = pattern.search(motion)
        if m:
            return ScreenResult(
                decision=ScreenDecision.refuse, layer=ScreenLayer.rules, category=category,
                reason=f"blocked term {m.group(0)!r} ({category})", motion=motion,
            )
    return None


def build_screen_user(motion: str) -> str:
    return tag("motion", motion) + "\nScreen this motion now and call the tool."


def _refuse_closed(motion: str, why: str) -> ScreenResult:
    return ScreenResult(
        decision=ScreenDecision.refuse, layer=ScreenLayer.fail_closed, category="screen_failed",
        reason=f"the safety screen could not decide ({why}); refused by default", motion=motion,
    )


async def classify_motion(
    llm: LLMClient, motion: str, *, version: int = 1, tag_: str = "motion_screen"
) -> MotionScreenOutput:
    """Layer 2: one forced-tool call. Raises on failure; `screen_motion` fails closed."""
    prompt = load_prompt("motion_screen", version)
    return await llm.structured(
        name="motion_screen",  # not a listed lane name -> background lane
        system=[PromptBlock(prompt.render())],
        user=build_screen_user(motion),
        tool=MOTION_SCREEN_TOOL,
        output_model=MotionScreenOutput,
        tag=tag_,
        max_tokens=400,
    )


async def screen_motion(
    text: str, *, llm: LLMClient, enabled: bool, version: int = 1
) -> ScreenResult:
    """Run the whole screen. `enabled` is FEATURE_CUSTOM_MOTIONS; off means refuse."""
    motion = normalise_motion(text)
    if not enabled:
        return ScreenResult(
            decision=ScreenDecision.refuse, layer=ScreenLayer.feature_off, category="feature_off",
            reason="custom motions are switched off; choose a curated motion", motion=motion,
        )
    ruled = rule_screen(motion)
    if ruled is not None:
        return ruled
    try:
        out = await classify_motion(llm, motion, version=version)
    except Exception as exc:  # fail closed on ANY classifier failure
        return _refuse_closed(motion, type(exc).__name__)
    decision = ScreenDecision(out.decision)
    if decision == ScreenDecision.refuse:
        return ScreenResult(
            decision=decision, layer=ScreenLayer.classifier, category=out.category or "other",
            reason=out.reason or "refused by the classifier", motion=motion,
        )
    if decision == ScreenDecision.reframe:
        reworded = normalise_motion(out.neutral_rewording or "")
        if not reworded or rule_screen(reworded) is not None:
            return _refuse_closed(motion, "the suggested rewording did not pass the rules")
        return ScreenResult(
            decision=decision, layer=ScreenLayer.classifier, category=out.category or "loaded",
            reason=out.reason or "reworded neutrally", motion=motion, reframed=reworded,
        )
    return ScreenResult(
        decision=ScreenDecision.allow, layer=ScreenLayer.classifier, category="none",
        reason=out.reason or "allowed", motion=motion,
    )
