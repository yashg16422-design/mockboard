# ADR-008: Which board lines are buffered or streamed, and what counts as "spoken" after a barge-in

- **Status:** Proposed by the voice builder (October 29, 2026). It becomes Accepted when the orchestrator records it. Implementation is voice chunks V3 (streaming) and V4 (barge-in), after DB5 (ruling 22).
- **Date:** October 29, 2026
- **Source:** [CTO_NOTES_03.md](../CTO_NOTES_03.md) §3 (V0, V3, V4, V5) and §5; [CTO_NOTES_04.md](../CTO_NOTES_04.md) §7 (per-sentence guard hook); [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 11, 14, 17 and round 7 (unspoken moves still count toward pressure limits); [ADR-003](ADR-003-ask-never-assert.md); [ADR-007](ADR-007-actor-as-own-frame-processor.md); the installed pipecat-ai 1.5.0 and pipecat-gnani 0.5.12 source ([spikes/NOTES.md](../../spikes/NOTES.md)).

## Context

The actor streams its line token by token. Sending each finished sentence to Timbre as soon as it exists makes the board start talking sooner, which matters because the actor's first sentence and Timbre's first byte together take 1.5 s of our 3.0 s p95 budget (CTO_NOTES_03 §4).

Some guards, however, can only judge a whole line. A fact challenge must say the card's figure, must not assert that the candidate is wrong, and must end as a question (ADR-003). The opener must be open-ended and must not state an uncarded fact about a place. If either line were streamed, the first sentence could already be playing before the guard sees the sentence that breaks the rule, and the deterministic template could no longer replace it.

Barge-in raises a second question. When the candidate cuts in, part of the board's line has been heard and part has not. The next turn's listener receives `last_question`, the report quotes the board's lines, and the strategist counts moves. All of them should reflect what the candidate actually heard, not what the actor generated.

Two facts from the 1.5.0 source shape the answer:

- Timbre returns no word timestamps, so Pipecat cannot tell us which words played. The `TTSTextFrame` that Pipecat's TTS base class emits for a request appears right after the request starts (seen in the offline interruption test), long before its audio has finished playing, so it is not evidence that the text was heard.
- The output transport reports `BotStartedSpeakingFrame` and `BotStoppedSpeakingFrame` when audio really starts and stops leaving the server, and it clears its audio queue on `InterruptionFrame`. Timbre's audio for each request arrives as raw 16-bit PCM, so its length in milliseconds is known from its byte count.

## Decision

### 1. Buffered lines

These lines are generated in full, guarded as a whole, and only then sent to Timbre:

- **Fact challenges.** The ask-never-assert guard needs the whole line, and the deterministic template must replace it if the guard fails.
- **The opener.** The opener guard needs the whole line.
- **The acknowledgement preface** after a conceded error (ruling 11). It is a fixed code-side sentence, so it is known before the model runs. In V5 it plays from audio synthesised once and cached, while the rest of the line is still being generated.
- **Every line that needs no model:** persona fallback lines (ruling 14), move templates (ruling 17), and masking fillers (V5). Their full text exists at once, so there is nothing to stream.

A buffered line is still sent to Timbre one sentence per request, so that barge-in accounting (section 3) works per sentence. Buffering means that no sentence is sent until the whole line has passed its guard.

### 2. Streamed lines

Every other move (probe, counter, pressure, steelman, reassure, move on, and any move the debate mode adds) streams sentence by sentence:

- A sentence is complete at `.`, `?`, `!` or `।` followed by a space or the end of the stream. A full stop between two digits, as in "12.5", is never a boundary (the open point from round 9).
- Each complete sentence passes the word cap and the session's per-sentence `guard(move, sentence)` (a no-op in UPSC mode, CTO_NOTES_04 §7). Only then is it pushed as its own `TTSSpeakFrame`.
- If a sentence fails its guard, or the actor stream fails after at least one sentence was sent, the line stops there. If nothing sent so far ends with a question mark, the move's deterministic template question is appended, so the turn still hands the floor back with a question. If nothing at all was sent, the existing ruling 14 and ruling 17 fallbacks apply unchanged.
- The trace records the time of the first sentence pushed (`actor_first_sentence`) and the first Timbre byte (`tts_first_byte`).

### 3. What counts as spoken after a barge-in

For each sentence sent to Timbre, the voice processor keeps its text and the milliseconds of audio received for it. It measures playback from `BotStartedSpeakingFrame` to the `InterruptionFrame` that ends it (or to `BotStoppedSpeakingFrame` if nobody interrupted).

- A sentence whose audio played to the end counts as spoken in full.
- For the sentence that was cut, the spoken words are estimated from the share of its audio that played: the first `floor(share × word count)` words, where the share is the milliseconds played divided by the milliseconds of audio received for that sentence. Speech is assumed to run at an even pace, so the trace calls this an estimate (`spoken_estimated=true`). If the sentence's audio had not fully arrived, the received length is used, which can only make the estimate smaller.
- Sentences that never started playing are not spoken.
- A masking filler is never part of the board's spoken line.

The session then records:

- `board_lines[...].text` and the trace's `board_line_spoken` hold only the spoken text, with `interrupted=true`. The full generated line stays in the trace as `board_line_generated`, so a cut-off line can still be studied.
- `last_question` for the next turn is the spoken text. If nothing was spoken, it stays the previous question, because the candidate is still answering that one.
- The move still counts toward pressure limits and topic budgets, as text mode already does for unspoken moves (round 7). This is the conservative choice, because it can only make the board gentler.
- An interrupted fact challenge keeps its claim marked as challenged, so the board never repeats the same challenge. The trace records `challenge_heard=false` when the sentence holding both figures did not finish, and the report can say so.

The session API changes these imply (a `speak_stream()` that yields sentences, and a way to tell the session what was spoken) are V3 and V4 work in `session.py`, sequenced after DB5 (ruling 22).

## Consequences

- Fact challenges and openers start a little later than streamed lines, by the time the actor takes to finish them (a 30-word cap keeps that short). Correctness of what the board asserts matters more here than a few hundred milliseconds.
- Streamed lines start as soon as their first sentence passes, which is what the 1.2 s actor first-sentence budget assumes.
- A cut-off line is recorded honestly, so the listener and the report never see words the candidate did not hear. The word-level split is an estimate, and the trace says so.
- Barge-in accounting depends on interruption really stopping Timbre. With pipecat-gnani 0.5.12 on Pipecat 1.5.0 it does not, unless our adapter makes `_disconnect()` and `_connect()` close and reopen the socket (ADR-007, spikes/NOTES.md). Until V2 adds that to the adapter, stale audio after an interruption would make "played" longer than what the candidate wanted to hear.

## Alternatives considered

- **Stream everything and guard after the fact.** This is faster, but a failing fact challenge could already be half spoken before its guard runs, which breaks ADR-003.
- **Buffer everything.** This is simpler and safer, but it adds the whole generation time to every turn and uses up most of the latency budget.
- **Count a line as spoken as soon as it is sent to Timbre, or when `TTSTextFrame` appears.** This is easy, but it overstates what the candidate heard, and in 1.5.0 `TTSTextFrame` appears before the audio has played.
- **Treat a cut sentence as not spoken at all.** This is simple, but a board question cut after most of its words would vanish from `last_question`, and the next turn's listener would misread the answer.
