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
    prisma_speech_start,
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
    kwargs.setdefault("request_id_prefix", "sess-test")
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
    # Ruling 27: Gnani's current header names, and none of the old spellings.
    assert hd["language_code"] == "en-IN"
    assert hd["x-sample-rate"] == "16000"
    assert hd["format"] == "verbatim"
    assert hd["model"] == "prisma-v2.5"
    assert hd["x-max-speech-s"] == "15"
    assert hd["x-min-silence-ms"] == "300"
    assert hd["x-vad-threshold"] == "0.6"
    assert not {"lang_code", "x-format", "x-model"} & set(hd)
    # Ruling 31: <session id>-c<k>, one per connection.
    assert hd["x-api-request-id"] == "sess-test-c1"
    # Bias travels as the documented update_settings frame, never as a header.
    assert not any("bias" in name for name in hd)
    assert "keyterms" not in hd and "topic" not in hd  # v3.0 only
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
    assert stt.config.idle_timeout_s == 300  # ruling 30: explicit, not Prisma's 60 s
    assert stt.config.model == "prisma-v2.5" and stt.config.vad_threshold == 0.7
    assert stt._url == "ws://example.invalid/stt"
    assert stt._commit_wait_s == 1.5 and stt._commit_noop_s == 0.3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config": STTConfig(min_silence_ms=50)},  # below the documented 100 ms
        {"config": STTConfig(fmt="loud")},
        {"config": STTConfig(sample_rate=22050)},
        {"bias_list": ["two words"], "bias_score": 1.0},
        {"bias_list": ["UPSC"], "bias_score": 3.0},
        {"bias_list": ["UPSC"]},  # v2.5: a list needs a non-zero score (docs)
        {"bias_list": ["UPSC"], "bias_score": 0.0},
        {"config": STTConfig(model="prisma-v3.0"), "bias_list": ["UPSC"], "bias_score": 1.0},
        {"config": STTConfig(model="prisma-v4")},  # the server would silently ignore it
        {"config": STTConfig(fmt="codemix")},  # v3.0 only
        {"config": STTConfig(idle_timeout_s=1000)},
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
    assert first.headers["x-api-request-id"] == "sess-test-c1"
    assert second.headers["x-api-request-id"] == "sess-test-c2"  # never repeats (ruling 31)
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


async def test_a_rejected_key_is_reported_once_and_not_retried():
    async with FakePrisma(reject_http_status=401) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            await h.wait_until(lambda: server.http_rejections >= 1)
            await asyncio.sleep(0.5)  # longer than the whole test backoff
    assert server.http_rejections == 1
    assert any("HTTP 401" in e for e in h.errors)
    assert all(KEY not in e for e in h.errors)


async def test_audio_held_while_disconnected_is_bounded_then_caught_up():
    async with FakePrisma() as server:
        # 0.3 s of backlog at most; the reconnect waits 0.6 s, so some audio is dropped.
        stt = make_stt(server.url, reconnect_backoff_s=(0.6,), max_backlog_s=0.3)
        async with Harness(stt) as h:
            first = await server.wait_for_conns(1)
            feed = asyncio.create_task(h.feed_audio(1.6))
            await asyncio.sleep(0.2)
            await server.close(first, code=1011, reason="internal error")
            second = await server.wait_for_conns(2, timeout=3)
            await feed
            await asyncio.sleep(0.3)

    assert stt.stats["frames_dropped"] > 0
    assert stt.stats["max_backlog_frames"] <= int(0.3 / 0.032)
    times = [t for t, _ in second.frames]
    gaps_ms = [(b - a) * 1000 for a, b in itertools.pairwise(times)]
    # The held frames go out faster than real time (catch-up), then the cadence returns.
    assert min(gaps_ms[:4]) < 24, gaps_ms[:10]
    tail = gaps_ms[-10:]
    assert 28 <= statistics.mean(tail) <= 36, tail
    assert all(size == 1024 for _, size in second.frames)


# --------------------------------------------------------------------------- ruling 27 v3.0


