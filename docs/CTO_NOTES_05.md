# CTO Notes 05: Review of V2 Speech Services and Debate Mode (Oct 30, 2026)

**Scope and method.** This is a read-only review at HEAD `046441e`; no code was edited.
- **Commands run.** I ran `tests/voice` in the voice venv (40 passed) and probed the debate number guard with small offline calls. I also searched every curated case-file argument for numbers and for partisan, religious or caste terms.
- **Sources.** I compared the code with Gnani's current STT and TTS WebSocket pages (fetched today) and with the installed `pipecat-ai` 1.5.0, `pipecat-gnani` 0.5.12 and `gnani-vachana` 0.7.9 sources.
- **Labels.** "(unverified)" marks anything that neither the docs nor the code settle.

## 1. What is good

- **`PrismaSTTService` is careful work.** It does the following:
  - It re-frames Pipecat's 640-byte input into exact 1,024-byte frames.
  - It paces them on absolute 32 ms deadlines, so jitter does not add up into drift.
  - It bounds the backlog while disconnected and counts the frames it drops.
  - It treats a single-utterance `error` as non-fatal, as the docs say.
  - It refuses to retry a 401 or a 1008, rotates before the 15-minute cap (preferring a pause between turns), and keeps the retiring socket open long enough for its last transcript.
  - It redacts the key in every log line, and a test proves this.
- **`processing` events are passed on in order** with the transcripts, which gives V3 exactly what Ruling 26 needs.
- **The Timbre interruption fix is correct for Pipecat 1.5.0.** I confirmed in the source that `InterruptibleTTSService._handle_interruption` calls `_disconnect()` and `_connect()`, which only flip flags, while the plugin's real close is `_disconnect_websocket()`. The audio gate is a sensible belt-and-braces, and the idle-socket check fixes a real flaw: the plugin's receive loop ends quietly on a clean server close but keeps the dead socket.
- **The debate integrity work is real.**
  - All 13 curated case files are qualitative. My scan found no digits or statistics, and no party, religion, caste or communal framing.
  - The motion screen fails closed on every path: feature off, a rule hit, a classifier exception, and a reframe whose rewording fails the rules.
  - The verdict is a template that quotes citation-checked rationales and refuses to quote a rationale that takes sides.
  - The scoreboard is computed by code from the trace, and a test shows the trace and the live session agree.
  - Defect C is closed properly.

## 2. Must-fix

**M1. Synthetic cards authorise figures in a real debate.**
- *Where:* `modes/debate/session.py` lines 138–141 build `self.cards` and `self.allowed` from the motion's card topics without excluding `synthetic_example` cards. `guards.allowed_figures` then admits their values. Motions 5 and 6 cite those cards through `card_ids` in `curated.json`.
- *Why:* I checked this directly. For motion 5, the sentence "Tourism is 12 percent of India's GDP." passes the guard, because 12 is the *Exampleland* test card's value. In a real-model debate, the opponent can state an invented figure about India, which is exactly what ruling 21 and ADR-003 forbid. Synthetic cards also have no `source_short`, so the line would carry no source at all.
- *Also wrong:* the card links do not support the arguments they are attached to:
  - a national GDP share is used for hill-town income;
  - "affected districts" is used for "too far from any station";
  - an incident decline is used as proof that prevention works.
- *Fix:*
  1. Exclude synthetic cards from `cards` and `allowed` unless the session runs on the fake LLM. Use a setting such as `ALLOW_SYNTHETIC_CARDS`, defaulting to off outside tests.
  2. Remove the `card_ids` from motions 5 and 6 until the owner writes real cards, and have the owner confirm that each real card actually supports the argument it is attached to.
  3. Add a test showing that, in real mode, a sentence with a synthetic card's figure is dropped.

**M2. The number guard accepts figures out of context.**
- *Where:* `guards.allowed_figures` and `sentence_allowed`.
- *Why:* I checked two cases.
  - The guard accepts the digits written in the motion in *any* sentence. For motion 1, "Nearly 10 percent of students are addicted." passes, because "Class 10" puts 10 on the allowed list.
  - A card's value passes whatever it is attached to. "Hill towns got 42 lakh visitors in 2022" would pass with a national foreign-arrivals card.
- *Fix:*
  1. A figure taken from the motion is allowed only inside the motion's own phrase (for example "Class 10"), and never next to a number word, a percent sign or a scale word.
  2. A card figure is allowed only in a sentence that shares at least two content words with that card's statement or aliases.
  3. Add tests for both cases above.

**M3. A `commit` must be ordered behind the audio already queued (needed before V3 relies on Ruling 28).**
- *Where:* `PrismaSTTService`. Today `rotate()` sends `commit` straight onto the socket, and V3 will need a public `commit()`.
- *Why:* Audio waits in `_queue` and is sent at real-time pace. After a reconnect, up to `max_backlog_s` (2 s) of the candidate's speech can still be queued. A commit sent directly overtakes it, so the tail of the answer either becomes a new utterance or is dropped as shorter than `x-min-segment-ms`.
- *Fix:*
  1. `commit()` first flushes the `FrameBuffer` remainder, then puts a commit marker into the same queue.
  2. The sender sends that marker only after every audio frame queued before it.
  3. `commit()` returns an awaitable that resolves when the transcript for that utterance arrives, or when a bounded timeout expires.
  4. Add a test with a backlog.

