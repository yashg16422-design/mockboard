"""Ruling 38: the voice lab, against the fake Timbre server and in fake mode (no network)."""

from __future__ import annotations

import wave

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from tests.voice.fake_gnani import FakeTimbre

from mockboard.config import Settings
from mockboard.voice import lab

KEY = "lab-key-SECRET-38"


def variant(**kw) -> lab.Variant:
    d = {"line": 1, **lab.BASELINE} | kw
    return lab.Variant.from_dict(d)


async def test_whole_line_is_one_request_and_per_sentence_is_one_each():
    async with FakeTimbre(chunks=3, chunk_bytes=640, chunk_gap_s=0.0) as server:
        whole = await lab.synthesize(variant(unit="line", model="timbre-v3.0", voice="Yashraj",
                                             language="hi-IN", rate=24000),
                                     key=KEY, url=server.url)
        each = await lab.synthesize(variant(unit="sentence", language="hi-en"),
                                    key=KEY, url=server.url)
    first, second = server.conns
    assert len(first.requests) == 1 and len(second.requests) == 3  # line 2 has 3 sentences
    req = first.requests[0]
    assert (req["model"], req["voice"], req["language"]) == ("timbre-v3.0", "Yashraj", "hi-IN")
    assert req["audio_config"]["sample_rate"] == 24000 and req["text"] == lab.LINES[1]["latin"]
    assert second.requests[0]["language"] == "hi-en"  # the lab may send it; the board may not
    assert first.headers["x-api-key-id"] == KEY
    assert len(whole.pcm) == 3 * 640 and len(each.pcm) == 3 * 3 * 640
    assert all(r["first_byte_ms"] is not None and r["error"] is None for r in each.requests)


async def test_a_timbre_error_is_reported_and_not_cached(tmp_path, monkeypatch):
    monkeypatch.setitem(lab.LINES[0], "latin", "FAIL. Yeh line nahi banegi.")
    async with FakeTimbre(chunks=2, chunk_bytes=640, chunk_gap_s=0.0) as server:
        lb = lab.VoiceLab(Settings(_env_file=None, gnani_api_key=KEY), out_dir=tmp_path,
                          url=server.url)
        meta = await lb.render(variant(line=0))
    assert meta["error"] and not list(tmp_path.glob("*.wav"))


def test_settings_are_checked_against_the_catalog():
    with pytest.raises(ValueError, match="not a Timbre"):
        variant(model="timbre-v3.0", voice="Poorvi")
    with pytest.raises(ValueError, match="rate"):
        variant(rate=12345)
    v = variant(model="timbre-v3.0", voice="Avinash", unit="line", rate=24000)
    assert v.env() == ("TTS_MODEL=timbre-v3.0 TTS_VOICE=Avinash TTS_LANGUAGE=auto "
                       "TTS_SAMPLE_RATE=24000 TTS_REQUEST_UNIT=line")
    assert v.id != variant().id and len(v.id) == 16


def test_the_page_renders_caches_and_serves_audio_in_fake_mode(tmp_path, capsys):
    lb = lab.VoiceLab(Settings(_env_file=None), out_dir=tmp_path, fake=True)
    client = TestClient(lab.create_app(lb))
    assert "voice lab" in client.get("/").text
    cfg = client.get("/api/config").json()
    assert cfg["fake"] and len(cfg["lines"]) == 3 and len(cfg["voices"]) == 6
    assert {v["model"] for v in cfg["voices"]} == {"timbre-v2.5", "timbre-v3.0"}
    body = {"line": 2, **lab.BASELINE, "unit": "line", "rate": 24000}
    first = client.post("/api/render", json=body).json()
    again = client.post("/api/render", json=body).json()
    assert not first["cached"] and again["cached"] and first["id"] == again["id"]
    assert lb.renders == 1 and first["chars_sent"] == len(lab.LINES[2]["latin"])
    wav = client.get(first["url"])
    assert wav.status_code == 200 and wav.headers["content-type"] == "audio/wav"
    with wave.open(str(tmp_path / f"{first['id']}.wav")) as w:
        assert w.getframerate() == 24000
    assert client.get("/api/audio/../../etc").status_code == 404
    assert client.post("/api/render", json={**body, "voice": "Nobody"}).status_code == 400


def test_the_key_is_never_shown(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GNANI_API_KEY", KEY)
    lb = lab.VoiceLab(Settings(_env_file=None), out_dir=tmp_path)
    assert lb.has_key() and KEY not in str(lb.public())
    monkeypatch.delenv("GNANI_API_KEY")
    assert lab.main(["--out", str(tmp_path)]) == 1  # no key, not fake: refuses to start
    assert KEY not in capsys.readouterr().err
