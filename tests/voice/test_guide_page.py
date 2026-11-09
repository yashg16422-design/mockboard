"""The guide page (/guide, Nov 7, owner): where everything is, how each mode works, what you can
say, and what the report shows. Every phrase it promises must really be recognised, it must call
no outside host, and its comparison must not make claims about a named product."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from mockboard.brain.requests import detect_request
from mockboard.brain.requests_ta import detect_tamil_request
from mockboard.brain.think_time import is_think_request
from mockboard.voice import server

STATIC = Path("src/mockboard/voice/static")
GUIDE = (STATIC / "guide.html").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
EXPECTED = {
    "Hear the question again": "repeat",
    "Think before answering": "think",
    "Move to another topic": "change_topic",
    "End the session": "end",
}


def _kind(phrase: str) -> str | None:
    hit = detect_request(phrase) or detect_tamil_request(phrase)
    if hit:
        return hit.kind.value
    return "think" if is_think_request(phrase) else None


def test_the_guide_is_served_with_its_own_stylesheet(settings):
    cfg = server.parse_args(["--mode", "upsc", "--fake-llm", "--fake-speech"])
    cfg.settings = settings
    client = TestClient(server.create_app(cfg))
    page = client.get("/guide")
    assert page.status_code == 200 and "How Mock Board works." in page.text
    assert client.get("/static/guide.css").status_code == 200
    assert client.get("/static/guide.html").status_code == 404  # the route, not a static file


def test_every_section_of_the_guide_is_there_and_linked():
    for anchor in ("map", "modes", "say", "report", "different", "privacy"):
        assert f'id="{anchor}"' in GUIDE and f'href="#{anchor}"' in GUIDE
    assert GUIDE.count('class="pin"') >= 12  # six callouts on the map, six in the legend


def test_every_phrase_the_guide_promises_is_recognised_as_that_request():
    block = GUIDE[GUIDE.index('class="say-table"') :]
    block = block[: block.index("</section>")]
    row = r'<div class="say-row" role="row"><span role="cell">([^<]+)</span>(.*?)</div>'
    rows = re.findall(row, block)
    assert {want for want, _ in rows} == set(EXPECTED)
    for want, cells in rows:
        phrases = re.findall(r"“([^”]+)”", cells)
        assert len(phrases) == 4, (want, phrases)  # Hinglish, English, Hindi, Tamil
        for phrase in phrases:
            assert _kind(phrase) == EXPECTED[want], (want, phrase)


def test_the_guide_calls_no_outside_host_and_names_no_competitor():
    for tag in re.findall(r"<(?:link|script)\b[^>]*>", GUIDE):
        assert "http://" not in tag and "https://" not in tag, tag
    low = GUIDE.lower()
    for name in ("chatgpt", "openai", "gemini", "alexa", "siri", "copilot"):
        assert name not in low, name


def test_the_guide_states_its_limits_and_the_models():
    for text in ("Gnani Prisma", "Gnani Timbre", "Amazon Nova Pro", "not a UPSC mark",
                 "not yet been reviewed by a fluent", "No name, roll number"):
        assert text in GUIDE, text


def test_the_app_header_links_to_the_guide():
    assert 'href="/guide"' in INDEX


def test_every_phrase_the_guide_promises_also_works_with_the_study_partner():
    """The fine print says the phrases work with the partner too ("another topic" = next point)."""
    from mockboard.modes.partner.session import _request

    partner_kind = {"repeat": "repeat", "think": "think", "change_topic": "skip", "end": "end"}
    block = GUIDE[GUIDE.index('class="say-table"') :]
    block = block[: block.index("</section>")]
    row = r'<div class="say-row" role="row"><span role="cell">([^<]+)</span>(.*?)</div>'
    for want, cells in re.findall(row, block):
        for phrase in re.findall(r"“([^”]+)”", cells):
            assert _request(phrase) == partner_kind[EXPECTED[want]], (want, phrase)
