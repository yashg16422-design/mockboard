# ADR-007: The actor runs inside our own Pipecat frame processor

- **Status:** Proposed by the voice builder (October 29, 2026). It becomes Accepted when the orchestrator records it. Implementation is voice chunk V3, which waits for DB5 (ruling 22).
- **Date:** October 29, 2026
- **Source:** [CTO_NOTES_02.md](../CTO_NOTES_02.md) (the follow-up decisions named at the end of ADR-001); [CTO_NOTES_03.md](../CTO_NOTES_03.md) §3, chunks V0 and V3; [CTO_NOTES_04.md](../CTO_NOTES_04.md) §7 (the two mode-agnostic hooks); [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 9, 13, 14, 17, 19, 20 and 22; the installed pipecat-ai 1.5.0 source, read for [spikes/NOTES.md](../../spikes/NOTES.md).

## Context

Pipecat is built around a standard shape for a voice bot: the transcript goes into an "LLM context aggregator", a Pipecat LLM service (for example its AWS Bedrock service) generates the reply, and the reply's text tokens flow straight into the speech service. If we used that shape, the language-model call that produces the board's spoken line would belong to Pipecat, not to us.

Phase 1 already built that call, and most of what makes Mock Board trustworthy lives in it:

- the actor's guards in `brain/actor.py`: the word cap, ask-never-assert for fact challenges (ADR-003), the opener guard and the card-figure check;
- the three Bedrock lanes with their own thread pools, clients and short timeouts (ADR-005, ruling 13);
- the actor's single attempt and its no-model fallback lines (ruling 14), and the rule that a masking filler is never followed by a persona fallback line (ruling 17);
- the end-of-turn deadline with a deterministic fallback move (ruling 9);
- our own per-turn trace, which is the source of truth for every latency we publish (ENGINEERING.md §8).

If Pipecat's Bedrock service generated the line, every one of these would have to be rebuilt on top of Pipecat's service classes, and two versions of the same behaviour would exist, one for text mode and one for voice mode.

Reading the 1.5.0 source showed that we do not need Pipecat's LLM pieces to get its turn-taking. In 1.5.0, voice activity detection runs in a `VADProcessor`, and end-of-turn detection (including Smart Turn v3, which is the default stop strategy) runs in a `UserTurnProcessor`. Neither needs an LLM service or an LLM aggregator. The processor raises an `on_user_turn_stopped` event and broadcasts a `UserStoppedSpeakingFrame`, which any processor further down the pipeline can react to. The V1 echo spike uses exactly this shape with no LLM at all.

## Decision

The board's line is produced by our own Pipecat frame processor, which we will call `BrainProcessor` (`src/mockboard/voice/`). Pipecat's Bedrock LLM service, its LLM context aggregators and its "LLM turn completion" strategies are not used.

The pipeline for one session is:

```
SmallWebRTC input -> VADProcessor(Silero) -> Gnani STT (our adapter) -> UserTurnProcessor(Smart Turn v3)
  -> BrainProcessor -> Gnani TTS (our adapter) -> SmallWebRTC output
```

`BrainProcessor` talks to the brain only through the session API that text mode already uses (ADR-001, ADR-002):

1. Every `TranscriptionFrame` (a Prisma final) becomes `session.submit_segment(text, segment_index=k)`. Segment indexes count up within a turn.
2. The user turn stopping (the `UserStoppedSpeakingFrame` that `UserTurnProcessor` broadcasts, which is the same moment its `on_user_turn_stopped` event fires) becomes `session.end_of_turn(deadline)`.
3. The move is then spoken through a new `session.speak_stream()` (V3), which runs the actor on the actor lane, applies the guards, and yields text that `BrainProcessor` pushes to the speech service as `TTSSpeakFrame`s. Which lines are held back until complete and which are streamed sentence by sentence is ADR-008.
4. An `InterruptionFrame` (the candidate cut in) cancels the running actor task. `brain/llm.py` already closes the boto3 stream when its task is cancelled. What then counts as "spoken" is ADR-008.

The processor knows nothing about interview or debate modes (ruling 19). It reads two mode-agnostic hooks from the session (CTO_NOTES_04 §7): a per-sentence `guard(move, sentence)`, which is a no-op in UPSC mode, and per-session turn-detection parameters. In 1.5.0 the second is straightforward, because `UserTurnProcessor`, its strategies and the Silero parameters are built once per pipeline, and so once per session.

What we keep from Pipecat is everything that moves audio or decides turns: the transport, Silero, Smart Turn v3, the user-turn controller, interruption broadcasting, the output audio queue, and the Gnani speech services (behind our own adapter, ADR-001).

## Consequences

- Text mode and voice mode keep one brain, one set of guards and one set of tests. The voice loop adds a processor; it does not fork the actor.
- Cancellation is ours to get right. Pipecat's LLM service would have stopped generation on interruption by itself; we must cancel the actor task on `InterruptionFrame` and prove it with an offline test (V4).
- Pipecat's own LLM metrics and OpenTelemetry LLM spans will not see the actor. Our trace already records the actor request, first token and first sentence, and V3 adds `turn_end`, `actor_first_sentence` and `tts_first_byte` marks, so nothing we publish depends on Pipecat's LLM metrics.
- Two plugin behaviours found while reading the 1.5.0 source must be handled in our Gnani adapter (V2), not left to Pipecat. Both are described in spikes/NOTES.md:
  1. `GnaniSTTService` sends each pipeline audio frame as it is (20 ms, 640 bytes from SmallWebRTC), while Gnani's documentation requires 1,024-byte frames. It also sends none of our VAD headers or `x-max-speech-s`.
  2. `GnaniTTSService` does not close the Timbre socket on interruption. Pipecat 1.5.0's `InterruptibleTTSService` calls `_disconnect()` and `_connect()`, which in 1.5.0 only flip a flag, and the plugin does not override them. An offline test with a fake socket showed every remaining audio chunk of an interrupted line still reaching the output after the interruption. A three-line subclass that makes those two methods close and reopen the socket fixed it in the same test (the spike's default). ADR-001's sentence that "synthesis stops cleanly" therefore does not hold for 0.5.12 on 1.5.0 without that fix.