**M4. Timbre can receive overlapping requests (needed before V3 streams sentence by sentence, per ADR-008).**
- *Where:* `TimbreTTSService.run_tts`, which inherits the plugin's behaviour.
- *Why:* The plugin's `run_tts` sends the request and returns immediately, and Pipecat 1.5.0 aggregates text by sentence by default. So sentence 2's request can go out while sentence 1 is still streaming. Timbre's docs say "Send one synthesis request at a time"; what the server does otherwise is unverified. Three things go wrong:
  - The plugin resets its PCM aligner on every request, which can cut sentence 1 in the middle of a sample.
  - The plugin sets `_bot_speaking = False` on the first `complete`, so a barge-in after that point skips the reconnect.
  - The next line's `run_tts` reopens our audio gate, so stale audio from the interrupted request can then play.
- *Fix:* Serialise requests in `TimbreTTSService`: `run_tts` waits, with a bound, for the previous request's `complete` or `error` before sending. Alternatively, V3 sends each guarded line of at most 30 words as one request. Either way, add a test with two sentences and a barge-in between them.

## 3. Should-fix

1. **A reconnect that gives up is silent** (`prisma_stt._reconnect_loop`). After five attempts (about 7.75 s of backoff) the service marks itself fatal, but it pushes only a *non-fatal* `ErrorFrame`, which the 1.5.0 `PipelineWorker` just logs. The candidate would keep talking into a dead microphone. V3 should catch `on_connection_error`, tell the user on screen, and either end the session cleanly or keep retrying slowly while the session is alive.
2. **Idle timeout.** The docs say the session closes after 60 s with "no speech detected", whether or not audio keeps arriving, and a candidate thinking or a long board turn can hit that. Set `x-idle-timeout-sec` (Ruling 27), for example to 300. Record an idle close as a normal event rather than an error. Add a fake-server test.
3. **Check which model actually served.** The docs say an unknown `model` value is *silently ignored*. Prisma v3.0 adds `request_id` and `config.language_code` to `connected`, so when v3.0 was requested and those fields are missing, log a loud warning. Put the model that actually served into the trace, because the submission must name it.
4. **Boosting is silently off** (`STTConfig` and `update_bias`). The docs say `bias_list` requires `bias_score` on v2.5, and that an omitted score disables boosting; v3.0 uses `keyterms` instead. Require a score with any list on v2.5, and send `keyterms` on v3.0.
5. **Time-to-first-byte watchdog for Timbre.** A half-open socket passes the `State.OPEN` check, and the line then never plays. V3 should, if no audio arrives within a bounded time after a request, reconnect and resend once, then fall back to on-screen text.
6. **A deterministic off-limits backstop for debate.** D2 depends only on the listener model's `off_limits` flag, and the opponent has no partisan guard at all. Run the screen's `RULES` patterns over every user segment (which forces `redirect`) and over every opponent sentence (drop the sentence and log it).
7. **Concession honesty** (`actor.concession_text`). The generic "you made some very good points" praises the user whatever they said. "Your answer on X was strong" claims a strength the listener never judged: the listener only tagged the point as answered. Instead, concede the strongest argument *for the user's side* from the case file, which already holds both sides: "I accept there is real force in …". That is honest without inventing praise.
8. **Scoreboard legend.** The "answered" and "arguments made" counts come from the listener model's tags, which code then counts. The legend should say exactly that.
9. **Motion screen** (behind a flag that is off). Re-run the classifier on a reframed motion and require `allow`. Add Devanagari terms to the blocklist.
10. **Case-file review.** `curated.json` says the case files "await the owner's review". The owner should review them before the demo. Soften universal factual claims, for example #12 A1 "made in almost every home".
11. **Secret hygiene.** `spikes/echo_bot/bot.py` `EchoConfig.api_key` is a plain dataclass field, so the key would appear in its `repr`. Use `field(repr=False)` there, and keep the same rule in V3's server config.

## 4. Rulings 27 and 28

**Ruling 27. I agree, with one change.**
- **Request id (change).** Sending `x-api-request-id` equal to the session id would repeat the same id on every reconnect and rotation. Gnani's `segment_id` is `<request_id>-seg-<N>`, with N starting at 1 on each connection, so segment ids would collide, and V3 attributes transcripts by them. Use `<session_id>-c<k>` per connection.
- **Default model.** `prisma-v2.5` as the default is right: it is the model the contest names.
- **The v3.0 arm of the A/B.** It should be `language_code="hi-IN,en-IN"` with `format=codemix`. The docs list both as v3.0 features, and together they are the native form of Ruling 2's dual-decode idea at no extra Prisma cost. Whether the contest accepts naming v3.0 in the submission is unverified, so check the contest page before shipping it.

**Ruling 28 (commit after Smart Turn). I agree, with four conditions V3 must meet.**
1. **Ordered.** The commit goes through the audio queue (M3).
2. **Attribution by cause, not by time window.** Snapshot `pending_segments` when committing, and attribute to the closed turn exactly the transcripts that the commit and the in-flight segments produce. Ruling 26's time window would wrongly attribute a candidate's resumed speech to the turn that just closed.
3. **Reopening a false end of turn.** If the voice detector hears speech again before the board's first audio, for example within about 500 ms of the commit, reopen the turn:
   - cancel the actor call;
   - keep the segments already heard;
   - end the turn again later.
   Without this, every pause in the middle of a thought becomes a board interruption, and Smart Turn's accuracy on Hinglish is still unmeasured.
4. **Commit with nothing open.** Gnani does not document what `commit` does when no utterance is open: nothing, an empty transcript, or an `error`. Bound the wait (around 300 ms), treat an empty transcript as nothing, and test all three replies.
