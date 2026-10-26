# CTO Notes 03: Docs Accuracy, M1–M6 Closure, Voice-Loop Plan, Probe Rules (Oct 26, 2026)

I reviewed HEAD `24ac84b` and re-ran `make test` (342 pass), `make lint`, `make eval-gate` and `make cli-fake`. Facts were re-checked today against the LangGraph, Pipecat, Smart Turn, Bedrock/Nova and Gnani docs, PyPI, and the installed langgraph and botocore sources. Unconfirmed items are marked "(unverified)". No code was edited.

## 1. Accuracy of README, WALKTHROUGH and ADR-001 to ADR-006

The documents are accurate and clearly written. Every setting value matches `config.py`. The Clopper–Pearson figures, LangGraph's `uniform(0, 1)` second jitter, the `topK: 1` placement, the flat-schema rule, and Nova Pro's lack of a structured-output mode and its caching limits (1K minimum, 20K maximum) are all correct. These are the inaccuracies, most important first.

1. **ADR-002 "Consequences" bullet 1, and WALKTHROUGH §1 ("Only one model call sits on the critical path").** Both claim the listener "usually finishes while the candidate is still talking". That is false for the **last** segment: Prisma sends its final transcript only after the candidate stops and the server VAD closes the segment. *Correction:* "Between end of turn and the actor there can be two model calls: the last segment's listener, bounded by `move_deadline_ms` (after which the previous segment's move is used), and the actor."
2. **ADR-001 "Decision", the pin.** "Pinned to the version `pipecat-gnani` was tested with (v1.5.0)" does not pin anything by itself.
   - Gnani's page says "Tested with Pipecat v1.5.0". But `pipecat-gnani` 0.5.12 (the latest) requires only `pipecat-ai>=0.0.50`, and Pipecat is now 1.14.0 (Oct 24).
   - `gnani-vachana` is at 0.8.2, which is allowed, but the integration was tested on the 0.7 line.
   - Pipecat's online docs describe the newest release, so APIs such as the turn and mute "strategies" may differ in 1.5.0 (unverified).
   - *Correction:* "Pin `pipecat-ai==1.5.0`, `pipecat-gnani==0.5.12`, and the `gnani-vachana` version that passes our contract tests. Read the 1.5.0 source when the docs disagree." Update the `requirements.txt` note to match.
3. **ADR-001 "Consequences", barge-in.** This reads as if we build the Timbre reconnect ourselves. Gnani's docs say `GnaniTTSService` extends Pipecat's `InterruptibleTTSService` ("synthesis stops cleanly"). Timbre's WebSocket page does confirm that no cancel message exists. *Correction:* "We use the plugin's interruptible TTS, measure its reconnect cost, and add a pre-opened spare connection only if the Timbre probe shows reconnecting is slow."
4. **WALKTHROUGH §3.5.** "A checkpointer saves the state after each run" is wrong. The docs say: "LangGraph creates a checkpoint at each super-step boundary." *Correction:* "…after every step of the graph (each super-step), keyed by `thread_id`."
5. **WALKTHROUGH §3.7.** "None of the branches' results are kept" holds only because the fact-check graph has no checkpointer. With a checkpointer, the writes of branches that succeeded are saved and are not re-run on resume. *Correction:* add "(in a graph without a checkpointer, like this one)".
6. **ADR-005 "Context".** "Bedrock's default read timeout is long (30 seconds in our settings)" conflates two numbers. *Correction:* "botocore's default read timeout is 60 s; our background lane uses 30 s."
7. **ADR-004, stop sequence (minor).** Nova's "Choosing a tool" page recommends `</tool>` whenever any tool choice is set, not only the forced `tool` mode. *Correction:* reword accordingly.
8. **ADR-001, Smart Turn v3 (an omission).** The Smart Turn repository lists Hindi among its 23 languages, but not code-mixed Hinglish. Pipecat recommends a Silero `stop_secs` of 0.2 s with Smart Turn. *Correction:* add a sentence saying Hinglish turn detection is unmeasured and that the false cut-in rate will decide it.

ADR-003, ADR-006 and README have no errors. The Nova Pro model card gives a 10K maximum output, so the code's 5,000-token ceiling is safe, and its "to be confirmed" comment can now cite the card.

## 2. Round-2 must-fixes M1–M6: all genuinely closed

- **M1.** `VoteCache.put` refuses fallbacks, no `CachePolicy` remains, and a two-session throttle test plus the chaos check cover it.
- **M2.** The R3, R3b and R7 limits are tested by `test_board_invariants.py`. Live, an honest "don't know" gets `reassure`, and a concession is acknowledged.
- **M3.** `spoken.py`, `source_short`, `actor.v2.md` and the new guard are in. The test that pinned the defect is replaced, and the live line is correct.
- **M4.** The two graphs, `submit_segment`/`end_of_turn`/`speak`, the delay validator, and 13 latency-budget tests all exist.
- **M5.** Three lanes with their own executors and clients, a pool validator, and a control test that fails on one shared pool.
- **M6.** The repo is under git (23 commits) and the README exists.

