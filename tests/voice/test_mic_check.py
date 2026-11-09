"""Upgrade #2: the microphone and language check (`speech/mic_check.py`, `/api/mic-check`).

Offline only: a fake Prisma server and the fake-speech app. No real Gnani call.
"""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path

import pytest

pytest.importorskip("pipecat")

from fastapi.testclient import TestClient
from pydantic import SecretStr
from tests.voice.fake_gnani import FakePrisma
from tests.voice.test_access_routes import app_for, ran  # noqa: F401  (the `ran` fixture)

from mockboard.speech import mic_check
from mockboard.speech.mic_check import CHECK_LINES, rms, score, transcribe_once
from mockboard.voice import server

STATIC = Path(__file__).resolve().parents[2] / "src/mockboard/voice/static"


def tone(seconds: float = 1.0, amplitude: int = 6000, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 220 * i / rate))) for i in range(n)
    )


SILENCE = b"\x00\x00" * 16000


# --- scoring ----------------------------------------------------------------------------------


@pytest.mark.parametrize("language", list(CHECK_LINES))
def test_the_exact_line_is_clear_in_every_language(language: str):
    result = score(language, CHECK_LINES[language])
    assert result.verdict == "clear" and result.match_ratio == 1.0 and not result.missing


def test_part_of_the_line_is_partly_heard_and_names_the_missing_words():
    result = score("english", "Clean drinking water is the biggest")
    assert result.verdict == "partly" and "district" in result.missing


def test_other_words_are_not_understood():
    assert score("english", "the weather is pleasant today").verdict == "unclear"


def test_small_recognition_slips_still_count_as_matched():
    """Prisma wrote "பிரச்சனை" for "பிரச்சினை" on Nov 6; near spellings must not fail a check."""
    heard = "என் மாவட்டத்தில் குடிநீர் தான் மிகப்பெரிய சவால்"
    assert score("tanglish", heard).verdict == "clear"
    assert score("hinglish", "mere zile me saf pani sabse badi chunauti hai").verdict == "clear"


def test_silence_and_errors_have_their_own_verdicts_and_advice():
    assert score("hindi", "", silent=True).verdict == "silent"
    error = score("hindi", "", error=True)
    assert error.verdict == "error" and "You can still begin" in error.advice


def test_lines_are_neutral_short_and_have_no_digits():
    for line in CHECK_LINES.values():
        assert not re.search(r"\d", line) and len(line.split()) <= 10
        assert "chahta" not in line and "चाहता" not in line  # no gendered verb


def test_the_page_reads_the_same_lines_as_the_server():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const MIC_LINES = \{(.*?)\n\};", app, re.S)
    assert block
    page_lines = dict(re.findall(r'^\s*(\w+): "(.*)",$', block.group(1), re.M))
    assert page_lines == CHECK_LINES


def test_the_page_builds_results_with_text_only_and_releases_the_microphone():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    body = app[app.index("function showMicResult") : app.index("async function startMicCheck")]
    assert "innerHTML" not in body
    assert 'getTracks().forEach((t) => t.stop())' in body
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="mic-result" aria-live="polite"' in html and "Nothing is saved" in html


def test_rms_tells_speech_from_silence():
    assert rms(SILENCE) == 0 and rms(tone()) > mic_check.MIN_SPEECH_RMS


# --- one Prisma stream, against the fake server ----------------------------------------------


async def test_transcribe_once_streams_the_clip_and_closes_the_socket(settings):
    async with FakePrisma(segment_frames=10) as fake:
        text, latency = await transcribe_once(
            tone(1.0), settings, lang_code="ta-IN", api_key="test-key", url=fake.url,
            realtime=False, idle_timeout_s=0.5,
        )
        conn = fake.conns[0]
        assert conn.headers["language_code"] == "ta-IN"
        await conn.closed.wait()
    assert text.startswith("segment 1")
    assert latency is None or latency >= 0


