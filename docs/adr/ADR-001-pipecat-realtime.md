# ADR-001: Pipecat for the real-time voice path

- **Status:** Accepted. Not implemented yet: the voice loop is the next phase.
- **Date:** October 25, 2026
- **Source:** [PLAN_v2_DELTA.md](../PLAN_v2_DELTA.md), architecture decision 1; [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 3, 9 and 16; [CTO_NOTES_03.md](../CTO_NOTES_03.md) §1.
- **Revised:** October 26, 2026 (ruling 16): exact pins, barge-in wording, Smart Turn and Hinglish.

## Context

Mock Board is meant to be spoken, not typed. The real-time part of a voice interview has jobs that have nothing to do with reasoning: it must move audio frames between the browser and the server many times a second, decide when the candidate has really finished speaking, stop the board's voice within a few hundred milliseconds when the candidate cuts in (barge-in), and stream speech to and from Gnani Prisma (speech to text) and Gnani Timbre (text to speech). Writing this plumbing ourselves would take most of the time we have, and getting turn-taking wrong ruins the experience more than any wording problem.

Pipecat is an open-source framework built for exactly this kind of voice pipeline. Gnani's own documentation describes a Pipecat integration (`pipecat-gnani`) for its speech services and says Gnani maintains it, while Pipecat's documentation lists it as a community integration that Pipecat itself does not test. Both statements are true, so we cannot assume either side will catch a breaking change for us.

## Decision

The real-time path will use Pipecat with the SmallWebRTC transport, Silero voice-activity detection plus Smart Turn v3 for end-of-turn detection, and `pipecat-gnani` for Prisma and Timbre. We wrap the Gnani integration behind our own small adapter, so a change upstream touches one file.

**Exact pins.** Saying "the version `pipecat-gnani` was tested with" pins nothing by itself, because `pipecat-gnani` 0.5.12 (the latest release) only requires `pipecat-ai>=0.0.50`, and Pipecat itself is already at 1.14.0. So the optional `voice` extra in `pyproject.toml` pins all three packages exactly:

- `pipecat-ai==1.5.0`, the version Gnani's integration page says it was tested with;
- `pipecat-gnani==0.5.12`;
- `gnani-vachana==0.8.2`, which is what pip resolves for those two pins today (`pipecat-gnani` allows `>=0.7.9,<1.0`). Gnani tested the integration on the 0.7 line, so this pin stays only if it passes our Prisma and Timbre contract tests (voice-loop chunk V2); otherwise we drop to 0.7.9.

Pipecat's online documentation describes its newest release, not 1.5.0, so class names and APIs such as the turn and mute "strategies" may differ in the version we pin. When the documentation and the code disagree, we read the 1.5.0 source.

**Resolved conflict (orchestrator ruling 18, October 26, 2026).** `pipecat-gnani` 0.5.12 requires `websockets>=12.0,<16.0`, while our base dependencies first pinned `websockets==17.3`. We chose one environment over two: the base range is now `websockets>=15,<16`, pinned to `15.0.1` in `requirements.txt`. The orchestrator verified this in a clean virtual environment: the full test suite and the offline probe self-test pass on `websockets==15.0.1`, and `pip install --dry-run -e .[voice]` resolves without conflict. When `pipecat-gnani` lifts its cap, raise ours with it. No live Gnani connection has been made on this version yet; the day-1 probes are the first real check.

Pipecat does the audio and turn-taking; it does not do the thinking. It talks to the brain only through the three session calls that already exist: `submit_segment` for each final Prisma transcript, `end_of_turn` when the candidate stops, and `speak` for the board's line. LangGraph is never placed inside the audio pipeline (see ADR-002).

## Consequences

- The brain built in phase 1 needs no change for voice: text mode and voice mode share one session API, which is already covered by tests.
- We depend on a community-tested integration, so we pin all three versions exactly, wrap it, and plan recorded-fixture contract tests for the Prisma and Timbre WebSocket messages.
- Some platform behaviour shapes the design. Gnani's documentation says Prisma currently sends final transcripts only, so the design assumes no interim transcripts until the `x-partials` probe says otherwise. Because of that, the last segment's final transcript arrives only after the candidate stops, and its listener call sits on the critical path (see ADR-002).
- Barge-in uses the plugin's own interruptible speech output: Gnani's documentation says `GnaniTTSService` extends Pipecat's `InterruptibleTTSService`, so synthesis stops cleanly when the candidate cuts in. Timbre's WebSocket page confirms that there is no cancel message, so stopping means closing the connection. We measure the plugin's reconnect cost with the Timbre probe, and add a pre-opened spare connection only if that probe shows reconnecting is slow (rule T1 in CTO_NOTES_03 §4).
- Turn detection for Hinglish is unmeasured. The Smart Turn repository lists Hindi among its 23 languages, but not code-mixed Hinglish, and Pipecat recommends a Silero `stop_secs` of 0.2 seconds when Smart Turn is used. Whether Smart Turn v3 is good enough for our candidates will be decided by the false cut-in rate we measure in spoken sessions, labelled with a rule written down before labelling.
- WebRTC across different networks needs STUN, and TURN is recommended for production by Pipecat's SmallWebRTC documentation. This is a known risk for the deployment on EC2, and a WebSocket audio fallback should be kept in mind.
- Two follow-up decisions should get their own ADRs before Pipecat code is written (CTO_NOTES_02): running our actor as our own Pipecat frame processor rather than Pipecat's Bedrock service, so the guards stay ours; and speaking fact challenges and the opener from a fully buffered, guarded line while other moves stream sentence by sentence.

## Alternatives considered

- **Writing our own WebSocket audio loop.** Full control, but VAD, turn detection, barge-in and transport would all be ours to build and debug in two weeks.
- **Putting the real-time loop inside LangGraph.** LangGraph is not built for frame-level audio streaming or sub-second cancellation (ADR-002).
