# Spike notes: pipecat-ai 1.5.0 with pipecat-gnani 0.5.12, as they really are

Written by the voice builder on October 29, 2026, for chunks V2 to V4. Everything here was read from the installed source in a fresh virtual environment, not from Pipecat's online docs, which describe newer releases. Each item says how it was checked:

- **[source]** means it was read in the installed code.
- **[headless]** means it was seen in a headless run of the echo spike: an aiortc client played a synthetic (flite) voice clip into `server.py --fake-speech`, with fake speech services.
- **[offline test]** means a test in `tests/voice/test_echo_spike.py` checks it, using fakes.
- **UNVERIFIED** means it still needs the owner's live run with real Gnani services and a real microphone.

## 1. Versions that work together

- `pipecat-ai[runner,silero,webrtc]==1.5.0`, `pipecat-gnani==0.5.12`, `gnani-vachana==0.7.9` and `aiortc==1.15.0`. These install cleanly into the brain's environment on Python 3.11 and 3.13, and `pip check` reports nothing. Every package resolves to a macOS arm64 wheel for Python 3.13 and 3.14.
- **gnani-vachana 0.8.2 does not work.** With it, `import pipecat_gnani` fails with `ImportError: cannot import name 'TIMBRE_V20_VOICES'`, because 0.8.2 removed a name that the plugin imports. Versions 0.7.9, 0.8.0 and 0.8.1 have that name. We use 0.7.9, which is the line Gnani tested and the fallback that ADR-001 names. [source]
- Without the `webrtc` extra there is no aiortc, so there is no SmallWebRTC. Without the `runner` extra there is no FastAPI or uvicorn. The `silero` extra is empty in 1.5.0, because Silero and Smart Turn v3 ship inside the base wheel together with their ONNX models. [source]

## 2. The real names in 1.5.0

**Running a pipeline.** `PipelineTask` and `PipelineRunner` are deprecated aliases since 1.3.0. The current names are `PipelineWorker` (in `pipecat.pipeline.worker`, which also holds `PipelineParams`) and `WorkerRunner` (in `pipecat.workers.runner`). The pattern is `runner = WorkerRunner(handle_sigint=False)`, then `await runner.add_workers(worker)`, then `await runner.run()`. `PipelineWorker` turns on RTVI by default (`enable_rtvi=True`); our page does not speak RTVI, so the spike turns it off. [source, headless]

**Transport.** `SmallWebRTCTransport(webrtc_connection=..., params=TransportParams(audio_in_enabled=True, audio_out_enabled=True))` comes from `pipecat.transports.smallwebrtc.transport`, and `TransportParams` from `pipecat.transports.base_transport`. In 1.5.0, `TransportParams` has **no** `vad_analyzer` or `turn_analyzer` field. The transport's events are `on_client_connected`, `on_client_disconnected` and `on_app_message`. Input audio arrives as `InputAudioRawFrame`s of 20 ms each (640 bytes at 16 kHz). [source, headless]