async def test_a_refused_socket_raises_so_the_route_can_say_error(settings):
    async with FakePrisma(refuse_with_1008=True) as fake:
        with pytest.raises(ConnectionError):
            await transcribe_once(tone(0.5), settings, lang_code="hi-IN", api_key="k",
                                  url=fake.url, realtime=False, idle_timeout_s=0.3)


# --- the route ---------------------------------------------------------------------------------


def post(client: TestClient, pcm: bytes, language: str = "hinglish"):
    return client.post(f"/api/mic-check?language={language}", content=pcm,
                       headers={"Content-Type": "application/octet-stream"})


def test_the_demo_route_says_it_is_a_demo(settings):
    with TestClient(app_for(settings)) as client:
        body = post(client, tone()).json()
    assert body["verdict"] == "clear" and body["demo"] is True
    assert body["heard"] == CHECK_LINES["hinglish"]


def test_silence_is_answered_without_a_speech_call(settings, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no Prisma call for silence")

    monkeypatch.setattr(mic_check, "transcribe_once", boom)
    with TestClient(app_for(settings)) as client:
        assert post(client, SILENCE, "tanglish").json()["verdict"] == "silent"


def test_bad_language_too_long_or_odd_audio_is_refused(settings):
    with TestClient(app_for(settings)) as client:
        assert post(client, tone(), "klingon").status_code == 400
        assert post(client, tone(11.0)).status_code == 413
        assert post(client, b"\x01\x02\x03").status_code == 413


def test_debate_has_no_mic_check(settings):
    cfg = server.parse_args(
        ["--mode", "debate", "--motion", "1", "--side", "for", "--fake-llm", "--fake-speech"]
    )
    cfg.settings = settings
    with TestClient(server.create_app(cfg)) as client:
        assert post(client, tone()).status_code == 404


def real_speech_app(settings):
    cfg = server.parse_args(["--mode", "upsc", "--fake-llm", "--fake-speech"])
    cfg.fake_speech = False  # the route's real path, with Prisma replaced below
    cfg.settings = settings.model_copy(update={"gnani_api_key": SecretStr("test-key")})
    return server.create_app(cfg)


def test_real_checks_are_counted_and_stop_politely_at_the_cap(ran, settings, monkeypatch):  # noqa: F811
    calls: list[str] = []

    async def fake_transcribe(pcm, s, *, lang_code, api_key, **kw):
        calls.append(lang_code)
        return CHECK_LINES["tanglish"], 640

    monkeypatch.setattr(mic_check, "transcribe_once", fake_transcribe)
    with TestClient(real_speech_app(settings)) as client:
        bodies = [post(client, tone(), "tanglish").json() for _ in range(4)]
    assert [b.get("verdict") for b in bodies[:3]] == ["clear"] * 3
    assert bodies[0]["lang_code"] == "ta-IN" and bodies[0]["latency_ms"] == 640
    assert "demo" not in bodies[0]
    assert len(calls) == 3  # the fourth was not sent to Prisma
    message = bodies[3]["limit"]["message"].lower()
    assert "not affected" in message
    for word in ("error", "denied", "blocked"):
        assert word not in message


def test_a_speech_service_failure_is_reported_honestly(settings, monkeypatch):
    async def failing(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(mic_check, "transcribe_once", failing)
    with TestClient(real_speech_app(settings)) as client:
        body = post(client, tone(), "english").json()
    assert body["verdict"] == "error" and body["heard"] == ""


def test_hinglish_heard_in_devanagari_is_clear_and_says_why():
    """The owner's own check (Nov 6): STT_LANG_CODE=hi-IN writes Hinglish in Devanagari."""
    result = score("hinglish", "मेरे जिले में साफ पानी सबसे बड़ी चुनौती है")
    assert result.verdict == "clear" and "Devanagari" in result.script_note
    assert score("hinglish", CHECK_LINES["hinglish"]).script_note == ""


def test_the_page_shows_the_script_note():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "r.script_note" in app
