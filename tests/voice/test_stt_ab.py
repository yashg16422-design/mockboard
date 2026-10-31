"""Ruling 35: the STT A/B tool against the fake Prisma server (websockets only, no Pipecat)."""

from __future__ import annotations

import wave

from tests.voice.fake_gnani import FakePrisma

from mockboard.config import Settings
from mockboard.voice import stt_ab

KEY = "ab-key-SECRET-31"


def write_wav(path, seconds, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00\xf0\xff" * int(seconds * rate / 2))


async def test_each_arm_streams_the_recording_with_its_own_headers(tmp_path):
    settings = Settings(_env_file=None)
    pcm = b"\x10\x00" * 16000  # 1 s
    async with FakePrisma(segment_frames=10, transcript_delay_s=0.01) as server:
        results = [await stt_ab.run_arm(server.url, KEY, settings, arm, pcm, pace=False,
                                        tail_s=0.2, settle_s=2)
                   for arm in stt_ab.ARMS.values()]
    heads = [c.headers for c in server.conns]
    assert [(h["model"], h["language_code"], h["format"]) for h in heads] == [
        ("prisma-v2.5", "en-IN", "verbatim"),
        ("prisma-v2.5", "hi-IN", "verbatim"),
        ("prisma-v3.0", "hi-IN,en-IN", "codemix"),
    ]
    assert all(h["x-min-silence-ms"] == "300" for h in heads)  # the app's own VAD values
    assert all({"type": "commit"} in c.texts for c in server.conns)
    assert all(len(c.frames) >= 31 and {n for _, n in c.frames} == {1024}
               for c in server.conns)
    assert all(r.segments and r.text.startswith("segment 1") for r in results)
    md = stt_ab.render(results, tmp_path / "x.mic.wav")
    assert "v2.5 en-IN" in md and "v3.0 hi-IN,en-IN codemix" in md and "| t |" in md
    assert all(s.wait_ms is not None and s.wait_ms >= 0 for r in results for s in r.segments)
    assert md.count("timing: wall") == 3 and "processing->final median" in md


async def test_the_command_writes_a_table_next_to_the_recording(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "voice-1.mic.wav"
    write_wav(rec, 0.3)
    monkeypatch.setenv("GNANI_API_KEY", KEY)
    async with FakePrisma(segment_frames=5, transcript_delay_s=0.01) as server:
        code = await stt_ab.amain([str(rec), "--only", "en", "hi", "--url", server.url])
    assert code == 0
    out = (tmp_path / "voice-1.mic.stt_ab.md").read_text()
    assert "## v2.5 en-IN" in out and "## v2.5 hi-IN" in out and "codemix" not in out
    assert KEY not in capsys.readouterr().out


def test_dry_run_and_resampling(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "a.wav"
    write_wav(rec, 0.5, rate=48000)
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    assert stt_ab.main([str(rec), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "codemix" in out and "prisma-v3.0" in out and "0.5 s of audio x 3 arms" in out
    assert len(stt_ab.load_pcm(rec)) == 16000  # 0.5 s at 16 kHz, 2 bytes each
    assert stt_ab.main(["--trace-dir", str(tmp_path / "none")]) == 1


async def test_a_refused_key_is_reported_per_arm_and_never_printed(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "voice-2.mic.wav"
    write_wav(rec, 0.2)
    monkeypatch.setenv("GNANI_API_KEY", KEY)
    async with FakePrisma(reject_http_status=401) as server:
        code = await stt_ab.amain([str(rec), "--url", server.url])
    assert code == 0 and server.http_rejections == 3  # every arm tried, none crashed the run
    out = capsys.readouterr().out
    assert out.count("errors: InvalidStatus") == 3 and KEY not in out