async def test_v3_headers_send_keyterms_topic_and_the_served_model_is_checked():
    cfg = STTConfig(lang_code="hi-IN,en-IN", fmt="codemix", model="prisma-v3.0",
                    keyterms=["UPSC", "Naxalism", "left wing extremism", "upsc"],
                    topic="A UPSC   mock interview\non internal security", idle_timeout_s=300)
    async with FakePrisma() as server:  # its `connected` has no v3.0 fields
        stt = make_stt(server.url, config=cfg)
        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            assert await stt.set_keyterms(["tourism", "homestay"])
            await h.wait_until(lambda: len(conn.texts) >= 1)
    hd = conn.headers
    assert hd["model"] == "prisma-v3.0" and hd["format"] == "codemix"
    assert hd["language_code"] == "hi-IN,en-IN"
    assert json.loads(hd["keyterms"]) == ["UPSC", "Naxalism", "left wing extremism"]
    assert hd["topic"] == "A UPSC mock interview on internal security"
    assert hd["x-idle-timeout-sec"] == "300"
    assert conn.texts[0] == {"type": "update_settings", "bias": {"list": ["tourism", "homestay"]}}
    assert stt.served_model.startswith("unconfirmed")  # loud: v3.0 not confirmed

    extra = {"request_id": "r1", "config": {"sample_rate": 16000, "chunk_size": 512,
                                             "language_code": "hi-IN", "format": "codemix"}}
    async with FakePrisma(connected_extra=extra) as server:
        stt = make_stt(server.url, config=STTConfig(model="prisma-v3.0"))
        async with Harness(stt):
            await server.wait_for_conns(1)
    assert stt.served_model == "prisma-v3.0"


async def test_set_keyterms_is_a_no_op_on_v25():
    async with FakePrisma() as server:
        stt = make_stt(server.url)
        async with Harness(stt):
            conn = await server.wait_for_conns(1)
            assert not await stt.set_keyterms(["UPSC"])
    assert conn.texts == [] and stt.served_model == "prisma-v2.5"


# --------------------------------------------------------------------------- commit


async def test_commit_closes_the_open_utterance_and_tags_its_transcript():
    async with FakePrisma(transcript_delay_s=0.1) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            await h.feed_audio(0.5)
            assert await h.wait_until(lambda: stt.utterance_open is True)
            result = await stt.commit()
            await h.wait_until(lambda: h.collector.of_type(TranscriptionFrame))
    assert result.reply == "transcript" and result.texts == ["committed 1"]
    assert result.open_at_commit is True and not result.timed_out
    assert 90 <= result.wait_ms < 1000
    frame = h.collector.of_type(TranscriptionFrame)[0]
    assert frame.result["mb"]["commit_id"] == result.commit_id
    assert frame.result["mb"]["seq"] == result.last_seq == 1
    assert conn.texts == [{"type": "commit"}]


async def test_commit_waits_for_segments_already_in_flight():
    async with FakePrisma(transcript_delay_s=0.25) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            await server.send(conn, prisma_speech_start(1))
            await server.send(conn, prisma_processing())  # segment 1 in flight
            await h.wait_until(lambda: stt.pending_segments == 1)
            await h.feed_audio(0.3)  # utterance 2 open
            task = asyncio.create_task(stt.commit())
            await asyncio.sleep(0.05)
            await server.send(conn, prisma_transcript("pehli baat", 1))
            result = await task
    assert result.pending_at_commit == 1
    assert result.texts == ["pehli baat", "committed 1"] and result.reply == "transcript"
    tags = [f.result["mb"]["commit_id"] for f in h.collector.of_type(TranscriptionFrame)]
    assert tags == [result.commit_id, result.commit_id]