**Offer endpoint.** `SmallWebRTCRequestHandler` (in `pipecat.transports.smallwebrtc.request_handler`) does the work. `handle_web_request(request=SmallWebRTCRequest(...), webrtc_connection_callback=...)` returns `{sdp, type, pc_id}`, and `handle_patch_request(SmallWebRTCPatchRequest(...))` adds trickled ICE candidates. `SmallWebRTCRequest.from_dict` accepts `request_data` or `requestData`. The smallest working client sends one audio transceiver (index 0), opens a data channel that sends `ping` messages (the connection's `is_connected()` relies on them), and POSTs a non-trickle offer. [source, headless]

**Pipecat's development runner** (`pipecat.runner.run.main`) serves `POST` and `PATCH /api/offer` and a prebuilt React UI at `/client` (from `pipecat-ai-prebuilt`, which is not pinned; it resolved to 1.3.0). It listens on `localhost:7860` and finds a `bot(runner_args)` function by scanning the current folder. It also calls `load_dotenv(override=True)` when imported, so `.env` overrides the shell's environment. The spike uses its own small FastAPI server instead. [source]

**Voice activity.** `VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams()))` comes from `pipecat.processors.audio.vad_processor`. It broadcasts `VADUserStartedSpeakingFrame` (with `start_secs`), `VADUserStoppedSpeakingFrame` (with `stop_secs`) and `UserSpeakingFrame`. The `VADParams` defaults are confidence 0.7, `start_secs` 0.2, `stop_secs` 0.2 and `min_volume` 0.6, so Pipecat's recommended 0.2 s stop is already the default. [source, headless]

**Turn detection.** `UserTurnProcessor(user_turn_strategies=UserTurnStrategies(start=[...], stop=[...]), user_turn_stop_timeout=5.0)` comes from `pipecat.turns.user_turn_processor`. It needs no LLM service and no LLM aggregator.

- The default start strategies are `VADUserTurnStartStrategy` and `TranscriptionUserTurnStartStrategy`.
- The default stop strategy is `TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())`.
- **Smart Turn v3 is available in 1.5.0.** `LocalSmartTurnAnalyzerV3` (in `pipecat.audio.turn.smart_turn.local_smart_turn_v3`) loads the bundled model `smart-turn-v3.2-cpu.onnx` through onnxruntime, with no extra install. The `local-smart-turn` extra (torch, transformers) is for the older v2 analyzer. `SmartTurnParams` defaults to `stop_secs=3`, `pre_speech_ms=500` and `max_duration_secs=8`.
- `MinWordsUserTurnStartStrategy(min_words=N)` also exists, which is a candidate for V4's minimum-words rule for barge-in.

[source, headless]

**The "user turn stopped" event.** `UserTurnProcessor` fires `on_user_turn_stopped(processor, strategy)`. The strategy is `None` when the 5 s `user_turn_stop_timeout` ended the turn instead of a strategy. At the same moment it broadcasts `UserStoppedSpeakingFrame` (a SystemFrame) both ways. Its other events are `on_user_turn_started(processor, strategy)`, `on_user_turn_inference_triggered`, `on_user_turn_stop_timeout` and `on_user_turn_idle`. [source, headless, offline test]

**Final transcripts.** These are `TranscriptionFrame(text, user_id, timestamp, language, result, finalized=False)`. The Gnani plugin puts Prisma's raw JSON message in `result`, and never sets `finalized`. Interim results would be `InterimTranscriptionFrame`, but Gnani is expected to send none. [source]

**Speech output.** Text goes in as `TTSSpeakFrame(text, append_to_context=...)`; the spike passes `False`, since there is no LLM context. Out of the TTS service come `AggregatedTextFrame`, `TTSStartedFrame`, `TTSAudioRawFrame`, `TTSTextFrame` and `TTSStoppedFrame`. The output transport adds `BotStartedSpeakingFrame`, `BotSpeakingFrame` and `BotStoppedSpeakingFrame` when audio really starts and stops leaving the server. [source, headless]

**How interruption reaches the TTS.**

1. A new user turn starts, and the start strategy has `enable_interruptions=True` (the default).
2. `UserTurnProcessor` broadcasts `InterruptionFrame` (a SystemFrame) both ways.
3. In the TTS, `TTSService._handle_interruption` clears the text aggregator and audio contexts. Then `InterruptibleTTSService._handle_interruption` calls `self._disconnect()` and `self._connect()` if the bot was speaking.
4. The output transport cancels and recreates its audio task, which drops queued audio, and sends `BotStoppedSpeakingFrame`.

In the headless run, `BotStoppedSpeaking` followed the interruption within about 3 ms (with fake TTS). [source, headless]

**Gnani services.**

- `GnaniSTTService(api_key, sample_rate, settings=GnaniSTTService.Settings(language, format, itn_native_numerals))`.
- `GnaniTTSService(api_key, voice_id, model, sample_rate, settings=GnaniTTSService.Settings(voice, language, encoding, container, sample_width))`.
- The plugin also has `GnaniSSETTSService`, `GnaniHttpTTSService` and `GnaniHttpSTTService`.
- Both streaming services fire `on_connected`, `on_disconnected` and `on_connection_error`.

[source, offline test]

## 3. Surprises, most important first

1. **Barge-in does not stop Timbre.** Pipecat 1.5.0's `WebsocketService._connect()` and `_disconnect()` only flip a flag, and `GnaniTTSService` implements `_connect_websocket()` and `_disconnect_websocket()` instead, so an interruption never closes the Timbre socket. With a fake socket streaming 20 chunks 50 ms apart, interrupted at 0.3 s, the stock plugin pushed **15 audio chunks after the interruption** and never reconnected. Our three-line subclass (`ClosingGnaniTTS` in `bot.py`) makes the two methods close and reopen the socket. With it, the socket closed about 10 ms after the interruption, no audio leaked, and a fresh socket opened for the next line. The V2 adapter must carry this fix. ADR-001's "synthesis stops cleanly" does not hold for this plugin version without it. [offline test; UNVERIFIED in a real browser]
2. **A SystemFrame can overtake DataFrames inside one processor.** Every `FrameProcessor` handles SystemFrames at once in its input task, while other frames wait in a second queue. So `UserStoppedSpeakingFrame` was handled by the echo processor **before** the turn's last `TranscriptionFrame`, even though `UserTurnProcessor` pushed the transcript first. In the first headless run this skipped the echo for turn 1. The spike now queues a private marker frame on stop and echoes when the marker comes up; the log field `transcripts_queued_behind_stop` showed 1 for that turn. V3's `BrainProcessor` must not call `end_of_turn` directly on the stop frame for the same reason. Related, from reading the source only: an `InterruptionFrame` empties each processor's waiting queue, apart from `UninterruptibleFrame`s, so a final that is still queued when a new turn starts could be dropped. [headless, offline test; the drop is source only]
3. **Turn end waits for a transcript, and Smart Turn can add 3 s.** The Smart Turn stop strategy has `wait_for_transcript=True`, so the turn ends only once at least one final has arrived. Gnani never sets `finalized`, so after that the strategy also waits out `ttfs_p99_latency - stop_secs`. For the Gnani service `ttfs_p99_latency` is unset, so Pipecat uses 1.0 s and logs a warning, which makes the wait 0.8 s. If Smart Turn judges the turn incomplete, the turn ends only after Smart Turn's own 3 s of silence. In the headless run with fake STT, one turn ended 117 ms after Silero's stop and two ended about 3.0 s after it. That was synthetic flite speech, so it says nothing about real Hinglish. V3 should set `ttfs_p99_latency` from probe P2 and decide Smart Turn's `stop_secs` from the false cut-in rate. [source, headless]
4. **A late transcript starts a new user turn and interrupts the bot.** The docstring of `TranscriptionUserTurnStartStrategy` says "while the bot is speaking", but the code starts a turn on any transcript that arrives with no user turn open, and that turn start broadcasts an interruption. A Prisma final that lands just after Smart Turn ended the turn would cut off the board's reply. The log line `transcript_outside_turn` counts these, and `--vad-start-only` removes the strategy. [source; UNVERIFIED how often it happens with Prisma]
5. **The STT plugin does not follow Gnani's framing or send our headers.** `GnaniSTTService.run_stt` sends each pipeline frame as it is (640 bytes), while Gnani's documentation says each binary frame is exactly 1,024 bytes (the SDK's own `STREAM_CHUNK_BYTES` is 1024). The spike's `Framed1024GnaniSTT` re-frames the audio. The plugin's connect headers are only `x-api-key-id`, `lang_code`, `x-sample-rate`, `x-api-request-id`, `x-format`, `itn_native_numerals` and the SDK's identification headers. None of our VAD headers, `x-max-speech-s` or bias settings are sent, so the V2 adapter must override the connect step. [source, offline test; UNVERIFIED whether Prisma accepts 640-byte frames]
6. **TTS model and language defaults.**
   - gnani-vachana 0.7.9's default model is `timbre-v2.0`, which has only Deepak, Kaveri, Pranav and Shubhra, and the plugin's default voice is Pranav. We must pass `model="timbre-v2.5"`.
   - The SDK validates the language before sending and refuses `hi-en`. It accepts only `auto`, `bn-IN`, `en-IN`, `gu-IN`, `hi-IN`, `kn-IN`, `ml-IN`, `mr-IN`, `pa-IN`, `ta-IN` and `te-IN`. Our `config.py` default `TTS_LANGUAGE=hi-en` would therefore raise in the plugin, so the V2 adapter must map it (omit it, or use `auto`) until the Timbre probe shows what the server accepts. The spike omits the language by default.

   [source]
7. **TTS audio framing.** The plugin asks Timbre for a `wav` container and strips the header itself, while our probes asked for `raw`. The WebSocket service pushes `TTSAudioRawFrame`s from its receive task with no `context_id`. [source]
8. **`import pipecat` tries to download NLTK's `punkt_tab` data** (`pipecat/utils/string.py`). On the owner's Mac this happens once and needs internet. Offline, it logs an error and carries on, and the tests pass. [source]
9. **`TTSTextFrame` is not proof of playback.** It appears right after `TTSStartedFrame`, before the audio has played, which is why ADR-008 measures playback from `BotStarted` and `BotStoppedSpeakingFrame` instead. [headless, offline test]
10. **Per-session turn settings are easy.** `UserTurnProcessor`, its strategies and `VADParams` are built once per pipeline, so once per session. At runtime there are also `UserTurnController.update_strategies()` and `VADParamsUpdateFrame`, which were not tried. This answers CTO_NOTES_04 §7's open question about per-session turn parameters. [source]
11. **Small traps.** A plain `FrameProcessor` subclass forwards nothing unless `process_frame` pushes. Custom services must initialise every settings field (for example `STTSettings(model=None, language=None)`), or Pipecat logs an ERROR. FastAPI cannot resolve request annotations that are imported inside a function when the module uses `from __future__ import annotations`; the result was a 422 error, which was our bug and is fixed. [headless, offline test]

## 4. Rough numbers from the headless run (not real speech, not real Gnani)

| Step | Time |
|---|---|
| Silero end of speech to turn stop, Smart Turn "complete" (fake STT, p99 set to 0.05 s) | 117 ms |
| Silero end of speech to turn stop, Smart Turn "incomplete" | about 3,000 ms |
| Turn stop to the echo's first audio at the output (fake TTS) | 6 to 9 ms |
| User speech start to the interruption, and the interruption to `BotStoppedSpeaking` | about 2 ms and 3 ms |

## 5. Questions the owner's live run must answer (all UNVERIFIED)

- Do Prisma and Timbre connect through the plugin with the real key? Does Prisma accept 1,024-byte frames, and also 640-byte ones (`--stock-stt`)?
- How long after Silero's stop does Prisma's final arrive, and does it ever land after the turn ended (`transcript_outside_turn`)?
- How often does Smart Turn wait the full 3 s on real Hinglish speech?
- Does barge-in silence Poorvi within a fraction of a second, and does `--stock-tts` keep talking as predicted?
- Does Chrome on macOS connect to `localhost` without STUN, and does the browser's echo cancellation keep the bot from interrupting itself without headphones?
