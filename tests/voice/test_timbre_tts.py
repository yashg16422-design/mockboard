"""Replay contract tests for Timbre TTS (ruling 25, voice chunk V2).

A fake Timbre server (tests/voice/fake_gnani.py) replays the documented start / audio /
complete / error messages. The plugin always connects to its own constant URL, so the tests
point that constant at the fake server. Skipped without the `voice` extra.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("pipecat")
pytest.importorskip("pipecat_gnani")

from pipecat.frames.frames import (
    ErrorFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.services.settings import TTSSettings
from pipecat_gnani import GnaniTTSService
from pipecat_gnani import tts as plugin_tts
from tests.voice.fake_gnani import FakeTimbre
from tests.voice.harness import Harness

from mockboard.config import Settings
from mockboard.speech.timbre_tts import TimbreTTSService

KEY = "tts-key-SECRET-77"
LINE = "Achha, toh aap Kanpur se hain. Wahan ki sabse badi samasya kya hai?"


@pytest.fixture
async def timbre(monkeypatch):
    async with FakeTimbre(chunks=20, chunk_bytes=1600, chunk_gap_s=0.05) as server:
        monkeypatch.setattr(plugin_tts, "GNANI_TTS_WS_URL", server.url)
        yield server


def make_tts(**kwargs) -> TimbreTTSService:
    kwargs.setdefault("language", "auto")
    return TimbreTTSService(api_key=KEY, voice="Poorvi", **kwargs)


async def test_request_asks_for_raw_16_bit_pcm_at_the_pipeline_rate(timbre):
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 20, timeout=5)

    req = timbre.conns[0].requests[0]
    assert req["text"] == LINE
    assert req["voice"] == "Poorvi" and req["model"] == "timbre-v2.5"
    assert req["language"] == "auto"
    assert req["audio_config"] == {
        "sample_rate": 16000,  # negotiated from the pipeline's StartFrame
        "encoding": "linear_pcm",
        "num_channels": 1,
        "sample_width": 2,
        "container": "raw",
    }
    assert timbre.conns[0].headers["x-api-key-id"] == KEY
    audio = h.collector.of_type(TTSAudioRawFrame)
    assert len(audio) == 20
    assert all(f.audio == b"\x01\x00" * 800 and f.sample_rate == 16000 for f in audio)


async def test_an_interruption_stops_the_line_and_the_next_line_plays(timbre):
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 4)
        interrupted_at = time.monotonic()
        await h.queue(InterruptionFrame())
        await asyncio.sleep(0.6)  # the interrupted line would still be streaming
        before_next = len(h.collector.frames)
        await h.queue(TTSSpeakFrame("Theek hai, aage badhte hain.", append_to_context=False))
        await h.wait_until(
            lambda: len(
                [f for _, f in h.collector.frames[before_next:] if isinstance(f, TTSAudioRawFrame)]
            ) >= 20,
            timeout=5,
        )

    frames = h.collector.frames
    cut = next(i for i, (_, f) in enumerate(frames) if isinstance(f, InterruptionFrame))
    gap = [f for _, f in frames[cut:before_next] if isinstance(f, TTSAudioRawFrame)]
    assert gap == []  # no audio of the interrupted line after the interruption
    first, second = timbre.conns[0], timbre.conns[1]
    assert first.closed_at is not None and first.closed_at - interrupted_at < 0.3
    assert first.chunks_sent < 20  # the server stopped streaming because we closed
    assert [r["text"] for r in second.requests] == ["Theek hai, aage badhte hain."]
    assert tts.interruptions == 1


async def test_stock_plugin_keeps_playing_after_an_interruption(timbre):
    """Documents the bug the fix exists for (pipecat-gnani 0.5.12 on pipecat-ai 1.5.0). If
    this starts failing after an upgrade, the plugin was fixed and our override can go."""
    stock = GnaniTTSService(
        api_key=KEY, model="timbre-v2.5",
        settings=GnaniTTSService.Settings(voice="Poorvi", language="auto"),
    )
    async with Harness(stock) as h:
        await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 4)
        await h.queue(InterruptionFrame())
        await asyncio.sleep(0.6)

    frames = h.collector.frames
    cut = next(i for i, (_, f) in enumerate(frames) if isinstance(f, InterruptionFrame))
    assert any(isinstance(f, TTSAudioRawFrame) for _, f in frames[cut:])
    assert len(timbre.conns) == 1  # never closed or reopened


async def test_audio_reaching_push_frame_after_an_interruption_is_dropped():
    tts = make_tts()
    pcm = TTSAudioRawFrame(audio=b"\x00\x00" * 10, sample_rate=16000, num_channels=1)
    tts._audio_gate_open = False  # state right after _handle_interruption
    await tts.push_frame(pcm)
    assert tts.audio_dropped_after_interruption == 1


async def test_a_timbre_error_is_reported_and_the_socket_stays_open(timbre):
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(TTSSpeakFrame("FAIL this line", append_to_context=False))
        await h.wait_until(lambda: any("Gnani TTS" in e for e in h.errors))
        await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 20)

    assert any("Invalid text or audio configuration" in e for e in h.errors)
    assert len(timbre.conns) == 1 and len(timbre.conns[0].requests) == 2
    assert h.collector.of_type(TTSStartedFrame)
    assert not [f for f in h.collector.of_type(ErrorFrame) if f.fatal]


def test_hinglish_hi_en_is_refused_before_anything_is_sent():
    with pytest.raises(ValueError, match="gnani-vachana"):
        make_tts(language="hi-en")


def test_voices_outside_the_catalog_and_bad_rates_are_refused():
    with pytest.raises(ValueError):
        TimbreTTSService(api_key=KEY, voice="Pranav-typo", language="auto")
    with pytest.raises(ValueError):
        make_tts(sample_rate=11025)


def test_from_settings_uses_tts_model_and_language():
    s = Settings(gnani_api_key="k-1", tts_model="timbre-v2.5", tts_language="hi-IN")
    tts = TimbreTTSService.from_settings(s, voice="Vikrant")
    assert tts._settings.language == "hi-IN"
    assert tts._settings.model == "timbre-v2.5"
    assert tts._settings.container == "raw" and tts._settings.encoding == "linear_pcm"


def test_the_default_tts_language_is_accepted_by_the_sdk_and_the_docs():
    """Ruling 25: TTS_LANGUAGE's default must pass gnani-vachana's own check (it refuses
    "hi-en") and be in Gnani's documented list."""
    from gnani.tts.client import SUPPORTED_TTS_LANGUAGES

    from mockboard.speech.gnani_protocol import TTS_LANGUAGES

    default = Settings.model_fields["tts_language"].default
    assert default == "auto"
    assert default in SUPPORTED_TTS_LANGUAGES and default in TTS_LANGUAGES


