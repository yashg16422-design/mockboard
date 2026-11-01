"""The voice page is static HTML. These checks do not open a browser or a server."""

from pathlib import Path

PAGE = Path("src/mockboard/voice/static/index.html").read_text(encoding="utf-8")


def test_home_says_what_this_is_and_how_a_session_works():
    assert "The interview, spoken." in PAGE
    assert "UPSC Personality Test" in PAGE
    assert "not a UPSC mark" in PAGE
    assert "Union Public Service Commission" in PAGE
    assert "Gnani Prisma" in PAGE
    assert "Gnani Timbre" in PAGE
    assert "Amazon Nova Pro" in PAGE
    assert 'id="begin"' in PAGE
    assert "How a session works" in PAGE


def test_page_keeps_the_voice_client_contract():
    # tests/voice/test_voice_app.py reads these from the served page.
    assert "Mock Board" in PAGE
    assert "headphones" in PAGE
    assert 'rel="icon"' in PAGE
    assert "page_id" in PAGE