async def test_commit_is_sent_behind_audio_queued_while_disconnected():
    async with FakePrisma() as server:
        stt = make_stt(server.url, reconnect_backoff_s=(0.4,), max_backlog_s=2.0)
        async with Harness(stt) as h:
            first = await server.wait_for_conns(1)
            await server.close(first, code=1011, reason="internal error")
            await h.wait_until(lambda: stt.request_id is None)
            await h.feed_audio(0.5)  # ~15 frames queued while there is no socket
            result = await stt.commit(wait_s=3.0)
            second = await server.wait_for_conns(2)
    assert stt.stats["max_backlog_frames"] >= 10
    # Every frame queued before the commit went out first, the flushed tail included
    # (CTO_NOTES_05 M3), and nothing was sent after it.
    assert second.commit_after_frames == [len(second.frames)]
    assert len(second.frames) >= 15
    assert result.reply == "transcript" and result.texts == ["committed 1"]


@pytest.mark.parametrize(
    ("noop_reply", "expected"), [("none", "none"), ("empty", "empty"), ("error", "error")]
)
async def test_commit_with_nothing_open_ends_within_the_noop_bound(noop_reply, expected):
    async with FakePrisma(commit_noop_reply=noop_reply, transcript_delay_s=0.05) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            await server.wait_for_conns(1)
            result = await stt.commit(wait_s=2.0, noop_s=0.3)
            await asyncio.sleep(0.1)
    assert result.reply == expected
    assert result.wait_ms < 600 and result.texts == []
    assert not h.collector.of_type(TranscriptionFrame)  # an empty transcript is nothing


async def test_commit_without_speech_start_support_still_resolves():
    async with FakePrisma(send_speech_start=False, transcript_delay_s=0.05) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            await server.wait_for_conns(1)
            await h.feed_audio(0.3)
            assert stt.utterance_open is None  # nobody can tell
            result = await stt.commit(wait_s=2.0, noop_s=0.3)
    assert result.open_at_commit is None and result.reply == "transcript"


async def test_commit_times_out_and_a_late_transcript_still_names_it():
    async with FakePrisma(transcript_delay_s=0.6) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            await server.wait_for_conns(1)
            await h.feed_audio(0.3)
            result = await stt.commit(wait_s=0.2)
            await h.wait_until(lambda: h.collector.of_type(TranscriptionFrame), timeout=2)
    assert result.reply == "timeout" and result.timed_out
    late = h.collector.of_type(TranscriptionFrame)[0]
    assert late.result["mb"]["commit_id"] == result.commit_id  # attributable by cause
    assert stt.stats["commit_timeouts"] == 1


async def test_speech_after_a_commit_is_not_attributed_to_it():
    async with FakePrisma(transcript_delay_s=0.05) as server:
        stt = make_stt(server.url)
        async with Harness(stt) as h:
            conn = await server.wait_for_conns(1)
            await h.feed_audio(0.3)
            result = await stt.commit()
            await h.feed_audio(0.3)  # the candidate resumes
            await server.send(conn, prisma_processing())
            await server.send(conn, prisma_transcript("aur ek baat", 2))
            await h.wait_until(lambda: len(h.collector.of_type(TranscriptionFrame)) >= 2)
    tags = [f.result["mb"]["commit_id"] for f in h.collector.of_type(TranscriptionFrame)]
    assert tags == [result.commit_id, None]


# --------------------------------------------------------------------------- ruling 30


async def test_an_idle_close_is_a_normal_event_and_reconnects():
    async with FakePrisma() as server:
        stt = make_stt(server.url)
        gave_up = []
        stt.add_event_handler("on_gave_up", lambda _s, m: gave_up.append(m))
        async with Harness(stt):
            first = await server.wait_for_conns(1)
            await server.close(first, code=1000, reason="idle timeout")
            await server.wait_for_conns(2)
    assert stt.stats["idle_closes"] == 1 and stt.stats["reconnects"] == 1
    assert any(e.kind == "idle_close" for e in stt.events) and not gave_up


async def test_giving_up_fires_on_gave_up_for_the_app():
    async with FakePrisma(reject_http_status=401) as server:
        stt = make_stt(server.url)
        gave_up = []

        @stt.event_handler("on_gave_up")
        async def _gave_up(_stt, message):
            gave_up.append(message)

        async with Harness(stt) as h:
            await h.wait_until(lambda: gave_up)
    assert gave_up and "401" in gave_up[0] and stt.gave_up == gave_up[0]
    assert KEY not in gave_up[0]