**Still open from round 2.** Should-fix 6 is not done: the `--app-prompts` mode for `nova_probe.py`. The probe still times a synthetic 1,500-token prompt with a toy schema. Rules N1 and N2 below need the real listener and actor prompts, so build this before the owner pays for the Nova probe.

## 3. Voice-loop plan (build order, commit-sized)

In every chunk, Pipecat moves audio and detects turns, the brain is reached only through the Session API, `make test` stays offline and green, each new stage gets a trace mark, and risky behaviour sits behind a flag that is off by default. **[probe]** marks a chunk that waits for the owner's day-1 numbers.

- **V0. Pins and two ADRs (½ day).** Add a `voice` optional extra with the exact pins from §1.2. ADR-007: the actor is our own frame processor, and Pipecat's Bedrock LLM service is not used, so our guards stay ours. ADR-008: fact challenges, the opener and acknowledgements play from a buffered, guarded line, other moves stream sentence by sentence, and the ADR defines what counts as "spoken" after a barge-in. *Done when* `pip install -e .[voice]` works in a fresh Python 3.11 venv and `make test` passes without the extra.
- **V1. Throwaway spike in `spikes/` (½ day, after the Prisma and Timbre probes).** An echo bot: SmallWebRTC → Silero → Smart Turn v3 → `GnaniSTTService` → repeat → `GnaniTTSService`. The goal is to learn 1.5.0's real API for a "user turn stopped" event without Pipecat's LLM service, which frames carry finals, and how interruption reaches the TTS. *Done when* the owner hears his own words back in a browser and `spikes/NOTES.md` lists the real class and frame names.
- **V1b. Deploy the spike early (½ day).**
  - EC2 in Mumbai, with Caddy serving the page and the offer endpoint over HTTPS.
  - Open a UDP range for media.
  - Give the *server* STUN servers: Pipecat's docs say the bot otherwise gathers only host candidates, and on EC2 those are private addresses.
  - Add TURN: coturn on the box (3478 UDP/TCP, 5349 TLS) or a hosted service (cost unverified).
  - *Done when* it works from home Wi-Fi, from phone mobile data, and from one restrictive network, where `chrome://webrtc-internals` shows a relay candidate. If it fails, switch to Pipecat's WebSocket transport next.
- **V2. Gnani adapter and contract tests (1 day).**
  - `speech/gnani.py` wraps the plugin and configures it from Settings: `lang_code`, VAD headers, `x-max-speech-s`, and word boosting.
  - Replay tests built from the probe logs, for Prisma's `connected`/`processing`/`transcript`/`error` and Timbre's `start`/`audio`/`complete`.
  - *Done when* the tests pass offline and a plugin upgrade would touch only this file.
  - **[probe]** The header values come from P2, P3 and P5.
- **V3. BrainProcessor (1½ days).**
  - On each final transcript it calls `submit_segment`. On turn stop it calls `end_of_turn(deadline)`, then a new `speak_stream()` that applies the guards and yields sentences to TTS.
  - Trace marks: `vad_end`, `final_transcript_k`, `turn_end`, `actor_first_sentence`, `tts_first_byte`.
  - *Done when* an offline pipeline test with fake STT, fake TTS and the FakeLLM proves ordering, the deadline and the marks.
  - **[probe]** `move_deadline_ms` comes from P2 plus N2.
- **V4. Barge-in (1 day).**
  - Pipecat's interruption reaches `GnaniTTSService`.
  - We cancel the actor task; `llm.py` already closes the boto3 stream.
  - We record only the words actually played, and set `interrupted=true`.
  - A minimum-words threshold stops coughs from counting as barge-ins.
  - *Done when* an offline test shows the cancellation propagating, and a manual run shows `barge_in_stop_ms` below 300 ms (the PLAN_v1 metric).
  - **[probe]** A spare connection is added only if T1 fires.
- **V5. Masking filler (1 day).**
  - Pre-synthesise each persona's short fillers and the acknowledgement sentence once with Timbre, and store them as PCM.
  - If no first sentence arrives by `mask_after_ms`, play one filler, then the line, with no overlap.
  - *Done when* tests show: a slow actor gives one filler then the line, a fast actor gives none, and a failed actor never gives two fillers in a row (§5).
  - **[probe]** `mask_after_ms` comes from N1.
