"""Timbre TTS for the voice loop: the stock Gnani plugin plus our interruption fix (ruling 25).

Ruling 24 keeps the stock `pipecat_gnani.GnaniTTSService` for TTS because it works; this
module is the one place that knows about it, so a plugin upgrade touches only this file.

Five things are ours:

1. **Barge-in really stops Timbre** (spikes/NOTES.md, surprise 1). On an interruption,
   Pipecat 1.5.0's `InterruptibleTTSService` calls `_disconnect()` then `_connect()`, which in
   1.5.0 only flip a flag; the plugin implements `_disconnect_websocket()` /
   `_connect_websocket()` instead, so the old socket kept streaming the interrupted line.
   Timbre has no cancel message (tts-websocket.md), so closing the socket is the only way to
   stop it. We make the two methods close and reopen the socket, and we also gate audio:
   from the interruption until the next line starts, any audio chunk that still reaches
   `push_frame` is dropped and counted (`audio_dropped_after_interruption`).
2. **Raw 16-bit PCM at the pipeline's sample rate.** The plugin asks for a `wav` container by
   default and strips the header itself; we ask for `container=raw`, `encoding=linear_pcm`,
   `sample_width=2` (tts-websocket.md: "Raw audio with no header (16-bit PCM with
   linear_pcm)"), at the rate negotiated from the pipeline's StartFrame unless one is given.
3. **One request at a time** (ruling 30, CTO_NOTES_05 M4). The plugin's `run_tts` sends the
   request and returns at once, so sentence 2 could go out while sentence 1 still streams,
   although Timbre's docs say "Send one synthesis request at a time". `run_tts` now waits
   (bounded by `request_timeout_s`) for the previous request's `complete` or `error` (or the
   interruption that closed it) before it sends.
4. **First-byte watchdog** (ruling 30, CTO_NOTES_05 should-fix 5). A half-open socket passes
   the open-state check and the line would never play. If no audio arrives within
   `first_byte_timeout_s`, the socket is reopened and the request resent once; if that also
   gives nothing, the request is marked failed and `on_first_byte_timeout(service, text)`
   fires, so the voice app can show the line as text.
5. **Playback accounting.** With `ledger` set (a `mockboard.voice.lines.PlaybackLedger`), each
   request, its audio bytes and its end are reported, so the voice loop knows which words
   the candidate heard after a barge-in (ADR-008 §3).

6. **Timbre v3.0** (ruling 38). Gnani's docs (tts-websocket.md, tts-sse.md and
   available-voices.md, read Oct 31, 2026) list `model: "timbre-v3.0"`, "Expressive
   text-to-speech": 13 voices, every one speaking all 10 languages, with `language` and inline
   audio tags. gnani-vachana 0.7.9 predates it: its `_validate_model` refuses `timbre-v3.0`,
   its `_validate_timbre_options` refuses a language for any model but v2.5, and the plugin's
   `_build_tts_payload` drops the language for v3.0. So this class builds the request body
   itself (the same fields and audio_config the plugin sends, plus `language` for both
   models), checks the voice against our copy of the published catalog
   (`gnani_protocol.check_voice`), and gives the plugin's constructor a v2.5 placeholder to
   validate before setting the real model and voice.
   # UNVERIFIED: timbre-v3.0 against the real service (fake server only).

The language is checked against gnani-vachana 0.7.9's own list before anything is sent,
because ruling 25 asks for a value both the SDK and Timbre accept (the SDK refuses `hi-en`,
which Gnani's docs list); see config.py TTS_LANGUAGE. Sending `hi-en` from the board needs an
orchestrator ruling; the voice lab (`mockboard.voice.lab`) can already send it.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator
from typing import Any, cast

from gnani.tts.client import SUPPORTED_TTS_LANGUAGES
from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat_gnani import GnaniTTSService
from pipecat_gnani import tts as _plugin_tts
from websockets.protocol import State

from mockboard.config import Settings
from mockboard.speech.gnani_protocol import TTS_MODELS, TTS_SAMPLE_RATES, check_voice

RAW_PCM = {"encoding": "linear_pcm", "container": "raw", "sample_width": 2}


class TimbreTTSService(GnaniTTSService):
    """`GnaniTTSService` that stops audio on interruption and asks for raw PCM.

    Construct it from Settings (the normal path)::

        tts = TimbreTTSService.from_settings(get_settings(), voice="Poorvi")

    or directly::

        tts = TimbreTTSService(api_key=key, voice="Poorvi", language="auto")
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice: str,
        model: str = "timbre-v2.5",
        language: str | None = None,
        sample_rate: int | None = None,
        first_byte_timeout_s: float = 3.0,
        request_timeout_s: float = 20.0,
        ledger: Any = None,
        **kwargs: Any,
    ):
        if not api_key:
            raise ValueError("TimbreTTSService needs a Gnani API key")
        if language is not None and language not in SUPPORTED_TTS_LANGUAGES:
            raise ValueError(
                f"TTS language {language!r} is refused by gnani-vachana 0.7.9 (accepted: "
                f"{sorted(SUPPORTED_TTS_LANGUAGES)}); see TTS_LANGUAGE in config.py (ruling 25)"
            )
        if sample_rate is not None and sample_rate not in TTS_SAMPLE_RATES:
            raise ValueError(f"Timbre sample rates are {TTS_SAMPLE_RATES}, not {sample_rate}")
        if model not in TTS_MODELS:
            raise ValueError(f"TTS_MODEL must be one of {TTS_MODELS} (Gnani docs), not {model!r}")
        check_voice(model, voice)  # the published catalog, before spending credits
        # gnani-vachana 0.7.9 refuses timbre-v3.0 outright (see the module docstring, item 6):
        # let the plugin validate a v2.5 placeholder, then set the real model and voice. The
        # request body is ours (`request_body`), so the SDK's checks never see v3.0 again.
        placeholder = model != "timbre-v2.5"
        super().__init__(
            api_key=api_key,
            model="timbre-v2.5" if placeholder else model,
            sample_rate=sample_rate,
            settings=GnaniTTSService.Settings(voice="Poorvi" if placeholder else voice,
                                              language=language, **RAW_PCM),
            **kwargs,
        )
        if placeholder:
            self._settings.model = model
            self._settings.voice = voice
            self._sync_model_name_to_metrics()
        self.model = model
        self.voice = voice
        self.language = language
        self._audio_gate_open = True
        self.interruptions = 0
        self.audio_dropped_after_interruption = 0
        self.idle_reconnects = 0
        self.ledger = ledger
        self._first_byte_timeout_s = first_byte_timeout_s
        self._request_timeout_s = request_timeout_s
        self._request_done = asyncio.Event()
        self._request_done.set()
        self._first_audio = asyncio.Event()
        self._watchdog: asyncio.Task | None = None
        self.serialise_waits = 0
        self.serialise_timeouts = 0
        self.first_byte_resends = 0
        self.first_byte_failures = 0
        self._register_event_handler("on_first_byte_timeout")

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        voice: str,
        api_key: str | None = None,
        sample_rate: int | None = None,
        **kwargs: Any,
    ) -> TimbreTTSService:
        """Build from the app's Settings (GNANI_API_KEY, TTS_MODEL, TTS_LANGUAGE).

        `sample_rate=None` (the default) uses the pipeline's output rate, so set
        `PipelineParams.audio_out_sample_rate` to TTS_SAMPLE_RATE.
        """
        if api_key is None:
            secret = settings.gnani_api_key
            api_key = secret.get_secret_value().strip() if secret is not None else ""
        if settings.gnani_tts_ws_url != _plugin_tts.GNANI_TTS_WS_URL:
            # The plugin connects to its own constant URL; it has no URL parameter.
            logger.warning(
                f"GNANI_TTS_WS_URL={settings.gnani_tts_ws_url!r} is ignored: pipecat-gnani "
                f"0.5.12 always connects to {_plugin_tts.GNANI_TTS_WS_URL!r}"
            )
        kwargs.setdefault("first_byte_timeout_s", settings.tts_first_byte_timeout_s)
        kwargs.setdefault("request_timeout_s", settings.tts_request_timeout_s)
        return cls(
            api_key=api_key,
            voice=voice,
            model=settings.tts_model,
            language=settings.tts_language,
            sample_rate=sample_rate,
            **kwargs,
        )

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self.sample_rate not in TTS_SAMPLE_RATES:
            await self.push_error(
                f"Timbre cannot synthesise at {self.sample_rate} Hz; use one of "
                f"{TTS_SAMPLE_RATES} for PipelineParams.audio_out_sample_rate",
                fatal=True,
            )

    # --- the interruption fix --------------------------------------------------------

    async def _disconnect(self):
        await super()._disconnect()  # 1.5.0: only sets the "disconnecting" flag
        await self._disconnect_websocket()  # the plugin's real close (cancels its receiver)
        self._end_request()  # the interrupted request is over; the next one may go

    async def _connect(self):
        await super()._connect()
        await self._connect_websocket()  # reopen now, so the next line pays no handshake

    async def _handle_interruption(self, frame: InterruptionFrame, direction: FrameDirection):
        # Close the gate first: a chunk the receiver pushes while the socket is closing must
        # not follow the InterruptionFrame downstream and play.
        self._audio_gate_open = False
        self.interruptions += 1
        await super()._handle_interruption(frame, direction)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if not self._request_done.is_set():
            # One request at a time (ruling 30): wait for the previous `complete`/`error`.
            self.serialise_waits += 1
            try:
                await asyncio.wait_for(self._request_done.wait(), self._request_timeout_s)
            except TimeoutError:
                self.serialise_timeouts += 1
                logger.warning("[timbre] previous request never completed; sending anyway")
                self._end_request(failed=True)
        self._request_done.clear()
        self._first_audio.clear()
        self._audio_gate_open = True  # a new line: its audio may play
        if self._ws is not None and getattr(self._ws, "state", State.OPEN) is not State.OPEN:
            # Timbre closed the socket while it was idle (tts-websocket.md mentions an idle
            # timeout). The plugin keeps the dead socket and the send would fail, losing the
            # line; drop it so the plugin opens a fresh one.
            # UNVERIFIED: Timbre's idle timeout length (the docs do not give it).
            self.idle_reconnects += 1
            await self._disconnect_websocket()
        if self.ledger is not None:
            self.ledger.unit_started(text)
        async for frame in self._send_request(text):
            yield frame
        if self._ws is None:  # the plugin could not connect or send: nothing will come
            self._end_request(failed=True)
        elif not self._request_done.is_set():
            self._cancel_watchdog()
            self._watchdog = self.create_task(self._first_byte_watchdog(text), "timbre-ttfb")

    def request_body(self, text: str, sample_rate: int) -> dict[str, Any]:
        """The Timbre request: the plugin's fields and audio_config (pipecat-gnani 0.5.12
        `_build_tts_payload`), plus `language` for timbre-v3.0 too (the docs list it for
        both models; the plugin only sends it for v2.5)."""
        body: dict[str, Any] = {
            "text": text,
            "voice": self.voice,
            "model": self.model,
            "audio_config": _plugin_tts._build_audio_config(self._settings, sample_rate),
        }
        if self.language is not None:
            body["language"] = self.language
        return body

    async def _send_request(self, text: str) -> AsyncGenerator[Frame | None, None]:
        """The plugin's `GnaniTTSService.run_tts` (pipecat-gnani 0.5.12) with our body."""
        if not self._ws:
            await self._connect_websocket()
        if not self._ws:
            logger.error("[timbre] synthesize failed: not connected")
            await self.push_error(error_msg="Gnani TTS WebSocket not connected")
            yield ErrorFrame(error="Gnani TTS WebSocket not connected")
            return
        try:
            body = self.request_body(text, _plugin_tts._resolved_tts_sample_rate(self))
            self._bot_speaking = True
            self._awaiting_first_chunk = True
            self._pcm_processor.reset()
            await self._ws.send(json.dumps(body))
            await self.start_tts_usage_metrics(text)
        except Exception as e:
            logger.error(f"[timbre] synthesize failed: {type(e).__name__}: {e}")
            await self.push_error(error_msg=f"Error sending TTS request: {e}", exception=e)
            yield ErrorFrame(error=f"Error sending TTS request: {e}", exception=e)
        yield None

    async def _first_byte_watchdog(self, text: str) -> None:
        """Ruling 30: no audio in time means a half-open socket; reopen, resend once, then
        give up on this request (the voice app shows the line as text)."""
        try:
            for attempt in (1, 2):
                try:
                    await asyncio.wait_for(self._first_audio.wait(), self._first_byte_timeout_s)
                    return
                except TimeoutError:
                    if self._request_done.is_set():
                        return
                if attempt == 1:
                    self.first_byte_resends += 1
                    logger.warning("[timbre] no first byte in time; reopening and resending")
                    await self._disconnect_websocket()
                    await self._connect_websocket()
                    if self._ws is None:
                        break
                    rate = _plugin_tts._resolved_tts_sample_rate(self)
                    payload = self.request_body(text, rate)
                    self._bot_speaking = True
                    self._awaiting_first_chunk = True
                    self._pcm_processor.reset()
                    await self._ws.send(json.dumps(payload))
            self.first_byte_failures += 1
            logger.error("[timbre] no audio after a resend; the line is shown as text only")
            self._end_request(failed=True)
            await self._call_event_handler("on_first_byte_timeout", text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a failed resend ends the request, never the pipeline
            logger.error(f"[timbre] first-byte watchdog failed: {type(e).__name__}: {e}")
            self._end_request(failed=True)

    def _cancel_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _end_request(self, *, failed: bool = False) -> None:
        if self._request_done.is_set():
            return
        self._request_done.set()
        self._cancel_watchdog()
        if self.ledger is not None:
            self.ledger.unit_done(failed=failed)

    async def _receive_messages(self):
        """The plugin's receive loop (pipecat-gnani 0.5.12, `GnaniTTSService._receive_messages`)
        with two additions: `complete` and `error` end the current request (serialisation and
        the ledger need to know exactly when), and a clean server close ends it too."""
        try:
            async for msg in self._ws:
                if isinstance(msg, bytes):
                    await self._handle_audio_chunk(msg)
                    continue
                data = json.loads(msg)
                msg_type = data.get("type", "")
                nested = data.get("data")
                if isinstance(nested, dict) and nested.get("audio"):
                    audio_b64 = cast("str", nested["audio"])
                else:
                    audio_b64 = cast("str", data.get("audio", ""))
                if msg_type == "audio":
                    if audio_b64:
                        await self._handle_audio_chunk(base64.b64decode(audio_b64))
                elif msg_type == "complete":
                    if audio_b64:
                        await self._handle_audio_chunk(base64.b64decode(audio_b64))
                    self._bot_speaking = False
                    await self.push_frame(TTSStoppedFrame())
                    self._end_request()
                elif msg_type == "error":
                    error_msg = data.get("message", "Unknown error")
                    logger.error(f"[timbre] stream error: {error_msg}")
                    self._bot_speaking = False
                    self._end_request(failed=True)
                    await self.push_error(error_msg=f"Gnani TTS: {error_msg}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[timbre] receive error: {type(e).__name__}: {e}")
            self._bot_speaking = False
            self._end_request(failed=True)
            await self.push_error(error_msg=f"Gnani TTS receive error: {e}", exception=e)
            await self._call_event_handler("on_connection_error", f"{e}")
            return
        self._end_request(failed=True)  # the server closed the socket mid-request

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSAudioRawFrame):
            if not self._audio_gate_open:
                self.audio_dropped_after_interruption += 1
                return
            if frame.audio:
                self._first_audio.set()
                if self.ledger is not None:
                    self.ledger.audio(len(frame.audio), frame.sample_rate, frame.num_channels)
        await super().push_frame(frame, direction)
