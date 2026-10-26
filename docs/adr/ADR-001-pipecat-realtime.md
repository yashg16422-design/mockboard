# ADR-001: Pipecat for the real-time voice path

- **Status:** Accepted. Not implemented yet: the voice loop is the next phase.
- **Date:** October 25, 2026
- **Source:** [PLAN_v2_DELTA.md](../PLAN_v2_DELTA.md), architecture decision 1; [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 3 and 9.

## Context

Mock Board is meant to be spoken, not typed. The real-time part of a voice interview has jobs that have nothing to do with reasoning: it must move audio frames between the browser and the server many times a second, decide when the candidate has really finished speaking, stop the board's voice within a few hundred milliseconds when the candidate cuts in (barge-in), and stream speech to and from Gnani Prisma (speech to text) and Gnani Timbre (text to speech). Writing this plumbing ourselves would take most of the time we have, and getting turn-taking wrong ruins the experience more than any wording problem.

Pipecat is an open-source framework built for exactly this kind of voice pipeline. Gnani's own documentation describes a Pipecat integration (`pipecat-gnani`) for its speech services and says Gnani maintains it, while Pipecat's documentation lists it as a community integration that Pipecat itself does not test. Both statements are true, so we cannot assume either side will catch a breaking change for us.

## Decision

The real-time path will use Pipecat, pinned to the version that `pipecat-gnani` was tested with (v1.5.0), with the SmallWebRTC transport, Silero voice-activity detection plus Smart Turn v3 for end-of-turn detection, and `pipecat-gnani` for Prisma and Timbre. We pin both packages and wrap the Gnani integration behind our own small adapter, so a change upstream touches one file.

Pipecat does the audio and turn-taking; it does not do the thinking. It talks to the brain only through the three session calls that already exist: `submit_segment` for each final Prisma transcript, `end_of_turn` when the candidate stops, and `speak` for the board's line. LangGraph is never placed inside the audio pipeline (see ADR-002).

## Consequences

- The brain built in phase 1 needs no change for voice: text mode and voice mode share one session API, which is already covered by tests.
- We depend on a community-tested integration, so we pin versions, wrap it, and plan recorded-fixture contract tests for the Prisma and Timbre WebSocket messages.
- Some platform behaviour shapes the design. Gnani's documentation says Prisma currently sends final transcripts only, so the design assumes no interim transcripts until the `x-partials` probe says otherwise. Timbre has no documented cancel message, so barge-in will close and replace the TTS connection.
- WebRTC across different networks needs STUN, and TURN is recommended for production by Pipecat's SmallWebRTC documentation. This is a known risk for the deployment on EC2, and a WebSocket audio fallback should be kept in mind.
- Two follow-up decisions should get their own ADRs before Pipecat code is written (CTO_NOTES_02): running our actor as our own Pipecat frame processor rather than Pipecat's Bedrock service, so the guards stay ours; and speaking fact challenges and the opener from a fully buffered, guarded line while other moves stream sentence by sentence.

## Alternatives considered

- **Writing our own WebSocket audio loop.** Full control, but VAD, turn detection, barge-in and transport would all be ours to build and debug in two weeks.
- **Putting the real-time loop inside LangGraph.** LangGraph is not built for frame-level audio streaming or sub-second cancellation (ADR-002).
