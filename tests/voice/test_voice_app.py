"""The local voice app (V3 item 5): config, page, form checks, session building for both
modes, and the trace summary. Offline; the WebRTC path itself needs a browser (manual).
Skipped without the `voice` extra."""

from __future__ import annotations

import itertools
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


def test_the_server_refuses_max_turns_for_the_panel_and_keeps_it_for_debate(capsys):
    """Ruling 76 item 3 (CTO_NOTES_16 S3): the panel sets its own turn cap, so a flag that would be
    silently ignored is refused, as the text CLI refuses it."""
    for argv in (["--mode", "upsc", "--max-turns", "4"],
                 ["--max-turns", "4"]):  # upsc is the default mode
        with pytest.raises(SystemExit) as exc:
            server.parse_args(argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--max-turns" in err and "ACCESS_MAX_ANSWERS" in err
    debate = ["--mode", "debate", "--motion", "1", "--side", "for"]
    assert server.parse_args([*debate, "--max-turns", "4"]).max_turns == 4
    assert server.parse_args(debate).max_turns == 9
    assert server.parse_args(["--mode", "upsc"]).max_turns == 9  # unused: the agenda decides
    quickstart = (server.HERE.parents[2] / "docs" / "VOICE_QUICKSTART.md").read_text("utf-8")
    advertised = [line for line in quickstart.splitlines() if "--max-turns 4" in line]
    assert advertised and all("debate only" in line for line in advertised)


# --- P8: the panel's voices -----------------------------------------------------------------


def test_the_default_panel_voices_are_lab_voices_that_pass_the_catalog_and_never_clash(settings):
    from mockboard.brain.agenda import SEGMENT_MEMBERS
    from mockboard.config import DEFAULT_PANEL_VOICES
    from mockboard.speech.gnani_protocol import check_voice
    from mockboard.voice.lab import VOICES as LAB_VOICES

    members = ["chairman", *SEGMENT_MEMBERS.values()]
    seats = list(dict.fromkeys(members))  # chairman, then each segment's member in order
    assert len(seats) == 5
    for model, table in DEFAULT_PANEL_VOICES.items():
        assert set(table) == set(seats), model
        for voice in table.values():  # nothing invented: the voice lab already offers it
            check_voice(model, voice)
            assert (model, voice) in {(m, v) for m, v, _note in LAB_VOICES}
        chair = table["chairman"]
        members_only = [table[m] for m in seats[1:]]
        assert chair not in members_only  # she speaks every hand-off, then a member answers
        assert all(a != b for a, b in itertools.pairwise(members_only))
    assert settings.tts_model in DEFAULT_PANEL_VOICES


def test_the_panel_voice_table_comes_from_the_model_unless_the_env_says_otherwise(monkeypatch):
    from mockboard.config import DEFAULT_PANEL_VOICES, Settings

    assert Settings(_env_file=None).panel_voices() == DEFAULT_PANEL_VOICES["timbre-v2.5"]
    v3 = Settings(_env_file=None, tts_model="timbre-v3.0", tts_voice="Avinash")
    assert v3.panel_voices() == DEFAULT_PANEL_VOICES["timbre-v3.0"]
    monkeypatch.setenv("TTS_PANEL_VOICES", "{}")
    assert Settings(_env_file=None).panel_voices() == {}  # one voice, as before P8
    monkeypatch.setenv("TTS_PANEL_VOICES", '{"chairman": "Deepak"}')
    assert Settings(_env_file=None).panel_voices() == {"chairman": "Deepak"}


def test_the_server_gives_the_panel_its_voices_and_debate_or_an_explicit_voice_none(settings):
    upsc = cfg_for(settings, ["--mode", "upsc", "--fake-llm", "--fake-speech"])
    assert upsc.panel_voices == settings.panel_voices() and upsc.panel_voices
    pick = upsc.voice_for()
    assert pick("chairman") == "Vikrant" and pick("ips_officer") == "Urmila"
    assert pick("someone_new") == upsc.voice  # a member the table leaves out speaks in TTS_VOICE
    one = cfg_for(settings, ["--mode", "upsc", "--voice", "Vikrant", "--fake-llm", "--fake-speech"])
    assert one.panel_voices == {} and one.voice_for() is None
    debate = cfg_for(settings, ["--mode", "debate", "--motion", "1", "--side", "for"])
    assert debate.panel_voices == {} and debate.voice_for() is None
    assert "panel_voices" not in json.dumps(upsc.public())  # nothing for the page to show


def test_a_panel_voice_the_model_does_not_have_stops_the_server_before_it_starts(settings):
    cfg = cfg_for(settings, ["--mode", "upsc", "--fake-llm", "--fake-speech"])
    server.check_panel_voices(cfg)  # the defaults pass
    cfg.panel_voices = {**cfg.panel_voices, "economist": "Yashraj"}  # a v3.0 voice on v2.5
    with pytest.raises(SystemExit) as exc:
        server.check_panel_voices(cfg)
    assert "economist" in str(exc.value) and "TTS_PANEL_VOICES" in str(exc.value)


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


def test_tanglish_session_pins_prisma_to_tamil(settings):
    from mockboard.config import Settings

    s2 = Settings(_env_file=None, gnani_api_key="k-test", stt_model="prisma-v3.0",
                  trace_dir=settings.trace_dir)
    cfg = cfg_for(s2, ["--mode", "upsc", "--fake-llm"])
    form = server.parse_form({"home_state": "Tamil Nadu", "degree_subject": "Physics",
                              "interview_language": "tanglish"}, allow_default=False)
    session = server.make_session(cfg, form)
    stt, _tts = server.make_speech(cfg, session, PlaybackLedger())
    assert stt.config.lang_code == "ta-IN"


def test_speech_context_per_topic_and_for_a_motion(settings, cards, fake_llm, form, tmp_path):
    s = upsc_session(settings, cards, fake_llm, form, tmp_path)
    terms, topic = speech_context(s)
    assert "Kanpur Nagar" in terms and topic.startswith("A UPSC")
    assert "panchayat" in terms and "Superintendent of Police" not in terms  # form_hr
    s.state.topic_index = s.state.topics.index("current_affairs")
    later = speech_context(s)[0]
    assert "Superintendent of Police" in later and "Kanpur Nagar" in later
    assert "Siang" not in later and "Siang" not in terms  # the paper is not the interview
    s.hooks._raw = "I have been following the Siang dam."
    heard = speech_context(s)[0]
    assert "Siang" in heard and "gig" not in heard
    # P7: the words follow the panel's segments, and no old fixed topic is in them.
    assert not {"Naxalism", "tourism", "heritage"} & set(later + terms)
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
    assert "final wait" in out and "all in" in out
    said = rig.brain.outcomes[0].trace["voice"]["board_line_spoken"]
    assert said.split()[0] not in out.split()  # no board text
    assert "Kanpur" not in out  # no candidate text
