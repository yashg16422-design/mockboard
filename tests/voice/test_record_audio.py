"""Ruling 35: opt-in recording of the candidate's microphone (Pipecat needed)."""

from __future__ import annotations

import wave

import pytest

pytest.importorskip("pipecat")

from tests.voice.harness import Harness

from mockboard.voice.recorder import MicRecorder


async def test_the_recorder_saves_exactly_the_input_audio(tmp_path):
    rec = MicRecorder(tmp_path / "traces" / "s.mic.wav")
    async with Harness(rec) as h:
        await h.feed_audio(1.0, real_time=False)
        await h.wait_until(lambda: rec.frames_written >= 16000)
    with wave.open(str(rec.path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 16000
    assert len(h.collector.frames) >= 50  # every frame passed on


def test_recording_is_off_unless_asked(settings):
    from mockboard.voice import server

    assert server.parse_args(["--mode", "upsc"]).record_audio is False
    cfg = server.parse_args(["--mode", "upsc", "--record-audio"])
    assert cfg.record_audio and cfg.public()["record_audio"] is True
