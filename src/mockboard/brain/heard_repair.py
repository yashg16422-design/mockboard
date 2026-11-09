"""Words the speech recogniser most likely misheard, and what the candidate most likely said.

The owner said "Claude" and Prisma wrote "क्राउड" (crowd), next to "चैट जीपीटी" (Nov 6). A person
would understand from the context; this step does the same, openly. One Nova call (`repair.v1`)
proposes pairs; code accepts a pair only if `heard` is really in that transcript, both sides are
short, and neither touches a number or a negation. Nothing is replaced silently: the page shows
"heard X, likely Y" beside the transcript, and the transcript itself is never edited.

Never raises (a cancelled call does): a failed, late or empty reply means no pairs.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from mockboard.brain.llm import LLMClient, LLMError, with_backoff
from mockboard.brain.prompts import PromptKit, tag
from mockboard.brain.tools import REPAIR_TOOL, RepairsOutput

__all__ = ["Repair", "repair_violations", "repairs_for"]

MAX_PER_ANSWER = 3
MAX_WORDS = 4
MAX_CHARS = 40
MAX_ANSWERS = 20
MAX_TOKENS = 600
_DIGIT = re.compile(r"\d")
# A pair may never flip or touch a negation, in any script the board hears.
_NEGATION = re.compile(
    r"(?<![a-z])(?:not|no|never|nahi|nahin|na|mat)(?![a-z])|नहीं|नही|मत|ना|இல்லை|வேண்டாம்|கூடாது",
    re.IGNORECASE,
)
# English letter names as Prisma writes them in Devanagari. A pair whose `heard` spells letters
# that `likely` simply contains ("ए आई टी सी" -> "AITC", "जीपीटी" -> "ChatGPT") is the same word in
# another script, not a mishearing: Nova proposed both on Nov 6 (repair.v1, owner's transcript).
# The list is ours; Tamil letter names are not covered yet.
_LETTER_NAMES = {
    "ए": "A", "बी": "B", "सी": "C", "डी": "D", "ई": "E", "एफ": "F", "जी": "G", "एच": "H",
    "आई": "I", "जे": "J", "के": "K", "एल": "L", "एम": "M", "एन": "N", "ओ": "O", "पी": "P",
    "क्यू": "Q", "आर": "R", "एस": "S", "टी": "T", "यू": "U", "वी": "V", "डब्ल्यू": "W",
    "डबल्यू": "W", "एक्स": "X", "वाई": "Y", "ज़ेड": "Z", "जेड": "Z",
}
_LETTER_NAMES = {unicodedata.normalize("NFC", k): v for k, v in _LETTER_NAMES.items()}
_LONGEST_NAME = max(len(k) for k in _LETTER_NAMES)


@dataclass(frozen=True)
class Repair:
    answer: int  # 1-based, as numbered in the prompt
    heard: str
    likely: str
    why: str = ""

    def as_json(self) -> dict[str, object]:
        return asdict(self)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _letters_of_word(word: str) -> str | None:
    """The English letters a Devanagari word spells ("जीपीटी" -> "GPT"), or None if it is not
    made only of letter names. Greedy, longest name first."""
    out, i = [], 0
    while i < len(word):
        for size in range(min(_LONGEST_NAME, len(word) - i), 0, -1):
            letter = _LETTER_NAMES.get(word[i : i + size])
            if letter:
                out.append(letter)
                i += size
                break
        else:
            return None
    return "".join(out)


def spelled_runs(text: str) -> list[str]:
    """Runs of spelled-out letters in `text`: consecutive words made only of letter names,
    joined ("ए आई टी सी" -> ["AITC"], "चैट जीपीटी" -> ["GPT"]). Pure."""
    runs: list[str] = []
    current = ""
    for word in _nfc(text).split():
        letters = _letters_of_word(word.strip(".,;:!?।\"'"))
        if letters:
            current += letters
        else:
            if current:
                runs.append(current)
            current = ""
    if current:
        runs.append(current)
    return runs


def is_script_change(heard: str, likely: str) -> bool:
    """True when `heard` spells two or more letters that `likely` already contains in Latin:
    the recogniser heard right and only the script differs. Pure."""
    target = re.sub(r"[^A-Z]", "", likely.upper())
    return any(len(run) >= 2 and run in target for run in spelled_runs(heard))


def repair_violations(heard: str, likely: str, transcript: str) -> list[str]:
    """Why a proposed pair is not shown (empty = shown). Pure."""
    heard, likely, transcript = _nfc(heard.strip()), _nfc(likely.strip()), _nfc(transcript)
    problems: list[str] = []
    if not heard or not likely:
        problems.append("empty")
        return problems
    if heard not in transcript:
        problems.append("heard is not in the transcript")
    if heard.casefold() == likely.casefold():
        problems.append("no change")
    if is_script_change(heard, likely):
        problems.append("same word in another script")
    for side in (heard, likely):
        if len(side) > MAX_CHARS or len(side.split()) > MAX_WORDS:
            problems.append("too long")
        if _DIGIT.search(side):
            problems.append("touches a number")
        if _NEGATION.search(side):
            problems.append("touches a negation")
        if "<" in side or ">" in side:
            problems.append("mark-up")
    return list(dict.fromkeys(problems))


def build_repair_user(question: str, answers: Sequence[str]) -> str:
    numbered = "\n".join(f"{n}. {text}" for n, text in enumerate(answers, start=1))
    return "\n\n".join([tag("question", question or "(several questions)"),
                        tag("answers", numbered)])


async def repairs_for(
    answers: Sequence[str],
    *,
    llm: LLMClient | None,
    kit: PromptKit | None,
    question: str = "",
    timeout_s: float = 15.0,
    tag_: str = "repair",
) -> list[Repair]:
    """Checked pairs for the given transcripts (1-based `answer`). No call without llm and kit.
    `tag_` is the trace tag: a sitting passes its session id, so the call is written with the
    report event (every model call must reach the trace)."""
    answers = [a for a in answers][:MAX_ANSWERS]
    if llm is None or kit is None or not any(a.strip() for a in answers):
        return []
    try:
        out = await asyncio.wait_for(
            with_backoff(lambda: llm.structured(
                name="repair", system=kit.system("repair"),
                user=build_repair_user(question, answers), tool=REPAIR_TOOL,
                output_model=RepairsOutput, tag=tag_, max_tokens=MAX_TOKENS,
            )),
            timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except (LLMError, ValueError, TypeError, TimeoutError, LookupError):
        return []
    kept: list[Repair] = []
    per_answer: dict[int, int] = {}
    for pair in out.repairs:
        if not 1 <= pair.answer <= len(answers):
            continue
        if per_answer.get(pair.answer, 0) >= MAX_PER_ANSWER:
            continue
        if repair_violations(pair.heard, pair.likely, answers[pair.answer - 1]):
            continue
        per_answer[pair.answer] = per_answer.get(pair.answer, 0) + 1
        why = " ".join(pair.why.split()[:12])
        kept.append(Repair(pair.answer, _nfc(pair.heard.strip()), _nfc(pair.likely.strip()), why))
    return kept
