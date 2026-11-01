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


def test_limit_notice_does_not_blame_the_visitor():
    js = Path("src/mockboard/voice/static/app.js").read_text(encoding="utf-8")
    start = js.index("function copyFree")
    end = js.index("function renderAccess")
    notice = js[start:end].lower()
    assert "thank you for trying mock board" in notice
    assert "today's test interviews are all taken" in notice
    for word in ("error", "denied", "blocked"):
        assert word not in notice


def test_the_field_follows_the_pointer_and_the_voice():
    assert 'id="home-field"' in PAGE
    assert 'id="room-field"' in PAGE
    js = Path("src/mockboard/voice/static/presence.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js
    assert "pointermove" in js
    app = Path("src/mockboard/voice/static/app.js").read_text(encoding="utf-8")
    assert "Presence.show" in app
    assert "Presence.who" in app
    assert "Presence.level" in app
    assert "Presence.pulse" in app


def test_page_keeps_the_voice_client_contract():
    # tests/voice/test_voice_app.py reads these from the served page.
    assert "Mock Board" in PAGE
    assert "headphones" in PAGE
    assert 'rel="icon"' in PAGE
    assert "page_id" in PAGE


def _js() -> str:
    return Path("src/mockboard/voice/static/app.js").read_text(encoding="utf-8")


def test_the_page_says_where_the_voice_goes():
    assert "The microphone stays on this page" not in PAGE
    assert PAGE.count("Your voice goes to our server and Gnani Prisma") == 2
    assert "It reads where you are" not in PAGE


def test_a_server_path_is_shown_only_on_a_local_run():
    js = _js()
    assert "m.path && local" in js and "!access.enabled" in js


def test_the_answer_events_replace_the_invented_one():
    js = _js()
    assert "answer_cap" not in js
    assert 'case "answers"' in js and 'case "closing"' in js
    assert '"Answer " + m.used + " of " + m.limit' in js
    assert 'id="answer-count"' in PAGE
    assert "do not count, up to 4" in js


def test_server_ended_sessions_close_the_microphone_and_refresh_access():
    js = _js()
    start = js.index("function sessionOver")
    body = js[start:js.index("async function connectionLost")]
    assert "stop();" in body and "loadAccess();" in body
    assert js.count("sessionOver();") == 2


def test_the_code_box_stays_usable_and_the_reset_time_is_ist():
    js = _js()
    assert "input:not(#access-code)" in js
    assert 'timeZone: "Asia/Kolkata"' in js
    assert '"/api/access"' in js and "Please try again in a few minutes." in js


def test_contrast_and_the_active_seat():
    tokens = Path("src/mockboard/voice/static/tokens.css").read_text(encoding="utf-8")
    assert "--faint: #8a8a93" in tokens
    assert "aria-current" in _js()