async def test_a_socket_timbre_closed_while_idle_is_replaced_before_the_next_line(monkeypatch):
    async with FakeTimbre(chunks=3, chunk_gap_s=0.01, idle_close_s=0.2) as server:
        monkeypatch.setattr(plugin_tts, "GNANI_TTS_WS_URL", server.url)
        tts = make_tts()
        async with Harness(tts) as h:
            await h.queue(TTSSpeakFrame("Pehli line.", append_to_context=False))
            await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 3)
            await asyncio.sleep(0.5)  # the server closes the idle socket
            await h.queue(TTSSpeakFrame("Doosri line.", append_to_context=False))
            await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 6)

    assert len(h.collector.of_type(TTSAudioRawFrame)) == 6
    assert [r["text"] for c in server.conns for r in c.requests] == ["Pehli line.", "Doosri line."]
    assert tts.idle_reconnects == 1
    assert not h.errors


# --------------------------------------------------------------------------- ruling 30


async def test_requests_are_serialised_one_at_a_time(timbre):
    from mockboard.voice.lines import PlaybackLedger

    ledger = PlaybackLedger()
    tts = make_tts(ledger=ledger)
    async with Harness(tts) as h:
        await h.queue(TTSSpeakFrame("Pehla vaakya.", append_to_context=False))
        await h.queue(TTSSpeakFrame("Doosra vaakya?", append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 40, timeout=6)

    conn = timbre.conns[0]
    assert [r["text"] for r in conn.requests] == ["Pehla vaakya.", "Doosra vaakya?"]
    # Timbre's docs: "Send one synthesis request at a time" (CTO_NOTES_05 M4).
    assert conn.request_times[1] >= conn.finished_times[0]
    assert tts.serialise_waits == 1
    assert [u.text for u in ledger.units] == ["Pehla vaakya.", "Doosra vaakya?"]
    assert all(u.done_at and not u.failed for u in ledger.units)
    assert [round(u.audio_ms) for u in ledger.units] == [1000, 1000]  # 20 x 1,600 bytes


async def test_a_barge_in_between_two_sentences_leaves_no_stale_audio(timbre):
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(TTSSpeakFrame("Pehla vaakya.", append_to_context=False))
        await h.queue(TTSSpeakFrame("Doosra vaakya?", append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 25, timeout=4)
        await h.queue(InterruptionFrame())
        await asyncio.sleep(0.5)
        mark = len(h.collector.frames)
        await h.queue(TTSSpeakFrame("Naya sawaal?", append_to_context=False))
        await h.wait_until(
            lambda: len([f for _, f in h.collector.frames[mark:]
                         if isinstance(f, TTSAudioRawFrame)]) >= 20, timeout=5)

    frames = h.collector.frames
    cut = next(i for i, (_, f) in enumerate(frames) if isinstance(f, InterruptionFrame))
    assert not [f for _, f in frames[cut:mark] if isinstance(f, TTSAudioRawFrame)]
    texts = [r["text"] for c in timbre.conns for r in c.requests]
    assert texts == ["Pehla vaakya.", "Doosra vaakya?", "Naya sawaal?"]
    assert len([f for _, f in frames[mark:] if isinstance(f, TTSAudioRawFrame)]) == 20


async def test_first_byte_watchdog_reopens_and_resends_once(monkeypatch):
    async with FakeTimbre(chunks=5, chunk_gap_s=0.01, silent_requests=1) as server:
        monkeypatch.setattr(plugin_tts, "GNANI_TTS_WS_URL", server.url)
        tts = make_tts(first_byte_timeout_s=0.3)
        async with Harness(tts) as h:
            await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
            await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 5, 3)
    assert tts.first_byte_resends == 1 and tts.first_byte_failures == 0
    assert len(server.conns) == 2 and server.conns[1].requests[0]["text"] == LINE


async def test_first_byte_watchdog_gives_up_and_tells_the_app(monkeypatch):
    from mockboard.voice.lines import PlaybackLedger

    async with FakeTimbre(chunks=5, chunk_gap_s=0.01, silent_requests=2) as server:
        monkeypatch.setattr(plugin_tts, "GNANI_TTS_WS_URL", server.url)
        ledger = PlaybackLedger()
        tts = make_tts(first_byte_timeout_s=0.2, ledger=ledger)
        timeouts = []
        tts.add_event_handler("on_first_byte_timeout", lambda _t, text: timeouts.append(text))
        async with Harness(tts) as h:
            await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
            await h.wait_until(lambda: timeouts, timeout=3)
            await h.queue(TTSSpeakFrame("Agla sawaal?", append_to_context=False))
            await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 5, 3)
    assert timeouts == [LINE] and tts.first_byte_failures == 1
    assert ledger.units[0].failed and not ledger.units[1].failed


