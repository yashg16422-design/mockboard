"""The page's mode picker (Nov 7, owner): one server offers the UPSC interview, debate and the
study partner, and each sitting says which it wants. A page that sends no mode gets the server's
`--mode`, as before."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mockboard.voice import server

STATIC = Path("src/mockboard/voice/static")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def cfg_for(settings, argv):
    cfg = server.parse_args(argv)
    cfg.settings = settings
    return cfg


@pytest.fixture
def upsc_cfg(settings):
    return cfg_for(settings, ["--mode", "upsc", "--fake-llm", "--fake-speech"])


def test_no_mode_is_the_servers_own_mode(upsc_cfg, settings):
    sitting = server.parse_sitting({"form": {"home_state": "Goa", "degree_subject": "History"}},
                                   upsc_cfg)
    assert sitting.mode == "upsc" and sitting.form.home_state == "Goa"
    debate_cfg = cfg_for(settings, ["--mode", "debate", "--motion", "1", "--side", "for",
                                    "--fake-llm", "--fake-speech"])
    sitting = server.parse_sitting({}, debate_cfg)
    assert sitting.mode == "debate" and sitting.motion.number == 1 and sitting.side == "for"


def test_an_upsc_server_runs_a_chosen_debate(upsc_cfg):
    sitting = server.parse_sitting(
        {"mode": "debate", "debate": {"motion": "3", "side": "against", "language": "english",
                                      "opponent": "wit", "length": "quick"}},
        upsc_cfg,
    )
    assert sitting.mode == "debate" and sitting.motion.number == 3
    assert (sitting.side, sitting.language, sitting.opponent) == ("against", "english", "wit")
    assert sitting.speech_language == "english" and sitting.form is None


@pytest.mark.parametrize(("debate", "problem"), [
    ({}, "motion"),
    ({"motion": "99"}, "motion"),
    ({"motion": "1", "side": "maybe"}, "side"),
    ({"motion": "1", "language": "tanglish"}, "language"),
    ({"motion": "1", "opponent": "bully"}, "opponent"),
    ({"motion": "1", "length": "marathon"}, "length"),
])  # fmt: skip
def test_a_bad_debate_setup_is_refused_in_plain_words(upsc_cfg, debate, problem):
    with pytest.raises(server.SittingError) as exc:
        server.parse_sitting({"mode": "debate", "debate": debate}, upsc_cfg)
    assert any(p.startswith(problem) for p in exc.value.problems)


def test_an_unknown_mode_and_a_bad_form_are_refused(upsc_cfg):
    with pytest.raises(server.SittingError):
        server.parse_sitting({"mode": "karaoke"}, upsc_cfg)
    with pytest.raises(server.SittingError) as exc:
        server.parse_sitting({"mode": "upsc", "form": {"home_state": "G"}}, upsc_cfg)
    assert exc.value.problems


def test_the_offer_route_refuses_a_bad_sitting_before_any_webrtc(upsc_cfg):
    from fastapi.testclient import TestClient

    client = TestClient(server.create_app(upsc_cfg))
    bad = client.post("/api/offer", json={"sdp": "x", "type": "offer",
                                          "request_data": {"mode": "debate", "debate": {}}})
    assert bad.status_code == 400 and bad.json()["error"] == "form"
    assert any("motion" in p for p in bad.json()["problems"])


def test_the_page_config_lists_modes_and_motions(upsc_cfg):
    public = upsc_cfg.public()
    assert public["modes"][:2] == ["upsc", "debate"]
    assert len(public["motions"]) >= 10
    assert all(set(m) == {"number", "title", "text"} for m in public["motions"])
    assert all(m["text"].startswith("This House") for m in public["motions"])


def test_the_page_has_the_chooser_and_both_setup_screens():
    for view in ("choose", "debate", "partner"):
        assert f'id="view-{view}"' in PAGE
    assert PAGE.count('class="mode-card"') == 3
    for mode in ("upsc", "debate", "partner"):
        assert f'data-mode="{mode}"' in PAGE
    assert 'id="debate-form"' in PAGE and 'id="partner-form"' in PAGE
    assert 'name="topic"' in PAGE and 'maxlength="80"' in PAGE
    # no identifier fields anywhere
    for banned in ('name="name"', 'type="email"', 'name="phone"', 'name="college"'):
        assert banned not in PAGE


def test_the_page_sends_the_chosen_mode_with_its_setup():
    assert 'mode: "debate", debate: sitting.debate' in JS
    assert 'mode: "partner", partner: sitting.partner' in JS
    assert re.search(r"function chooseMode\(mode\)", JS)


def test_every_spoken_hint_on_the_setup_screens_is_recognised():
    from mockboard.brain.requests import detect_request
    from mockboard.brain.think_time import is_think_request

    for view in ("debate", "partner"):
        start = PAGE.index(f'id="view-{view}"')
        block = PAGE[start : PAGE.index("</section>", start)]
        for phrase in re.findall(r"“([^”]+)”", block):
            assert detect_request(phrase) is not None or is_think_request(phrase), phrase


def test_the_prisma_locale_follows_the_sitting_language(settings):
    """Found live on generated speech (Nov 9): an English sitting on hi-IN missed every spoken
    request ("could you repeat the question" came back in Devanagari)."""
    from mockboard.speech.mic_check import lang_code_for

    for configured in ("en-IN", "hi-IN"):  # .env.example used to say en-IN
        s = settings.model_copy(update={"stt_lang_code": configured})
        assert lang_code_for("english", s) == "en-IN"
        assert lang_code_for("tanglish", s) == "ta-IN"
        assert lang_code_for("hinglish", s) == "hi-IN"
        assert lang_code_for("hindi", s) == "hi-IN"
    ab = settings.model_copy(update={"stt_lang_code": "hi-IN,en-IN"})
    assert lang_code_for("hinglish", ab) == "hi-IN,en-IN"
