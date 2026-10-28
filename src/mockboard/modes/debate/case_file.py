"""Case files for CUSTOM motions (CTO_NOTES_04 §2), behind FEATURE_CUSTOM_MOTIONS.

Curated motions have hand-written case files. A screened custom motion gets its opponent's
case from ONE forced-tool background call at setup. Every returned argument is validated as a
`CaseArgument`, which rejects any digit or number word (the opponent never has a sourced
figure for a custom motion); invalid arguments are dropped. With fewer than three valid
arguments, or on any failure, the generic qualitative case below is used instead.
"""

from __future__ import annotations

from pydantic import ValidationError

from mockboard.brain.llm import LLMClient, PromptBlock
from mockboard.brain.prompts import load_prompt, tag
from mockboard.brain.tools import CASE_FILE_TOOL, CaseFileOutput
from mockboard.modes.debate.motions import CaseArgument, Side

# Generic, qualitative fallback cases, written to fit almost any "This House would ..." motion.
GENERIC_CASE: dict[Side, list[tuple[str, str, list[str]]]] = {
    Side.for_: [
        ("The change tackles a real problem that the current situation leaves unsolved.",
         "Keeping things as they are means accepting a cost that people already feel.",
         ["problem", "current situation", "cost", "change"]),
        ("The benefits reach the people who need them most.",
         "A well-designed change helps those with the fewest alternatives first.",
         ["benefit", "people", "need", "help"]),
        ("The risks can be managed with careful design.",
         "Pilots, safeguards and review let us fix problems as they appear.",
         ["risk", "safeguards", "pilot", "design"]),
    ],
    Side.against: [
        ("The costs and side-effects may outweigh the benefits.",
         "A change that solves one problem can create others that are harder to undo.",
         ["cost", "side effects", "harm", "trade-off"]),
        ("There are better ways to reach the same goal.",
         "A smaller or more targeted step could get most of the benefit with less disruption.",
         ["alternative", "targeted", "better way", "smaller step"]),
        ("The people affected may not have been asked.",
         "Changes imposed from above often miss local needs that only those affected can see.",
         ["consultation", "affected", "local", "voice"]),
    ],
}


def generic_case(opponent_side: Side) -> list[CaseArgument]:
    prefix = "F" if opponent_side is Side.for_ else "A"
    return [
        CaseArgument(id=f"{prefix}{i}", claim=c, warrant=w, keywords=k)
        for i, (c, w, k) in enumerate(GENERIC_CASE[opponent_side], start=1)
    ]


def to_case_arguments(out: CaseFileOutput, opponent_side: Side) -> list[CaseArgument]:
    """Valid, figure-free arguments only, renumbered in order; at most four."""
    prefix = "F" if opponent_side is Side.for_ else "A"
    valid: list[CaseArgument] = []
    for raw in out.arguments:
        try:
            arg = CaseArgument(
                id=f"{prefix}{len(valid) + 1}",
                claim=raw.claim.strip(),
                warrant=raw.warrant.strip(),
                keywords=[k.strip() for k in raw.keywords if k.strip()][:10] or ["argument"],
            )
        except ValidationError:
            continue  # a figure, or too short/long: dropped, never spoken
        valid.append(arg)
        if len(valid) == 4:
            break
    return valid


async def build_custom_case(
    llm: LLMClient, motion: str, opponent_side: Side, *, version: int = 1
) -> tuple[list[CaseArgument], str]:
    """The opponent's case for a screened custom motion, and how it was obtained
    ("model" or "generic_fallback: <why>")."""
    prompt = load_prompt("case_file", version)
    try:
        out = await llm.structured(
            name="case_file",  # background lane
            system=[PromptBlock(prompt.render())],
            user=tag("motion", motion) + "\n" + tag("side", opponent_side.value)
            + "\nWrite the case now and call the tool.",
            tool=CASE_FILE_TOOL,
            output_model=CaseFileOutput,
            tag="case_file",
            max_tokens=800,
        )
    except Exception as exc:
        return generic_case(opponent_side), f"generic_fallback: {type(exc).__name__}"
    args = to_case_arguments(out, opponent_side)
    if len(args) < 3:
        return generic_case(opponent_side), f"generic_fallback: {len(args)} valid arguments"
    return args, "model"
