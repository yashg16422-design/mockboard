"""Six fixes from the Nov 7 review of the dark particle frontend (Ruling 93), checked in fake mode.

1. "How the board heard you" rows showed no sign that they open, so a likely mishearing was
   never seen: the rows draw their own arrow, and a row with a likely mishearing starts open.
2. On a phone the live captions fell below the brain, off screen: they now follow the controls.
3. A greyed "Start speaking" button sat in the room during a sitting, though you just speak: it is
   hidden during a sitting and reads "Start again" after one. Debate still starts from it.
4. Captions showed the board's internal segment id ("form_hr"): it is kept on the row, not shown.
5. The home disclaimer could sit on the particle cloud: it has its own dark backing.
6. Fonts came from Google's font service, which the privacy notice did not mention: the page now
   serves its own fonts and calls no outside host.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from mockboard.voice import server

STATIC = Path("src/mockboard/voice/static")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "site.css").read_text(encoding="utf-8")
FONTS_CSS = (STATIC / "fonts.css").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = JS.index(f"function {name}(")
    return JS[start : JS.index("\n}\n", start)]


def test_1_heard_rows_show_an_arrow_and_open_when_something_was_likely_misheard():
    assert "summary::before" in CSS and ".heard-row[open] summary::before" in CSS
    body = _function("renderHeard")
    assert "item.open = true" in body
    assert body.index("row.likely.length") < body.index("item.open = true")


def test_2_on_a_phone_the_captions_come_before_the_brain():
    phone = CSS[CSS.index("@media (max-width: 900px)") :]
    areas = re.search(r'#view-room \{.*?grid-template-areas:([^;]+);', phone, re.S).group(1)
    names = re.findall(r'"(\w+)"', areas)
    assert names.index("act") < names.index("caps") < names.index("stage")


def test_3_no_talk_button_during_a_sitting_and_debate_still_starts_from_it():
    assert re.search(r'<button id="start"[^>]*\bhidden\b[^>]*>Start again</button>', PAGE)
    start = _function("start")
    assert '$("start").hidden = true' in start
    assert '"Start again"' in JS and '$("start").hidden = false' in JS
    begin = JS[JS.index('$("begin").addEventListener') :]
    begin = begin[: begin.index("});")]
    assert 'cfg.mode !== "upsc"' in begin and '"Start speaking"' in begin


def test_4_captions_keep_the_segment_id_but_do_not_show_it():
    body = _function("caption")
    assert "div.dataset.segment" in body
    assert '" · " + segment' not in body and "textContent = segment" not in body
    assert ".cap .segment" not in CSS


def test_5_the_home_disclaimer_has_its_own_backing():
    block = CSS[CSS.index(".hero-honest {") :]
    block = block[: block.index("}")]
    assert "background:" in block and "backdrop-filter" in block


def test_6_the_page_calls_no_outside_host_for_fonts_or_anything_else():
    for tag in re.findall(r"<(?:link|script)\b[^>]*>", PAGE):
        assert "http://" not in tag and "https://" not in tag, tag
    assert "googleapis" not in PAGE and "gstatic" not in PAGE
    assert '<link rel="stylesheet" href="/static/fonts.css">' in PAGE
    urls = re.findall(r'url\("(/static/fonts/[^"]+)"\)', FONTS_CSS)
    assert len(urls) == 6
    for url in urls:
        assert (STATIC / url.removeprefix("/static/")).read_bytes()[:4] == b"wOF2"
    for family in ("Geist", "Geist-Mono", "Noto-Sans-Devanagari", "Noto-Sans-Tamil"):
        assert "SIL OPEN FONT LICENSE" in (STATIC / "fonts" / f"OFL-{family}.txt").read_text()


def test_6_the_server_serves_the_fonts_and_nothing_else_in_that_folder(settings):
    cfg = server.parse_args(["--mode", "upsc", "--fake-llm", "--fake-speech"])
    cfg.settings = settings
    client = TestClient(server.create_app(cfg))
    assert client.get("/static/fonts.css").status_code == 200
    font = client.get("/static/fonts/geist-latin.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"
    assert client.get("/static/fonts/OFL-Geist.txt").status_code == 404
    assert client.get("/static/fonts/other.woff2").status_code == 404  # exact names only
    assert client.get("/static/lab.html").status_code == 404