- Turn detection waits for a transcript. In 1.5.0, Smart Turn's stop strategy does not end the turn until at least one final transcript has arrived (`wait_for_transcript=True` by default) and, because Prisma does not mark finals as "finalized", it then waits out an STT safety timeout taken from `ttfs_p99_latency` (1.0 s by default, minus Silero's `stop_secs`). This puts the Prisma final directly on the turn-end path. V3 must set `ttfs_p99_latency` from probe P2 instead of leaving the default.
- A late Prisma final can open a new user turn. The default start strategies include `TranscriptionUserTurnStartStrategy`, which starts a turn (and broadcasts an interruption) on any transcript that arrives when no user turn is open. A final that arrives just after the turn stopped would therefore interrupt the board's line. The spike logs this; V3 or V4 decides whether to drop that strategy (`--vad-start-only` in the spike tries this).

## Alternatives considered

- **Pipecat's AWS Bedrock LLM service with our prompts.** This is the least code on day one, but the guards, lanes, single attempt, fallbacks and deadline would all have to be rebuilt around Pipecat's service classes and kept in step with text mode by hand.
- **A custom Pipecat `LLMService` subclass that wraps our actor.** This would keep our guards, but it would pull in Pipecat's context aggregation and function-call machinery, which we do not use, and the `speak` path would still differ from text mode.
- **Running the brain outside the pipeline and injecting `TTSSpeakFrame`s from an event handler.** This works for the echo spike, but it splits one turn's logic between event handlers and frames, which makes ordering and cancellation harder to test offline than a single processor.
