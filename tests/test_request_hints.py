"""The form's spoken-request hints must name phrases the board's rules really recognise.

The hint is a promise to the candidate ("say this and it works"). A hint whose phrase the rules
miss depends on the model's vote alone, and an `end` never works that way. Each language's hint
in `app.js` is parsed and every quoted phrase is run through the same detection the director uses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mockboard.brain.requests import detect_request
from mockboard.brain.requests_ta import detect_tamil_request
from mockboard.brain.think_time import is_think_request

APP = Path(__file__).resolve().parents[1] / "src/mockboard/voice/static/app.js"
PAGE = APP.with_name("index.html")


def _hints() -> dict[str, list[str]]:
    source = APP.read_text(encoding="utf-8")
    block = re.search(r"const TOPIC_HINTS = \{(.*?)\n\};", source, re.S)
    assert block, "TOPIC_HINTS not found in app.js"
    out: dict[str, list[str]] = {}
    for lang, text in re.findall(r"^\s*(\w+): '(.*)',$", block.group(1), re.M):
        out[lang] = re.findall(r"“([^”]+)”", text)
    return out


def _kind(phrase: str) -> str | None:
    hit = detect_request(phrase) or detect_tamil_request(phrase)
    if hit:
        return hit.kind.value
    return "think" if is_think_request(phrase) else None


def test_every_form_language_has_a_hint():
    page = PAGE.read_text(encoding="utf-8")
    languages = set(re.findall(r'<option value="(\w+)">', page)) & {
        "hinglish", "english", "hindi", "tanglish"
    }
    assert languages == {"hinglish", "english", "hindi", "tanglish"}
    assert set(_hints()) == languages


@pytest.mark.parametrize("language", ["hinglish", "english", "hindi", "tanglish"])
def test_each_hint_phrase_is_recognised_as_the_request_it_names(language: str):
    phrases = _hints()[language]
    assert len(phrases) == 3, phrases
    change, repeat, think = phrases
    assert _kind(change) == "change_topic", change
    assert _kind(repeat) == "repeat", repeat
    assert _kind(think) == "think", think


def test_the_page_default_hint_matches_the_hinglish_hint():
    """Without JavaScript the page still shows a working phrase."""
    page = PAGE.read_text(encoding="utf-8")
    default = re.search(r'id="topic-hint"[^>]*>([^<]+)<', page)
    assert default and re.findall(r"“([^”]+)”", default.group(1)) == _hints()["hinglish"]