async def test_timbre_v3_is_sent_past_the_old_sdk_with_its_language(timbre):
    # Ruling 38: tts-websocket.md lists timbre-v3.0 with `language`; gnani-vachana 0.7.9
    # refuses the model, so the request body is ours.
    tts = TimbreTTSService(api_key=KEY, voice="Yashraj", model="timbre-v3.0", language="hi-IN",
                           sample_rate=24000)
    async with Harness(tts) as h:
        await h.queue(TTSSpeakFrame(LINE, append_to_context=False))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 20, timeout=5)
    req = timbre.conns[0].requests[0]
    assert (req["model"], req["voice"], req["language"]) == ("timbre-v3.0", "Yashraj", "hi-IN")
    assert req["audio_config"]["sample_rate"] == 24000
    assert req["audio_config"]["container"] == "raw"
    assert all(f.sample_rate == 24000 for f in h.collector.of_type(TTSAudioRawFrame))


def test_voice_and_model_must_match_the_published_catalog():
    with pytest.raises(ValueError, match=r"not a Timbre v3\.0 voice"):
        TimbreTTSService(api_key=KEY, voice="Poorvi", model="timbre-v3.0")
    with pytest.raises(ValueError, match=r"not a Timbre v2\.5 voice"):
        TimbreTTSService(api_key=KEY, voice="Yashraj", model="timbre-v2.5")
    with pytest.raises(ValueError, match="TTS_MODEL must be one of"):
        TimbreTTSService(api_key=KEY, voice="Poorvi", model="timbre-v2.0")
    s = Settings(_env_file=None, gnani_api_key=KEY, tts_model="timbre-v3.0", tts_voice="Avinash")
    tts = TimbreTTSService.from_settings(s, voice=s.tts_voice)
    assert (tts.model, tts.voice, tts.language) == ("timbre-v3.0", "Avinash", "auto")


# ------------------------------------------------------------------ P8: a voice per request


def voice_frame(voice: str) -> TTSUpdateSettingsFrame:
    return TTSUpdateSettingsFrame(delta=TTSSettings(voice=voice))


def say(text: str) -> TTSSpeakFrame:
    return TTSSpeakFrame(text, append_to_context=False)


