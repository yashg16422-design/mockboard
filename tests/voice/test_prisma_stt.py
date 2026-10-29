"""Replay contract tests for our Prisma STT service (ruling 24, voice chunk V2).

A fake Prisma server (tests/voice/fake_gnani.py) replays the documented message shapes and
records what our client sends. Nothing touches the network or spends credits. Skipped
without the `voice` extra, so `make test` stays green on a plain checkout.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import statistics

import pytest

pytest.importorskip("pipecat")


from loguru import logger
from pipecat.frames.frames import (
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from tests.voice.fake_gnani import (
    FakePrisma,
    prisma_error,
    prisma_processing,
    prisma_transcript,
)
from tests.voice.harness import Harness

from mockboard.config import Settings
from mockboard.speech.gnani_protocol import STTConfig
from mockboard.speech.prisma_stt import PrismaProcessingFrame, PrismaSTTService

KEY = "test-key-SECRET-9f2c"


def make_stt(url: str, **kwargs) -> PrismaSTTService:
    config = kwargs.pop(
        "config",
        STTConfig(lang_code="en-IN", sample_rate=16000, fmt="verbatim", max_speech_s=15.0,
                  min_silence_ms=300, vad_threshold=0.6),
    )
    kwargs.setdefault("reconnect_backoff_s", (0.05, 0.1, 0.2))
    kwargs.setdefault("ttfs_p99_latency", 0.5)
    return PrismaSTTService(api_key=KEY, config=config, url=url, **kwargs)


# --------------------------------------------------------------------------- headers


async def test_connect_headers_come_from_config_and_bias_goes_in_update_settings():
    async with FakePrisma() as server:
        stt = make_stt(server.url, bias_list=["UPSC", "Naxalism"], bias_score=1.5)
        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            await h.wait_until(lambda: stt.last_event("settings_updated") is not None)

    hd = conn.headers
    assert hd["x-api-key-id"] == KEY
    assert hd["lang_code"] == "en-IN"
    assert hd["x-sample-rate"] == "16000"
    assert hd["x-format"] == "verbatim"
    assert hd["x-max-speech-s"] == "15"
    assert hd["x-min-silence-ms"] == "300"
    assert hd["x-vad-threshold"] == "0.6"
    assert hd["x-api-request-id"].startswith("mb_")
    # Bias travels as the documented update_settings frame, never as a header.
    assert not any("bias" in name for name in hd)
    assert conn.texts == [
        {"type": "update_settings", "bias": {"list": ["UPSC", "Naxalism"], "score": 1.5}}
    ]
    assert stt.last_event("settings_updated").raw["config"]["bias_word_count"] == 2


def test_from_settings_reads_the_app_settings_and_overrides():
    s = Settings(gnani_api_key="k-123", stt_lang_code="hi-IN", stt_max_speech_s=8.0,
                 gnani_stt_url="ws://example.invalid/stt")
    stt = PrismaSTTService.from_settings(s, min_silence_ms=400)
    assert stt.config.lang_code == "hi-IN"
    assert stt.config.max_speech_s == 8.0
    assert stt.config.min_silence_ms == 400
    assert stt._url == "ws://example.invalid/stt"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config": STTConfig(min_silence_ms=50)},  # below the documented 100 ms
        {"config": STTConfig(fmt="loud")},
        {"config": STTConfig(sample_rate=22050)},
        {"bias_list": ["two words"]},
        {"bias_list": ["UPSC"], "bias_score": 3.0},
        {"rotate_after_s": 900, "rotate_hard_s": 950},  # past Prisma's 15-minute cap
    ],
)
def test_invalid_values_fail_at_construction(kwargs):
    with pytest.raises(ValueError):
        PrismaSTTService(api_key=KEY, url="ws://x", **kwargs)


# --------------------------------------------------------------------------- framing


async def test_audio_is_reframed_to_exact_1024_bytes_at_real_time_cadence():
    async with FakePrisma() as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            await h.feed_audio(2.0)  # 100 frames of 640 bytes = 64,000 bytes = 62.5 frames
            await h.wait_until(lambda: len(conn.frames) >= 62, timeout=3)

    sizes = [size for _, size in conn.frames]
    assert sizes and all(size == 1024 for size in sizes)
    assert len(sizes) == 62  # the half frame stays buffered; nothing is padded mid-stream
    times = [t for t, _ in conn.frames]
    gaps_ms = [(b - a) * 1000 for a, b in itertools.pairwise(times)]
    # Docs: one 1,024-byte frame every 32 ms at 16 kHz, no bursts. Pipecat's 20 ms input
    # would give 20/40 ms gaps if forwarded as it arrives; the pacer evens them out.
    assert 30 <= statistics.mean(gaps_ms) <= 34
    steady = [g for g in gaps_ms if 26 <= g <= 38]
    assert len(steady) >= 0.85 * len(gaps_ms), gaps_ms
    # A frame that the event loop sent late is followed by one sent on its own deadline,
    # so a short gap can follow a long one; a run of short gaps would be a burst.
    assert sum(g < 20 for g in gaps_ms) <= 3, gaps_ms


# --------------------------------------------------------------------------- messages


async def test_processing_and_transcripts_become_frames_in_order():
    async with FakePrisma(segment_frames=20, transcript_delay_s=0.15) as server:
        stt = make_stt(server.url)
        seen_processing = []

        @stt.event_handler("on_processing")
        async def _on_processing(_stt, event):
            seen_processing.append(event)

        async with Harness(stt) as h:
            await server.wait_for_conns(1)
            feed = asyncio.create_task(h.feed_audio(1.5))
            # Between `processing` and its `transcript`, one segment is in flight.
            assert await h.wait_until(lambda: stt.pending_segments == 1, timeout=3)
            await feed
            await h.wait_until(lambda: len(h.collector.of_type(TranscriptionFrame)) >= 2)
            assert stt.pending_segments == 0

    kinds = [
        type(f).__name__
        for f in h.collector.of_type(PrismaProcessingFrame, TranscriptionFrame)
    ]
    assert kinds[:4] == [
        "PrismaProcessingFrame", "TranscriptionFrame", "PrismaProcessingFrame",
        "TranscriptionFrame",
    ]
    first = h.collector.of_type(TranscriptionFrame)[0]
    assert first.text == "segment 1"
    assert first.result["segment_index"] == 1 and first.result["is_final"] is True
    assert str(first.language) in ("Language.EN_IN", "en-IN")
    events = [e for e in stt.events if e.kind in ("processing", "transcript")]
    assert [e.kind for e in events][:2] == ["processing", "transcript"]
    assert events[1].arrived - events[0].arrived >= 0.1  # stamped on arrival
    assert events[1].segment_index == 1
    assert len(seen_processing) >= 2


async def test_an_error_message_is_reported_and_the_session_continues():
    async with FakePrisma() as server:
        stt = make_stt(server.url)
        prisma_errors = []

        @stt.event_handler("on_prisma_error")
        async def _on_prisma_error(_stt, message):
            prisma_errors.append(message)

        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            await server.send(conn, prisma_error("Transcription failed for segment"))
            await server.send(conn, prisma_processing())
            await server.send(conn, prisma_transcript("phir bhi chal raha hai", 2))
            await h.wait_until(lambda: h.collector.of_type(TranscriptionFrame))
            await h.feed_audio(0.3)
            await h.wait_until(lambda: len(conn.frames) >= 8)

    assert prisma_errors == ["Transcription failed for segment"]
    assert any("Transcription failed" in e for e in h.errors)
    assert [f.text for f in h.collector.of_type(TranscriptionFrame)] == ["phir bhi chal raha hai"]
    assert len(server.conns) == 1  # no reconnect: the docs say the session stays open
    assert len(conn.frames) >= 8 and stt.stats["errors"] == 1


# --------------------------------------------------------------------------- reconnect


async def test_server_close_at_the_session_cap_reconnects_and_audio_moves_on():
    async with FakePrisma() as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            first = await server.wait_for_conns(1)
            feed = asyncio.create_task(h.feed_audio(1.2))
            await asyncio.sleep(0.3)
            await server.send(first, prisma_error("Session exceeded 15 minutes"))
            await server.close(first, code=1001, reason="session limit")
            second = await server.wait_for_conns(2)
            await feed
            await h.wait_until(lambda: len(second.frames) >= 10)

    assert stt.stats["reconnects"] == 1
    assert len(first.frames) >= 5 and len(second.frames) >= 10
    assert all(size == 1024 for _, size in first.frames + second.frames)
    assert second.headers["x-api-request-id"] != first.headers["x-api-request-id"]
    closed = [e for e in stt.events if e.kind == "closed"]
    assert closed and closed[0].raw["code"] == 1001


async def test_a_1008_refusal_is_not_retried():
    async with FakePrisma(refuse_with_1008=True) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            await server.wait_for_conns(1)
            await asyncio.sleep(0.6)  # longer than the whole test backoff
    assert len(server.conns) == 1
    assert any("1008" in e for e in h.errors)


async def test_rotation_before_the_cap_waits_for_a_pause_and_keeps_the_last_transcript():
    async with FakePrisma() as server:
        stt = make_stt(server.url, rotate_after_s=0.3, rotate_hard_s=5.0,
                       rotation_check_s=0.05, rotation_grace_s=0.5)
        async with Harness(stt) as h:
            first = await server.wait_for_conns(1)
            await h.queue(VADUserStartedSpeakingFrame())
            await h.feed_audio(0.6)
            assert len(server.conns) == 1  # due, but the user is speaking
            await h.queue(VADUserStoppedSpeakingFrame(stop_secs=0.2))
            second = await server.wait_for_conns(2, timeout=2)
            # The old socket's last segment still arrives after the switch.
            await server.send(first, prisma_processing())
            await server.send(first, prisma_transcript("aakhri baat", 3))
            await h.feed_audio(0.4)
            await asyncio.sleep(0.7)

    assert stt.stats["rotations"] >= 1
    assert {"type": "commit"} in first.texts
    assert first.closed.is_set()  # retired after the grace period
    assert len(second.frames) >= 8
    assert "aakhri baat" in [f.text for f in h.collector.of_type(TranscriptionFrame)]


async def test_the_api_key_never_reaches_the_logs():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="TRACE")
    try:
        async with FakePrisma() as server:
            stt = make_stt(server.url)
            async with Harness(stt) as h:
                first = await server.wait_for_conns(1)
                await server.send(first, prisma_error("bad things"))
                await server.close(first, code=1011)
                await server.wait_for_conns(2)
                await h.feed_audio(0.2)
    finally:
        logger.remove(sink)
    text = "\n".join(lines)
    assert "[prisma]" in text and "headers=" in text  # we did log the connect headers
    assert KEY not in text
    assert all(KEY not in json.dumps(e.raw) for e in stt.events)
