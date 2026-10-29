"""Timbre TTS for the voice loop: the stock Gnani plugin plus our interruption fix (ruling 25).

Ruling 24 keeps the stock `pipecat_gnani.GnaniTTSService` for TTS because it works; this
module is the one place that knows about it, so a plugin upgrade touches only this file.

Two things are ours:

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

The language is checked against gnani-vachana 0.7.9's own list before anything is sent,
because the SDK refuses values outside it (it refuses `hi-en`); see config.py TTS_LANGUAGE.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from gnani.tts.client import SUPPORTED_TTS_LANGUAGES
from loguru import logger
from pipecat.frames.frames import Frame, InterruptionFrame, StartFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat_gnani import GnaniTTSService
from pipecat_gnani import tts as _plugin_tts

from mockboard.config import Settings
from mockboard.speech.gnani_protocol import TTS_SAMPLE_RATES, check_voice

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
        check_voice(model, voice)  # the published catalog, before spending credits
        super().__init__(
            api_key=api_key,
            model=model,
            sample_rate=sample_rate,
            settings=GnaniTTSService.Settings(voice=voice, language=language, **RAW_PCM),
            **kwargs,
        )
        self._audio_gate_open = True
        self.interruptions = 0
        self.audio_dropped_after_interruption = 0

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
        self._audio_gate_open = True  # a new line: its audio may play
        async for frame in super().run_tts(text, context_id):
            yield frame

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSAudioRawFrame) and not self._audio_gate_open:
            self.audio_dropped_after_interruption += 1
            return
        await super().push_frame(frame, direction)