async def test_a_settings_frame_changes_the_voice_the_next_request_sends(timbre):
    """`TTSUpdateSettingsFrame` alone would not: the body is ours and reads `self.voice`."""
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(voice_frame("Vikrant"))
        await h.queue(say("Pehla vaakya."))
        await h.queue(voice_frame("Urmila"))
        await h.queue(say("Doosra vaakya?"))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 40, timeout=6)

    assert [(r["voice"], r["text"]) for r in timbre.conns[0].requests] == [
        ("Vikrant", "Pehla vaakya."), ("Urmila", "Doosra vaakya?"),
    ]
    assert tts.voice_changes == ["Vikrant", "Urmila"] and not tts.voice_refused
    assert len(timbre.conns) == 1  # no reconnect for a new voice
    assert tts._settings.voice == "Urmila" and tts.voice == "Urmila"
    assert not h.errors


async def test_no_settings_frame_means_the_voice_the_service_was_built_with(timbre):
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(say(LINE))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 20, timeout=5)
    assert timbre.conns[0].requests[0]["voice"] == "Poorvi" and tts.voice_changes == []


async def test_a_voice_the_model_does_not_have_is_refused_and_the_old_voice_stays(timbre):
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(voice_frame("Yashraj"))  # a v3.0 voice, on a v2.5 service
        await h.queue(say("Pehla vaakya."))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 20, timeout=5)
    assert timbre.conns[0].requests[0]["voice"] == "Poorvi"
    assert tts.voice_refused == ["Yashraj"] and tts.voice_changes == []
    assert not h.errors  # refused quietly: a bad config must not stop the interview


async def test_a_timbre_v3_voice_is_accepted_past_the_plugins_own_check(timbre):
    """The plugin refuses every v3.0 voice name, so a switch cannot go through its validation."""
    tts = TimbreTTSService(api_key=KEY, voice="Avinash", model="timbre-v3.0", language="auto")
    async with Harness(tts) as h:
        await h.queue(voice_frame("Vasudha"))
        await h.queue(say("Pehla vaakya."))
        await h.queue(voice_frame("Poorvi"))  # a v2.5 voice on a v3.0 service: refused
        await h.queue(say("Doosra vaakya?"))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 40, timeout=6)
    assert [(r["model"], r["voice"]) for r in timbre.conns[0].requests] == [
        ("timbre-v3.0", "Vasudha"), ("timbre-v3.0", "Vasudha"),
    ]
    assert tts.voice_changes == ["Vasudha"] and tts.voice_refused == ["Poorvi"]
    assert not h.errors


async def test_a_frame_that_survives_an_interruption_changes_the_voice_but_the_line_is_dropped(
    timbre
):
    """The settings frame is uninterruptible and the line after it is not: the voice sticks. That
    is why the voice loop sends one before every line (tests/voice/test_panel_voice.py)."""
    tts = make_tts()
    async with Harness(tts) as h:
        await h.queue(voice_frame("Vikrant"))
        await h.queue(say(LINE))
        await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 4)
        await h.queue(InterruptionFrame())
        await asyncio.sleep(0.4)
        mark = len(h.collector.frames)
        await h.queue(voice_frame("Urmila"))
        await h.queue(say("Naya sawaal?"))
        await h.wait_until(
            lambda: len([f for _, f in h.collector.frames[mark:]
                         if isinstance(f, TTSAudioRawFrame)]) >= 20, timeout=5)
    sent = [(r["voice"], r["text"]) for c in timbre.conns for r in c.requests]
    assert sent == [("Vikrant", LINE), ("Urmila", "Naya sawaal?")]


async def test_a_resend_by_the_watchdog_keeps_the_voice_of_the_first_send(monkeypatch):
    async with FakeTimbre(chunks=5, chunk_gap_s=0.01, silent_requests=1) as server:
        monkeypatch.setattr(plugin_tts, "GNANI_TTS_WS_URL", server.url)
        tts = make_tts(first_byte_timeout_s=0.3)
        async with Harness(tts) as h:
            await h.queue(voice_frame("Vikrant"))
            await h.queue(say(LINE))
            await asyncio.sleep(0.1)
            await h.queue(voice_frame("Urmila"))  # arrives while the first line waits for audio
            await h.wait_until(lambda: len(h.collector.of_type(TTSAudioRawFrame)) >= 5, 3)
    assert tts.first_byte_resends == 1
    assert [(r["voice"], r["text"]) for c in server.conns for r in c.requests] == [
        ("Vikrant", LINE), ("Vikrant", LINE),
    ]
