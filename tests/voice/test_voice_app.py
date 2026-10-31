"""The local voice app (V3 item 5): config, page, form checks, session building for both
modes, and the trace summary. Offline; the WebRTC path itself needs a browser (manual).
Skipped without the `voice` extra."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pipecat")
pytest.importorskip("aiortc")

from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.voice.test_brain_processor import Rig, upsc_session

from mockboard.speech.keyterms import speech_context
from mockboard.voice import server, summarize
from mockboard.voice.brain_processor import LISTENING
from mockboard.voice.lines import PlaybackLedger


def cfg_for(settings, argv):
    cfg = server.parse_args(argv)
    cfg.settings = settings
    return cfg


def test_parse_args_and_public_config(settings):
    cfg = cfg_for(settings, ["--mode", "debate", "--motion", "1", "--side", "against",
                             "--fake-llm", "--fake-speech"])
    assert cfg.mode == "debate" and cfg.side == "against" and cfg.motion.number == 1
    pub = cfg.public()
    assert pub["mode"] == "debate" and pub["motion"].startswith("This House")
    assert "gnani" not in json.dumps(pub).lower()  # nothing secret or path-like
    assert "api_key" not in repr(cfg)
    with pytest.raises(SystemExit):
        server.parse_args(["--mode", "debate"])  # needs --motion and --side
    with pytest.raises(SystemExit):
        server.parse_args(["--mode", "debate", "--motion", "99", "--side", "for"])


def test_page_config_and_form_validation(settings):
    cfg = cfg_for(settings, ["--mode", "upsc", "--fake-speech"])  # real LLM: no default form
    client = TestClient(server.create_app(cfg))
    page = client.get("/")
    assert page.status_code == 200 and "Mock Board" in page.text and "headphones" in page.text
    assert client.get("/api/config").json()["mode"] == "upsc"
    assert client.get("/favicon.ico").status_code == 204  # no 404 line in the log
    assert 'rel="icon"' in page.text and "page_id" in page.text  # defect D: one offer/page
    bad = client.post("/api/offer", json={"sdp": "x", "type": "offer",
                                          "request_data": {"form": {"home_state": "U"}}})
    assert bad.status_code == 400 and bad.json()["error"] == "form"


def test_form_reads_only_known_fields_and_needs_the_required_ones():
    form = server.parse_form({"home_state": "Uttar Pradesh", "degree_subject": "Electronics",
                              "hobbies": "chess, cycling", "name": "Someone"},
                             allow_default=False)
    assert form.hobbies == ["chess", "cycling"] and not hasattr(form, "name")
    assert server.parse_form({}, allow_default=True).home_state  # fake-llm default form
    with pytest.raises(ValidationError):
        server.parse_form({}, allow_default=False)


def test_sessions_and_speech_are_built_for_both_modes(settings):
    upsc = cfg_for(settings, ["--mode", "upsc", "--fake-llm", "--fake-speech"])
    s = server.make_session(upsc, server.parse_form({}, allow_default=True))
    assert s.mode == "upsc_interview" and s.session_id.startswith("voice-")
    stt, tts = server.make_speech(upsc, s, PlaybackLedger())
    assert type(stt).__name__ == "FakeCommitSTT" and tts.ledger is not None

    deb = cfg_for(settings, ["--mode", "debate", "--motion", "2", "--side", "for",
                             "--fake-llm", "--fake-speech"])
    d = server.make_session(deb, None)
    assert d.mode == "debate" and d.form is None
    assert d.expects_long_turn  # the opening statement comes first (CTO_NOTES_04 §7)


def test_real_speech_services_get_session_ids_and_context(settings):
    from mockboard.config import Settings

    s2 = Settings(_env_file=None, gnani_api_key="k-test", stt_model="prisma-v3.0",
                  trace_dir=settings.trace_dir)
    cfg = cfg_for(s2, ["--mode", "upsc", "--fake-llm"])
    session = server.make_session(cfg, server.parse_form({}, allow_default=True))
    stt, tts = server.make_speech(cfg, session, PlaybackLedger())
    assert stt._request_id_prefix == session.session_id
    assert "UPSC" in stt.config.keyterms and stt.config.topic.startswith("A UPSC")
    assert tts.ledger is not None and tts._settings.voice == "Poorvi"


def test_speech_context_per_topic_and_for_a_motion(settings, cards, fake_llm, form, tmp_path):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    terms, topic = speech_context(s)
    assert "Kanpur Nagar" in terms and "tourism" in topic
    s.state.topic_index = s.state.topics.index("internal_security")
    assert "Naxalism" in speech_context(s)[0]
    deb = server.make_session(cfg_for(settings, ["--mode", "debate", "--motion", "1",
                                                 "--side", "for", "--fake-llm"]), None)
    terms, topic = speech_context(deb)
    assert "distraction" in terms and topic.startswith("A friendly debate")


async def test_summary_prints_timings_without_any_text(settings, cards, fake_llm, form,
                                                       tmp_path, capsys):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    async with Rig(s) as rig:
        await rig.until(lambda: rig.brain.state == LISTENING)
        await rig.speak()
        await rig.until(lambda: rig.brain.outcomes)
    path = s.recorder.path
    assert summarize.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "STOP->FIRST AUDIO" in out and "p95" in out and "commit transcript" in out
    said = rig.brain.outcomes[0].trace["voice"]["board_line_spoken"]
    assert said.split()[0] not in out.split()  # no board text
    assert "Kanpur" not in out  # no candidate text
