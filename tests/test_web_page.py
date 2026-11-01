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


def test_form_is_one_column_of_the_contract_fields():
    assert 'name="home_state"' in PAGE
    assert 'name="district"' in PAGE
    assert 'name="degree_subject"' in PAGE
    assert 'name="optional_subject"' in PAGE
    assert 'name="work_experience"' in PAGE
    assert 'name="interview_language"' in PAGE
    assert "kisi aur topic pe baat karte hain" in PAGE
    assert "Likely questions" in PAGE
    assert "Not available in this version" in PAGE
    assert 'name="name"' not in PAGE
    assert 'type="email"' not in PAGE
    assert 'name="phone"' not in PAGE
    assert 'name="college"' not in PAGE
    for n in ("1", "2", "3", "4", "5"):
        assert f'data-seat="{n}"' in PAGE


def test_webrtc_client_still_posts_one_offer():
    js = Path("src/mockboard/voice/static/app.js").read_text(encoding="utf-8")
    assert "page_id" in js
    assert "/api/offer" in js
    assert 'createDataChannel("messages")' in js
    assert "ping: " in js
    assert "event: \"stop\"" in js or 'event: "stop"' in js


def test_report_can_be_printed_and_grouped_when_the_server_sends_members():
    assert 'id="print"' in PAGE
    assert 'id="report"' in PAGE
    assert 'id="report-grouped"' in PAGE
    js = Path("src/mockboard/voice/static/app.js").read_text(encoding="utf-8")
    assert "weakest_question" in js
    assert "practice_plan" in js
    assert "window.print" in js
    assert "m.text" in js


def test_page_keeps_the_voice_client_contract():
    # tests/voice/test_voice_app.py reads these from the served page.
    assert "Mock Board" in PAGE
    assert "headphones" in PAGE
    assert 'rel="icon"' in PAGE
    assert "page_id" in PAGE
