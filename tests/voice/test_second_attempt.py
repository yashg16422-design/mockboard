"""Upgrade #3: the spoken second attempt at the weakest question.

Offline only: the comparison is pure code; Timbre and Prisma are the project's fake servers, and
the routes run in the fake-speech app or with the speech calls replaced. No real Gnani call.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
from pathlib import Path

import pytest

pytest.importorskip("pipecat")

from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect
from tests.voice.fake_gnani import FakePrisma, FakeTimbre
from tests.voice.test_access_routes import app_for, ran  # noqa: F401  (the `ran` fixture)

from mockboard.brain.second_attempt import CHECKS, compare_attempts, features
from mockboard.speech import replay_speech
from mockboard.speech.replay_speech import PrismaRelay, synthesize
from mockboard.voice import server

STATIC = Path(__file__).resolve().parents[2] / "src/mockboard/voice/static"
QUESTION = "Is soch ke khilaaf sabse vazandar baat kya ho sakti hai, aur uska aap kya karenge?"


def tone(seconds: float = 0.5, rate: int = 16000) -> bytes:
    return b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * i / rate)))
        for i in range(int(seconds * rate))
    )


# --- the comparison (pure) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "present"),
    [
        ("First I would meet the farmers, because they bear the cost.", {"first_step", "affected",
                                                                         "reason", "cost"}),
        ("Sabse pehle logon se baat karunga kyunki unki zaroorat alag hai, lekin samay lagega.",
         {"first_step", "affected", "reason", "other_side"}),
        ("सबसे पहले मैं किसानों से बात करूँगा, क्योंकि नुकसान उन्हीं का है।",
         {"first_step", "affected", "reason", "cost"}),
        ("முதலில் மக்களிடம் பேசுவேன், ஏனென்றால் அவர்கள் தான் பாதிக்கப்படுகிறார்கள். ஆனால் செலவு அதிகம்.",
         {"first_step", "affected", "reason", "other_side", "cost"}),
    ],
)
def test_features_are_found_in_every_language(answer: str, present: set[str]):
    found = {key for key in CHECKS if features(answer)[key]}
    assert found == present


def test_whole_words_only_for_latin_cues():
    """"blog" is not "log", "butter" is not "but", "firstly" counts and "thirst" does not."""
    found = features("I write a blog about butter and thirst")
    assert not found["affected"] and not found["other_side"] and not found["first_step"]


def test_a_better_second_answer_names_what_was_added_and_what_to_try_next():
    result = compare_attempts(
        "Haan, technology se sab theek ho jayega.",
        "Sabse pehle gaon ke logon se poochunga, kyunki unki zaroorat alag hai.",
    )
    assert "said what you would do first" in result.added
    assert "gave a reason" in result.added and "named who is affected" in result.added
    assert result.summary.startswith("This time you")
    assert "Next time, also try:" in result.summary
    assert result.before_words == 7


def test_no_second_answer_is_said_plainly_and_nothing_is_scored():
    result = compare_attempts("Pehle logon se baat karunga.", "").as_json()
    assert result["summary"].startswith("No second answer was heard")
    assert not any("score" in key for key in result)
    assert "not a mark" in result["note"]


def test_a_dropped_part_is_reported():
    result = compare_attempts("First, talk to people, because trust matters.", "Technology.")
    assert "said what you would do first" in result.dropped


# --- speech, against the fake servers ----------------------------------------------------------


async def test_synthesize_returns_audio_and_stops_at_the_first_complete(settings):
    async with FakeTimbre(chunks=3, trailing_complete=True) as fake:
        pcm = await synthesize(QUESTION, "Vikrant", settings, api_key="k", url=fake.url)
    assert len(pcm) > 0 and fake.conns[0].requests[0]["text"] == QUESTION


async def test_synthesize_refuses_empty_or_long_text_without_a_call(settings):
    for text in ("", "x" * (replay_speech.MAX_TTS_CHARS + 1)):
        with pytest.raises(ValueError):
            await synthesize(text, "Vikrant", settings, api_key="k", url="ws://127.0.0.1:9")


async def test_a_timbre_error_raises(settings):
    async with FakeTimbre() as fake:
        with pytest.raises(ConnectionError):
            await synthesize("FAIL please", "Vikrant", settings, api_key="k", url=fake.url)


async def test_the_relay_streams_live_audio_and_closes(settings):
    async with FakePrisma(segment_frames=8) as fake:
        relay = PrismaRelay(settings, lang_code="hi-IN", api_key="k", url=fake.url,
                            idle_timeout_s=0.4)
        await relay.open()
        for _ in range(4):
            await relay.push(tone(0.25))
        heard, latency = await relay.finish()
        await fake.conns[0].closed.wait()
    assert heard.startswith("segment 1")
    assert fake.conns[0].headers["language_code"] == "hi-IN"
    assert latency is None or latency >= 0
    await relay.close()  # safe twice


# --- routes ------------------------------------------------------------------------------------


def start(client: TestClient, **body):
    return client.post("/api/replay/start", json={"segment": "degree", "language": "hinglish",
                                                  **body})


def test_the_demo_flow_is_labelled_and_free(settings):
    with TestClient(app_for(settings)) as client:
        attempt = start(client).json()
        assert attempt["demo"] is True and attempt["attempt"]
        aid = attempt["attempt"]
        spoken = client.post(f"/api/replay/{aid}/question", json={"question": QUESTION})
        assert spoken.status_code == 204
        with client.websocket_connect(f"/ws/replay/{aid}") as ws:
            ws.send_bytes(tone())
            ws.send_text(json.dumps({"type": "done"}))
            reply = ws.receive_json()
        assert reply["demo"] is True and reply["heard"] == server.REPLAY_DEMO_ANSWER
        # one listen per attempt
        url = f"/ws/replay/{aid}"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url) as ws:
            ws.receive_json()


def test_unknown_attempts_bad_input_and_debate_are_refused(settings):
    with TestClient(app_for(settings)) as client:
        missing = client.post("/api/replay/nope/question", json={"question": QUESTION})
        assert missing.status_code == 404
        assert start(client, language="klingon").status_code == 400
        aid = start(client).json()["attempt"]
        assert client.post(f"/api/replay/{aid}/question", json={"question": ""}).status_code == 400
    cfg = server.parse_args(
        ["--mode", "debate", "--motion", "1", "--side", "for", "--fake-llm", "--fake-speech"]
    )
    cfg.settings = settings
    with TestClient(server.create_app(cfg)) as client:
        assert start(client).status_code == 404


def test_compare_route_is_free_and_capped(settings):
    with TestClient(app_for(settings)) as client:
        body = client.post("/api/replay/compare", json={
            "before": "Technology.", "after": "Pehle logon se baat karunga kyunki " + "x " * 5000,
        }).json()
        assert body["added"] and body["after_words"] <= 2100
        assert client.post("/api/replay/compare", content=b"[]").status_code == 400


def real_app(settings, **updates):
    cfg = server.parse_args(["--mode", "upsc", "--fake-llm", "--fake-speech"])
    cfg.fake_speech = False
    cfg.settings = settings.model_copy(update={"gnani_api_key": SecretStr("k"), **updates})
    return server.create_app(cfg)


def test_real_attempts_are_counted_once_each_and_stop_politely(ran, settings, monkeypatch):  # noqa: F811
    async def fake_synth(text, voice, s, *, api_key, **kw):
        return b"\x00\x01" * 800

    monkeypatch.setattr(replay_speech, "synthesize", fake_synth)
    with TestClient(real_app(settings)) as client:
        attempts = [start(client).json() for _ in range(4)]
        assert all("attempt" in a and a["demo"] is False for a in attempts[:3])
        message = attempts[3]["limit"]["message"].lower()
        assert "type a second answer" in message
        for word in ("error", "denied", "blocked"):
            assert word not in message
        res = client.post(f"/api/replay/{attempts[0]['attempt']}/question",
                          json={"question": QUESTION})
        assert res.status_code == 200 and res.headers["content-type"] == "audio/wav"
        assert res.content[:4] == b"RIFF"
        again = client.post(f"/api/replay/{attempts[0]['attempt']}/question",
                            json={"question": QUESTION})
        assert again.status_code == 404  # one spoken question per attempt


def test_a_timbre_failure_falls_back_to_text(settings, monkeypatch):
    async def failing(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(replay_speech, "synthesize", failing)
    with TestClient(real_app(settings)) as client:
        aid = start(client).json()["attempt"]
        res = client.post(f"/api/replay/{aid}/question", json={"question": QUESTION})
    assert res.status_code == 502 and res.json() == {"error": "speech"}


async def test_the_live_listen_relays_to_prisma(settings):
    async with FakePrisma(segment_frames=8) as fake:
        def run() -> dict:
            with TestClient(real_app(settings, gnani_stt_url=fake.url)) as client:
                aid = start(client, language="tanglish").json()["attempt"]
                with client.websocket_connect(f"/ws/replay/{aid}") as ws:
                    for _ in range(4):
                        ws.send_bytes(tone(0.25))
                    ws.send_text(json.dumps({"type": "done"}))
                    return ws.receive_json()

        reply = await asyncio.to_thread(run)
        await fake.conns[0].closed.wait()
    assert reply["heard"].startswith("segment 1")
    assert fake.conns[0].headers["language_code"] == "ta-IN"


async def test_a_prisma_refusal_is_reported_honestly(settings):
    async with FakePrisma(refuse_with_1008=True) as fake:
        def run() -> dict:
            with TestClient(real_app(settings, gnani_stt_url=fake.url)) as client:
                aid = start(client).json()["attempt"]
                with client.websocket_connect(f"/ws/replay/{aid}") as ws:
                    return ws.receive_json()

        reply = await asyncio.to_thread(run)
    assert reply == {"error": "speech"}


# --- the page ----------------------------------------------------------------------------------


def test_the_page_builds_the_attempt_with_text_only_and_releases_the_microphone():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    body = app[app.index("let replayRun = null;") : app.index("// How the board heard each answer")]
    assert "innerHTML" not in body
    assert "getTracks().forEach((t) => t.stop())" in body
    assert "Type my answer instead" in body and "/api/replay/compare" in body


def test_the_report_gives_the_page_the_first_answer():
    from mockboard.brain.judge import fallback_report
    from mockboard.brain.report_panel import page_fields
    from mockboard.brain.schemas import ReportLanguage, WeakestMoment

    report = fallback_report(session_id="s", ledger=[], moves=[], language=ReportLanguage.en,
                             reason="t", feature_flags={}, prompt_versions={})
    report = report.model_copy(update={"weakest": WeakestMoment(
        turn_id="t1", segment="degree", question="Q?", answer="Haan, theek hai.")})
    assert page_fields(report)["weakest"]["answer"] == "Haan, theek hai."


# --- likely-misheard words and what Timbre reads (owner's findings, Nov 6) --------------------


def test_the_demo_repair_makes_no_model_call(settings, monkeypatch):
    from mockboard.brain import heard_repair

    async def boom(*a, **k):
        raise AssertionError("no model call in the demo")

    monkeypatch.setattr(heard_repair, "repairs_for", boom)
    with TestClient(app_for(settings)) as client:
        aid = start(client).json()["attempt"]
        body = client.post(f"/api/replay/{aid}/repair", json={"heard": "क्राउड", "question": "Q?"})
    assert body.json() == {"likely": []}


def test_the_real_repair_runs_once_per_attempt(settings, monkeypatch):
    from mockboard.brain import heard_repair
    from mockboard.brain.heard_repair import Repair

    async def found(answers, **kw):
        return [Repair(1, "क्राउड", "Claude")]

    monkeypatch.setattr(heard_repair, "repairs_for", found)
    with TestClient(real_app(settings)) as client:
        aid = start(client).json()["attempt"]
        first = client.post(f"/api/replay/{aid}/repair", json={"heard": "चैट जीपीटी या क्राउड"})
        second = client.post(f"/api/replay/{aid}/repair", json={"heard": "चैट जीपीटी या क्राउड"})
    assert first.json() == {"likely": [{"heard": "क्राउड", "likely": "Claude"}]}
    assert second.status_code == 404


def test_the_hinglish_question_is_rewritten_for_timbre_only(settings, monkeypatch):
    sent: list[str] = []

    async def fake_synth(text, voice, s, *, api_key, **kw):
        sent.append(text)
        return b"\x00\x01" * 800

    monkeypatch.setattr(replay_speech, "synthesize", fake_synth)
    with TestClient(real_app(settings)) as client:
        aid = start(client).json()["attempt"]
        client.post(f"/api/replay/{aid}/question",
                    json={"question": "Aur aap use kaise sambhalenge?"})
    assert sent == ["Aur aap उसे kaise sambhalenge?"]


def test_the_voice_loops_timbre_service_filters_only_the_request_text():
    from mockboard.speech.timbre_tts import TimbreTTSService
    from mockboard.speech.tts_text import hinglish_for_tts

    tts = TimbreTTSService(api_key="k", voice="Poorvi", language="auto", sample_rate=16000,
                           text_filter=hinglish_for_tts)
    body = tts.request_body("Aap use kaise dekhenge?", 16000)
    assert body["text"] == "Aap उसे kaise dekhenge?"
    plain = TimbreTTSService(api_key="k", voice="Poorvi", language="auto", sample_rate=16000)
    assert plain.request_body("Aap use kaise dekhenge?", 16000)["text"] == "Aap use kaise dekhenge?"


def test_the_report_shows_likely_misheard_words(monkeypatch, capsys):
    from mockboard import cli
    from mockboard.brain import heard_repair
    from mockboard.brain.heard_repair import Repair

    async def found(answers, **kw):
        assert answers  # the sitting's answers, in order
        return [Repair(1, answers[0].split()[0], "Kanpur-Nagar")]

    monkeypatch.setattr(heard_repair, "repairs_for", found)
    rc = cli.main(["--fake", "--script", str(Path(__file__).resolve().parents[2]
                                             / "examples" / "fake_session.txt"),
                   "--session-id", "repair-test", "--no-trace"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "likely misheard: Kanpur -> Kanpur-Nagar" in out