- **V6. Web client (1 day).** A consent notice, a headphones prompt, the form, a live transcript, one session per token, and a 10-minute hard stop (under Prisma's 15-minute cap). It collects no identifiers. *Done when* it works in desktop and Android Chrome.
- **V7. Echo-aware filtering (1 day, `FEATURE_ECHO_FILTER` off by default).**
  - Request browser echo cancellation in the audio constraints.
  - Require stronger evidence before treating speech during the board's turn as a barge-in.
  - Drop, and log, any transcript that fuzzy-matches the board line being played.
  - *Done when* the matcher has Hinglish unit tests and false barge-ins are counted in the trace.
- **V8. Production deploy (1 day).**
  - IAM role, Gnani key in SSM, systemd, a Budget alarm, a daily cap and a kill switch.
  - Watch T3 CPU credits during three concurrent sessions, since Smart Turn, Silero and Opus all run on the CPU.
  - *Done when* five full sessions in a row succeed on the three V1b networks.
- **V9. Measure and tune (1–2 days).**
  - Run ten spoken sessions and take p50/p95 from the traces.
  - Label false cut-ins using a rule written down before labelling.
  - Put the measured values into `config.py` with their source, and remove the UNVERIFIED markers they settle.

The total is about 10 working days. Cut V7 and the polish in V9 first. Never cut V1b, V3 or V4.

## 4. Day-1 probe decision rules

Run every probe from EC2 in Mumbai; use the laptop only to separate network time from model time.

The thresholds come from splitting our own p95 target (3.0 s from candidate stop to first board audio) into a budget. They are not measurements:

| Stage | Budget |
|---|---|
| Turn detection (Silero 0.2 s plus Smart Turn, under 0.1 s per Pipecat) | 0.3 s |
| Last final transcript plus listener | 1.0 s |
| Actor's first sentence | 1.2 s |
| Timbre first byte | 0.3 s |
| Transport | 0.2 s |

**Nova** (run with `--app-prompts` once it is built):
- **N1. Actor first-sentence p95.** At most 1.2 s: keep the design and set `mask_after_ms` to about 1,200. Above that, in order:
  1. Confirm actor calls show `cacheReadInputTokens > 0`. Real cards will lift the prefix past 1K tokens.
  2. Shrink the per-turn actor input.
  3. Re-run from the laptop, and if possible from Sydney, to split network time from model time.
  4. If model time dominates, the owner decides on Nova Lite or Micro for the actor, or tries Bedrock's "priority" tier. The model card lists it for Nova Pro; the Converse parameter is unverified.
- **N2. Listener total p95.**
  - At most 0.6 s: set `move_deadline_ms` to about 1,000.
  - Between 0.6 and 1.0 s: accept occasional previous-segment moves and watch `fallback_reason`.
  - Above 1.0 s: cut the listener's output, and ask the owner about Nova Micro for it.
- **N3. Throttling.** Any throttle in 100 sequential calls: file a Service Quotas increase that day.
- **N4. Tool validity.** More than 2 of 50 without a valid tool call: fix the prompt or schema before the voice loop, because a failing listener silently disables fact-checks.
- **N5. Caching with tools.** `cacheReadInputTokens` stays 0 on repeat tool calls: caching does not apply alongside `toolConfig`, or the prefix was too short. Recompute the cost per session and close the UNVERIFIED note in `llm.py`.
- **N6. Region.** Do not move EC2 to Sydney on Nova numbers alone, because the Gnani round trips would grow. The model card lists an APAC profile, `apac.amazon.nova-pro-v1:0`, whose destinations include Mumbai. Whether the Sydney-only constraint allows it is the owner's decision.

**Prisma:**
- **P1.** Any partials with `x-partials: true`: write an ADR for early listener runs behind `FEATURE_PRISMA_PARTIALS`. Otherwise finals-only stays.
- **P2. Last audio frame to final, p95.** At most 0.6 s: the budget holds. Above that: re-probe with `--min-silence-ms 300`. Still above 1.0 s: document that the last segment feeds the next turn, not the current move.
- **P3.** A 20–30 s answer arrives as one segment: lower `x-max-speech-s` (try 8). Many segments under 1 s: raise the minimum silence.
- **P4.** Verbatim mode drops fillers: drop the `filler_count` signal rather than fake it.
- **P5.** Compare `en-IN` with `hi-IN` on the same clip and pick the default. Dual decode stays gated by Ruling 2.
- **P6.** No `settings_updated` reply: do not rely on word boosting.
- **P7.** The summed `audio_duration_ms` roughly equals the clip including silence: assume every streamed second costs credits (unverified), and recompute how many sessions 4,900 credits allow.

**Timbre:**
- **T1.** Cold minus warm first byte above 300 ms: add one pre-opened spare connection per session (V4).
- **T2.** Warm first byte p95 above 500 ms: compare SSE and use the faster.
- **T3.** The second request on one socket fails: use one connection per line.
- **T4.** Poorvi is the only Timbre v2.5 voice listed as `hi-en`. The owner picks three clearly different voices by ear.

## 5. Rulings 9–14

I have no disagreements with Rulings 9 to 13. I suggest two refinements to Ruling 14 for voice mode, plus one note on Ruling 9's deadline.

1. **No double filler.** The masking filler (about 1.2 s) and the persona fallback line (the 2.5 s budget) can fire back to back: "Hmm…" followed by "Ek minute… aap apni baat jaari rakhiye" sounds broken and asks nothing. If a filler has already played, the fallback should be the move's template question instead.
2. **Fast-fail retry.** A throttle that returns within a few hundred milliseconds could be retried once while the first-token budget has room. Make this a setting that defaults to off, and decide it from N3.
3. **Ruling 9's deadline.** The 1.5 s `move_deadline_ms` from Ruling 9's implementation does not fit a 3.0 s p95 once the actor and Timbre are added (§4). It stays provisional until P2 and N2 set it.
