"""Checks on what a client sends us (CTO_NOTES_18 §5). Messages name the field, never the value.

The form is the site's `CandidateForm` (no names or identifiers). On top of its own limits, every
free-text field is refused when it holds a link, markup characters or an instruction phrase: the
partner topic's rules plus the agenda's Hindi instruction words. The candidate's speech is not
refused for its words (a candidate may say "ignore"); it is only capped and stripped of control
characters, because our code never follows instructions in it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from mockboard.brain.schemas import CandidateForm
from mockboard.modes.partner.session import _INSTRUCTION as _PARTNER_INSTRUCTION
from mockboard.modes.partner.session import _TOPIC_CHARS, _URL, validate_topic

MAX_SPEECH_CHARS = 4000
FORM_TEXT_FIELDS = ("home_state", "district", "degree_subject", "optional_subject",
                    "work_experience")
FORM_FIELDS = (*FORM_TEXT_FIELDS, "hobbies", "interview_language")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# The agenda's Hindi instruction words (`agenda._INSTRUCTION`), without its "assistant", which a
# real job title can hold ("assistant engineer").
_HINDI_INSTRUCTION = re.compile(r"\b(?:nirdesh|bhool jao)\b|निर्देश|अनदेखा", re.IGNORECASE)


class RefusedInput(ValueError):
    """Plain words for the client: what was refused and why, never the refused text."""


def _check_text(field: str, value: str) -> None:
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text:
        return
    if _URL.search(text):
        raise RefusedInput(f"{field}: a link is not allowed")
    if not _TOPIC_CHARS.match(text):
        raise RefusedInput(f"{field}: use letters, numbers, spaces and simple punctuation only")
    if _PARTNER_INSTRUCTION.search(text) or _HINDI_INSTRUCTION.search(text):
        raise RefusedInput(f"{field}: describe yourself only, not instructions")


def check_form(raw: object) -> CandidateForm:
    """The application form, or `RefusedInput`. Unknown fields (a name, a phone) are refused."""
    if not isinstance(raw, Mapping):
        raise RefusedInput("form: send one object with the form's fields")
    extra = sorted(str(k) for k in raw if k not in FORM_FIELDS)
    if extra:
        raise RefusedInput(
            "form: these fields are not asked for (no names or identifiers): " + ", ".join(extra)
        )
    values: dict[str, Any] = {k: v for k, v in raw.items() if v not in (None, "")}
    if isinstance(values.get("hobbies"), str):
        values["hobbies"] = [h.strip() for h in values["hobbies"].split(",") if h.strip()]
    for field in FORM_TEXT_FIELDS:
        if field in values:
            if not isinstance(values[field], str):
                raise RefusedInput(f"{field}: must be text")
            _check_text(field, values[field])
    hobbies = values.get("hobbies", [])
    if not isinstance(hobbies, list) or not all(isinstance(h, str) for h in hobbies):
        raise RefusedInput("hobbies: a list of up to three short words or phrases")
    for hobby in hobbies:
        _check_text("hobbies", hobby)
    try:
        return CandidateForm.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'form'}: {e['msg']}" for e in exc.errors()
        )
        raise RefusedInput(problems) from None


def check_topic(raw: object) -> str:
    try:
        return validate_topic(raw)  # type: ignore[arg-type]
    except ValueError as exc:
        raise RefusedInput(f"topic: {exc}") from None


def clean_speech(raw: object) -> str:
    """The candidate's words as one line of text, capped. Not refused for what it says."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise RefusedInput("candidate_said: must be text")
    text = _CONTROL.sub(" ", unicodedata.normalize("NFC", raw))
    text = " ".join(text.split())
    if len(text) > MAX_SPEECH_CHARS:
        raise RefusedInput(
            f"candidate_said: keep one answer under {MAX_SPEECH_CHARS} characters"
        )
    return text
