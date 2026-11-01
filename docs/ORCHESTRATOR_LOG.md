# Orchestrator log

The orchestrator checks every agent's output against the plan and records rulings here. Rulings bind the Builder and the CTO until the owner overrides them.

## Round 1 — Oct 25, 2026

### Ruling 1: second vote before a fact challenge (CTO conflict with delta §6)
Accepted the CTO's argument: two identical runs at temperature 0 add cost but almost no independence.
- The second vote is now the **deterministic numeric check** (value, unit, year against the card, with tolerance). A "contradicted" verdict stands only when the model verdict and the numeric check agree.
- For the MVP, fact challenges are allowed **only on numeric or date claims**. Non-numeric contradictions are logged and shown in the report, but never challenged live.
- A differently framed second prompt is a later experiment behind a feature flag, measured on the eval set.

### Ruling 2: dual-decode fusion (CTO conflict with delta §11)
Accepted with a gate. Build it behind a feature flag `DUAL_DECODE` (default off). Test on the 30-clip audio set by Nov 3. Keep it on only if it clearly lowers WER or raises claim-extraction F1 on Hinglish; otherwise present it in the submission as an honest experiment.

### Ruling 3: correction to the CTO's fact notes
- **pipecat-gnani maintenance:** Gnani's own docs (docs.gnani.ai/pipecat/introduction) say the integration is maintained by Gnani.ai; Pipecat's docs list it as a community integration that Pipecat itself does not test. Both are true. The CTO's recommendation stands: pin the version and wrap it behind our own adapter.
- **Interim transcripts:** the Pipecat plugin *can* forward interim transcripts, but Gnani's docs state that Gnani currently sends final transcripts only. Design stays final-only; the `x-partials` probe decides whether this changes.

### Ruling 4: adopted from CTO_NOTES_01 without conflict
- LangGraph: in-memory checkpointer per session; per-session queue so segment analyses run in order; every per-claim branch catches its own errors; conditional edge skips the librarian when there are no claims; node cache for librarian verdicts keyed by claim hash and card-set version; explicit `RetryPolicy` on throttling/timeout errors; no interrupts in live sessions.
- Feature flags for every innovation, default safe.
- Tracing: Langfuse Cloud free plan for LangGraph and Pipecat traces; our own per-turn trace file stays the source of truth for latency.
- Workflow additions: recorded-fixture contract tests for Gnani WebSockets, latency-budget test with fake services, Bedrock throttling chaos test, prompt-size test against Nova's caching limit, cut-line reviews on Nov 1 and Nov 5.
- Submission language: call the turn graph a deterministic multi-stage workflow with named patterns, not "autonomous agents".

### Pending owner decisions
Lead award; whether "Nova Pro in Sydney only" is an account restriction; internal-security subtopics for camera; which pre-Nov 9 product ideas from CTO_NOTES_01 §3 to adopt.

## Round 1b — Oct 25, 2026 (owner approved the CTO's product ideas, subject to cross-check)

### Ruling 5: product features after cross-check
- ADOPT application-form-driven questions (DAF-style: places lived, education, hobbies, current affairs linked to them). Generated questions must be open-ended and must not embed a fact about a place unless a fact card backs it. Form collects no names or identifiers. Fields: home state, district (optional), degree subject, hobbies (up to 3), optional subject (optional), work experience summary (optional), interview language.
- ADOPT "replay this moment" drill: replays only moments the report flagged; reuses cached board audio.
- ADOPT Hindi report first (judge output language parameter `report_language` = "en" | "hi"); full UI toggle later. Evidence: UPSC allows English, Hindi or any Eighth Schedule language for the interview; Nova Pro lists Hindi among 15 optimised languages (AWS Nova user guide).
- DOWNGRADE shareable report card to optional (Crowd Favourite counts engagement on the builder's own nominated posts).

### Ruling 6: judge rubric follows UPSC's published qualities
Judge scores 1-5 or "insufficient_evidence" for: mental_alertness, critical_assimilation, clear_logical_exposition, balance_of_judgement, variety_depth_of_interest, social_cohesion_leadership, intellectual_moral_integrity. Plus two practice indicators labelled as Mock Board's own: factual_accuracy, composure. Each score carries evidence quotes and turn ids. No accent, dialect or grammar dimension.

### Note for later (not a change): Nova Lite and Nova Micro are also served in Sydney (AWS Nova user guide). If the owner's constraint allows, Micro could serve the listener for lower latency; owner to decide.

## Round 2 — Oct 25, 2026 (evening, after two session interruptions)

- The original Builder was stopped by a session end; work on disk was kept. Remaining phase 1 split into chunks: Builder 1b (graphs, tests, CLI, eval) and Builder 2 (probes, data/cards) in parallel on non-overlapping paths.
- **Builder 2 accepted.** Orchestrator re-ran `python -m probes.selftest`: 18/18 pass offline. All six cards are `synthetic_example: true`, labelled "TEST CARD: Exampleland…", `source_url: null`. Nine protocol items marked UNVERIFIED in probes/gnani_ws.py; the day-1 probes exist to settle them.
- **Ruling 7: code paths win over ENGINEERING.md where they differ.** Fact cards live in `data/cards/*.json` (not `data/factcards/*.jsonl`). The CTO updates ENGINEERING.md in its next round.
- **Ruling 8:** numpy and scipy go into an optional `probes` extra in pyproject (Builder 1b owns pyproject). TTS language defaults to `hi-en` until a `TTS_LANGUAGE` setting is added to config.

- **Builder 1b accepted with corrections.** Orchestrator re-ran: 155/155 tests pass, ruff clean, `make cli-fake` (PYTHONPATH=src) runs a full session, report in 9 dimensions, trace written. Behaviour observed in the fake transcript that tests did not catch:
  - **Defect A (honesty penalised):** after "Mujhe exact pata nahi hai…" (an honest "I don't know"), the strategist chose `pressure`. The board must respect honest unknowns: reassure or move on, and the judge credits intellectual integrity. Add a rule and a test.
  - **Defect B (challenge wording):** the fact challenge read the card text verbatim ("hamare synthetic test card ke anusaar: TEST CARD: …") without quoting the candidate's own figure. Required shape: candidate's figure, card's figure and year, then a question about the source — e.g. "Aapne 30 percent kaha; mere paas 2023 ka figure 12 percent hai. Aapka figure kahan se hai?" Sentence case. Test it.
  - Minor: when the candidate concedes an error, prefer an acknowledgement before the next move; identical probe lines repeat in fake mode (acceptable offline, but the real actor prompt should avoid repeating the previous line).
- `python -m mockboard.cli` needs `PYTHONPATH=src` or `make install`; README must say so.

## Round 3 — Oct 25, 2026 (CTO code review, CTO_NOTES_02.md)

- **Ruling 9 (accepts CTO M4, supersedes PLAN_v2_DELTA §2's single turn graph):** split the slow brain into a **fast analysis graph** (listener → strategist, the only work allowed between end-of-turn and the actor call) and a separate **fact-check graph** (per-claim librarian fan-out + numeric gate) whose verdicts reach the strategist on the next turn. Add `submit_segment` / `end_of_turn` methods on the session for Pipecat, a deadline with a deterministic fallback move, and a latency-budget test. This is consistent with the plan's intent (fact challenges are a turn late by design).
- **Ruling 10 (accepts CTO M1, withdraws the node-cache part of Ruling 4):** replace LangGraph's node cache for librarian votes with our own vote cache that stores only real verdicts (never fallbacks), keyed by normalised-claim hash + card-set version, with a two-session test proving a throttled fallback in one session is not served to another.
- **Ruling 11 (accepts CTO refinements):** Defect B adds an optional `source_short` card field for spoken citations; figures are rendered in the card's own scale (lakh/crore vs millions); `actor.v2.md`; the guard checks the card's figure, not the card's text. Defect A's acknowledgement after a conceded error is a field on the existing move (moves stay at nine); listener emits `concedes_error`; strategist: first honest "don't know" in a topic → reassure, second → move_on; no pressure right after reassure; cap consecutive pressure.
- Order of work: M6 (git — done by orchestrator), M1, M2, M3 + Nova request fixes (`topK: 1`, stricter schema check, truncated-output handling, case-insensitive tag escaping), then M4 + M5, then docs (README, WALKTHROUGH, ADR-001…005) and `make eval-gate`.
- Note: the synthetic prompt prefix (~929 tokens) is below Nova's caching minimum; zero cache hits are expected until real cards are added.

## Round 4 — Oct 25, 2026 (fix chunk M1-M3 + Nova request fixes)

- **Accepted.** Orchestrator re-ran: 287/287 tests, lint clean, commits d014f50..ad13318 on main. Transcript check: honest "don't know" → `reassure` ("Koi baat nahi, aaram se…"); fact challenge → "Aapne 30 percent kaha. Exampleland test data ke 2023 ke figure ke hisaab se yeh 12 percent hai. Aapka figure kahan se hai?"; concession acknowledged. Defects A and B closed.
- Builder's deviations accepted: the acknowledgement is a fixed code-side sentence (always said, cacheable as audio); actor.v2 told not to repeat it.
- **Ruling 12:** when a topic's question budget is already used up, an honest "don't know" leads to `move_on` (R3b), not `reassure` — reassuring and then immediately leaving the topic reads oddly. Add an invariant test.
- Noted (fake mode only): the offline responder stacks "Achha." after the acknowledgement; the real actor prompt already forbids repeating it. No action unless it shows up with Nova.

## Round 5 — Oct 26, 2026 (M4: two-graph split)

- Previous M4 builder stopped by a session end after only a docstring sketch; a fresh builder used it as the blueprint.
- **Accepted.** Orchestrator re-ran: 308/308 tests, lint clean, commits 03587f0..e23a27f. Transcript identical to round 4; trace shows every move from the fast graph (move_wait 3-6 ms in fake mode, fallback_reason null on all turns); the fact challenge lands on the turn after the claim.
- Builder deviations accepted: ledger snapshot taken once when the turn opens (move independent of fact-check speed; a mid-turn verdict counts one turn later); `--think-s` for scripted CLI runs only; `speak()` added; `answer()` removed.
- **Ruling 13 (from builder's open point 3):** a hung listener call must not block a session's later segments for Bedrock's 30 s read timeout. Give fast-graph Bedrock calls their own short client timeouts (botocore `read_timeout`/`connect_timeout` from settings, retries capped) separate from background calls, and ensure a timed-out segment analysis is abandoned for the move (its claims may still be fact-checked if the listener result arrives). Goes into the M5 chunk.

## Round 6 — Oct 26, 2026 (M5 lanes, Ruling 13 timeouts, Ruling 12, chaos test)

- **Accepted.** Orchestrator re-ran: 325/325 tests (~17 s), lint clean, commits d456b61..d637db4; transcript identical. Chaos test: 24 seeded sessions, every session reports, no fallback vote cached, no challenge from a fallback, all fallbacks traced; it fails when the code is deliberately broken.
- Builder additions accepted: no retry jitter on the listener lane (LangGraph 1.2.17 adds up to 1 s); `stale_analyses` in the trace; no `pressure` when the listener failed.
- **Ruling 14 (from builder disagreement 2):** the actor lane gets **one attempt, no botocore retry**. If the actor call fails or exceeds its budget, the board speaks a cached, persona-matched fallback line ("Ek minute… aap apni baat jaari rakhiye" style, stored per persona as data) and the turn continues. Rationale: roughly 8 s of silence is worse than a graceful filler; a board member pausing is natural, a frozen board is not. Masking filler (plan: play a cached acknowledgement if the first sentence is not ready by ~1.5 s) arrives with the voice loop; the fallback line is its text-mode equivalent now.
- Timeouts stay UNVERIFIED until the Nova probe runs; the fast read timeout must cover a whole Converse listener call.

## Round 7 — Oct 26, 2026 (Ruling 14 + docs + eval gate) — phase 1 complete

- **Accepted.** Orchestrator re-ran: 342/342 tests, lint clean, `make eval-gate` exit 0; commits 403c41e..7056524. README (1,697 words), WALKTHROUGH (7,042 words; LangGraph concepts introduced in order; one analogy per component; commands state machine and folder), ADR-001..006.
- Builder deviations accepted: a failed actor call on a fact challenge or topic change still speaks the template (the challenge must be heard; the topic really changes); unspoken moves still count toward pressure limits (conservative); fallback lines are Hinglish only for now (add per-language lines when the language toggle lands); eval-gate recall 0.8 is a placeholder until the frozen golden dev set exists.
- Phase 1 is done. Next: owner runs the day-1 probes with real keys; CTO round 3 reviews docs accuracy and proposes the voice-loop phase.

## Round 8 — Oct 26, 2026 (CTO round 3: CTO_NOTES_03.md)

- CTO confirmed M1-M6 closed by checking code and tests. Eight doc inaccuracies found (ADR-001, ADR-002, ADR-004, ADR-005, WALKTHROUGH §1, §3.5, §3.7); ADR-003, ADR-006 and README accurate.
- **Ruling 15:** before the owner runs the paid Nova probe, `nova_probe.py` gets an `--app-prompts` mode that times the app's real listener (forced tool) and actor (streamed) requests built by the app's own builders, plus a `--region`/profile switch so the same probe runs from the laptop and from EC2 Mumbai. Prisma and Timbre probes may run now.
- **Ruling 16:** fix all eight doc inaccuracies. Most important: the last segment's listener call sits on the critical path (Prisma's final transcript arrives only after the candidate stops), so the latency budget is listener-last + actor, not actor alone. Pin the voice stack exactly in an optional `voice` extra: `pipecat-ai==1.5.0`, `pipecat-gnani==0.5.12`, and the `gnani-vachana` version it resolves; note that Pipecat's online docs describe newer releases.
- **Ruling 17 (refines Ruling 14 for voice mode):** never play a masking filler and then a persona fallback line back to back; if a filler already played, the fallback is the move's template question. Add a setting `actor_fast_throttle_retry` (default off) allowing one retry when a throttle fails within a few hundred ms; decided from probe data.
- Ruling 9's 1.5 s move deadline stays provisional; the CTO's probe decision rules in CTO_NOTES_03 §4 decide it.
- Next: voice-loop chunks V0-V9 from CTO_NOTES_03 §3 after the owner's probe results.

## Round 9 — Oct 26, 2026 (Rulings 15-17 delivered)

- **Accepted.** Orchestrator re-ran: 364/364 tests, lint clean, probe self-test 22/22; commits 5566e16..3ca94ad. `nova_probe --app-prompts` times the app's real listener and actor requests, built by the app's own builders, from laptop or EC2. Eight doc fixes applied, including the listener-last + actor latency budget.
- **Ruling 18:** one environment, not two. Base `websockets` range lowered to `>=15,<16` (pinned 15.0.1) so the pinned voice extra (pipecat-ai 1.5.0, pipecat-gnani 0.5.12, gnani-vachana 0.8.2) installs alongside. Verified by the orchestrator in a clean venv: tests and self-test pass, `pip install --dry-run -e .[voice]` resolves. ADR-001 updated.
- Open for V2 contract tests: gnani-vachana 0.8.2 is newer than the 0.7 line Gnani tested; first-sentence timer can trip on decimals like "12.5".
- Owner can now run all three day-1 probes.

## Round 10 — Oct 29, 2026 (phase 2 starts; owner adds a Debate mode)

- Owner ran the offline tests on his Mac (Homebrew Python; `make venv` added in 98900f8). Day-1 probe numbers are not in yet; chunks marked [probe] in CTO_NOTES_03 §3 keep UNVERIFIED defaults until they arrive.
- **Ruling 19 (owner request): two modes.** `upsc_interview` (current behaviour, default) and `debate`, a fun mode for non-aspirants. The CTO writes the debate design (CTO_NOTES_04.md) before any debate code. Constraints set now: the board argues the opposite side of a motion the user picks; fact-checking stays ask-never-assert; motions avoid partisan politics (no parties, leaders or elections) and stay on policy, ethics, technology, culture and light fun topics; debate gets its own rubric, not the UPSC qualities; mode is a session-level setting, so the voice loop needs no mode-specific code.
- **Ruling 20: fastest path to real-model testing.** (1) Text mode on real Nova already exists (`make cli-real`); owner can use it now. (2) Voice runs on the laptop first: browsers allow the microphone on `localhost` without HTTPS, so V0, V1 spike, V2 and V3 target a local run before any EC2 work. V1b/V8 deploy follow once the local loop works.
- Work split to avoid file conflicts: Voice builder owns `spikes/`, `src/mockboard/speech/`, `src/mockboard/voice/`, voice ADRs; the debate builder (after CTO_NOTES_04) owns mode config, strategist rules, prompts, personas, judge rubric. `session.py` changes are sequenced, not parallel.

## Round 11 — Oct 29, 2026 (debate design accepted: CTO_NOTES_04.md)

- **Ruling 21: debate mode accepted as designed**, with one correction. Scope for Nov 9 is DB0–DB7 **including DB5** (rounds, case file, Moderator intro in `session.py`): the CTO's recommendation listed "DB0–DB4, DB6, DB7", but without DB5 no debate can run. Curated motions only in the public demo; custom motions are built behind `FEATURE_CUSTOM_MOTIONS` (default off) and the screen fails closed. Additional excluded topics (religion, caste and communal conflict, named private individuals, live court cases) and the mid-debate `redirect` move are accepted. Orchestrator reviewed the 13 curated motions: none partisan.
- **Ruling 22: sequencing around `session.py`.** The debate builder runs DB0→DB7 in order and owns `session.py` until DB5 is committed. The voice builder does V0, V1, V2 meanwhile (no `session.py` changes) and starts V3 only after DB5 lands. V3 adds the two mode-agnostic hooks from CTO_NOTES_04 §7: a per-sentence `guard(move, sentence)` in `speak_stream` (no-op in UPSC mode) and per-session turn-detection parameters.
- Cut rule (CTO): if V3–V5 do not work in voice by Nov 4, debate ships text-only or is cut. Voice never waits for debate.

## Round 12 — Oct 29, 2026 (V0 + V1 spike accepted)

- **Accepted.** Orchestrator re-ran: 495 passed, 1 skipped (spike test skips without the voice extra), lint clean; commits 676c28b, 3eb7cb3, 3acab5c. Builder staged only its own paths.
- Pin change accepted: `gnani-vachana==0.7.9` (0.8.2 breaks `import pipecat_gnani`).
- **Ruling 23 (Python version):** `requires-python >= 3.12`, because the probes extra pins numpy 2.5.3 (3.12+). The owner's Mac (Homebrew 3.13/3.14) and EC2 Ubuntu 24.04 (3.12) both satisfy it.
- **Ruling 24 (STT: own the protocol):** the stock `GnaniSTTService` sends 640-byte frames and none of our VAD/`x-max-speech-s`/boost headers, contradicting Gnani's documented 1,024-byte framing. V2 builds our own Pipecat STT service in `src/mockboard/speech/` on top of the already-tested protocol code (`probes/gnani_ws.py` logic moved or imported into `speech/`, probes keep working), sending documented 1,024-byte frames at real-time cadence with our headers from Settings, plus replay contract tests. Keep the stock plugin only for TTS if it works.
- **Ruling 25 (TTS):** the Gnani SDK refuses `hi-en`, so `TTS_LANGUAGE` must change to a value the SDK and Timbre accept; V2 reads the allowed values from the installed SDK and Gnani's TTS docs, picks the one for Hinglish text, and records the source. The spike's fix for interrupted TTS still playing (1.5.0 `_disconnect` only flips a flag) moves into `speech/` with a test; we do not rely on stock interruption behaviour.
- **Ruling 26 (turn-taking for V3):** two observed hazards — the stop frame can arrive before the turn's last transcript, and a late Prisma final starts a new turn that interrupts the bot. V3 must: end a turn only after the stop frame AND the in-flight segment's transcript (bounded wait); attribute a final that arrives within a short window after turn end to the closed turn instead of starting a new one; and never let a late final trigger an interruption. Tests with recorded event orders from `spikes/echo_bot` logs.
- Owner's live run of the spike will settle: real Prisma/Timbre connection, frame size acceptance, final-transcript lateness, barge-in in a browser, localhost WebRTC without STUN, Smart Turn timing on Hinglish.

## Round 13 — Oct 30, 2026 (resume after a session end)

- Both builders were stopped by a session end. On disk: debate DB0–DB5 committed (188e7e7), working tree clean, 496 passed / 1 skipped, lint clean. Voice V2 had not started.
- **DB0–DB5 accepted.** Orchestrator ran `make cli-fake-debate`: Moderator intro, opponent opening that restates the user's point before rebutting, a concession on a strong point, closing with an honest concession, "The board will confer". The UPSC `cli-fake` board lines are byte-identical to 74c338f (md5 match).
- DB5 has landed, so `session.py` is released from the debate builder. Order now: Voice builder does V2 (speech/ only) in parallel with debate DB6–DB7 (no `session.py` edits; if DB6 needs one, it reports instead). V3 (which edits `session.py`) starts after V2 and after DB6 is committed.

## Round 14 — Oct 30, 2026 (DB6–DB7)

- **Accepted with one correction.** Orchestrator re-ran `make cli-fake-debate`: verdict, code-computed scoreboard, best line and rubric with citation-checked quotes all present; tests 589 passed / 1 skipped per builder (lint red only from the voice builder's uncommitted `speech/` file, not debate code); UPSC output byte-identical per builder.
- Builder deviations accepted: separate `DebateDimension` / `DebateReport` types (UPSC report cannot change by accident); verdict thresholds 3.5 / 2.5 marked UNVERIFIED; Hinglish debates report in English or Hindi.
- **Defect C (scoreboard reads wrong, maths is right):** the table mixes the user's counts ("arguments": the user's claim-and-warrant pairs) with the opponent's ("standing": the opponent's case arguments not conceded), with no labels saying whose is whose, so the opening row shows "3 arguments, 4 standing". The verdict sentence "You answered 3 of the opponent's 4 arguments and won 1 concession(s), so 3 of its arguments were still standing" implies answering removes an argument; only a concession does. Fix: column headers that name the side ("your points", "their points answered", "their points conceded", "their points still standing"), a one-line legend under the table, verdict wording that separates answering from conceding (e.g. "You answered 3 of the opponent's 4 points. It conceded 1, so 3 were still standing at the end."), Hindi equivalents, and a test that standing = opponent points − concessions.
- **Defect C closed** (914d186). Orchestrator re-ran: 605 passed / 2 skipped; verdict now reads "You answered 3 of the opponent's 4 points. It conceded 1, so 3 were still standing at the end."; columns name the side; legend explains that only a concession removes a point. Debate mode DB0–DB7 complete. Voice V2 still in progress (uncommitted speech/ files present).

## Round 15 — Oct 30, 2026 (V2 accepted; Gnani STT docs changed)

- **V2 accepted.** Orchestrator re-ran: 611 passed / 3 skipped, lint clean, probe self-test 22/22; commits 047d11c..f9700e2. `PrismaSTTService` (our framing and headers), `TimbreTTSService` (stock plugin + interruption fix + idle-socket fix), protocol code in `mockboard.speech.gnani_protocol`. `TTS_LANGUAGE=auto` (SDK refuses `hi-en`; Gnani docs call `auto` recommended for mixed content). The builder's one out-of-scope edit (a test asserting the old default) is accepted.
- **Orchestrator re-checked https://docs.gnani.ai/api/STT/stt-websocket.md today.** Current facts: required headers `x-api-key-id`, `language_code`; new optional `model` (`prisma-v3.0` | `prisma-v2.5`, default v2.5, unknown values silently ignored), `keyterms`, `topic`, `previous_text`, `x-idle-timeout-sec` (default 60 s, 5–900), `x-api-request-id`; renamed `format`, `bias_list`, `bias_score` (older spellings like `lang_code`, `x-format`, `x-model` still accepted; new wins); a `{"type": "commit"}` control frame closes the open utterance immediately; transcripts are always `is_final: true` (no partials documented); 15-minute cap ends with an `error` and close 1001.
- **Ruling 27 (Gnani protocol update):** switch `gnani_protocol` to the current header names (keep accepting nothing else from the old set except where Gnani requires); make `STT_MODEL` a setting (default `prisma-v2.5`, the model the contest names; `prisma-v3.0` behind the setting for an A/B on the owner's clips — the submission names whichever ships); per-topic `keyterms`/`topic` where the model supports them; set `x-idle-timeout-sec` explicitly; send `x-api-request-id` = our session id for traceability; add Prisma VAD values as Settings fields (UNVERIFIED defaults until probe data).
- **Ruling 28 (turn-taking, refines Ruling 26):** when Smart Turn ends the user's turn, V3 sends `commit` to Prisma and waits (bounded) for the resulting transcript before `end_of_turn`. This replaces waiting for Prisma's own silence window and fixes the "stop before last transcript" hazard. Late-final handling from Ruling 26 stays as a safety net.
- V3 scope expanded so the owner can talk to the real board: a runnable local voice app (`python -m mockboard.voice.server --mode upsc|debate`) on localhost, reusing the spike's page.

## Round 16 — Oct 30, 2026 (CTO round 5: CTO_NOTES_05.md)

- Orchestrator confirmed must-fix 1 in code: `modes/debate/session.py` builds the opponent's allowed figures from all topic cards (synthetic included) and from the motion text.
- **Ruling 29 (debate integrity, CTO must-fixes 1–2) → debate builder:** synthetic cards are never a source of allowed figures outside `--fake` runs; remove `card_ids` links on motions 5 and 6 until the owner authors real cards; a motion's own numbers are allowed only inside the motion's own phrase (e.g. "Class 10"), and a card figure only in a sentence that shares content words with that card. Add a rule-based off-limits check on both user and opponent text (should-fix), and make the closing concession name a real argument the user made. Tests reproducing the CTO's two leaks must fail before and pass after.
- **Ruling 30 (speech ordering, CTO must-fixes 3–4) → voice builder, inside V3:** `commit` flushes the framer and goes through the same send queue as audio, resolving when its transcript arrives (bounded wait; bounded too when no utterance is open); Timbre receives one request at a time (serialise, or one request per guarded line); surface Prisma reconnect give-up to the user; explicit idle timeout (300 s); check the served model in `connected`; require a bias score when boosting on v2.5; Timbre first-byte watchdog.
- **Ruling 31 (amends Ruling 27/28 per CTO):** `x-api-request-id` = `<session_id>-c<k>` (k = connection counter) so ids never repeat across reconnects; turn transcripts are matched to the commit that produced them, not by a time window; if the candidate resumes speaking before the board's first audio, the turn reopens. v2.5 stays default; a v3.0 A/B uses `hi-IN,en-IN` with `format=codemix` (UNVERIFIED whether the submission may name v3.0).
- File split to avoid conflicts: the debate builder owns `src/mockboard/modes/debate/**`, `data/motions/**`, `tests/test_debate*`; the voice builder must not touch those.

## Round 17 — Oct 30, 2026 (Ruling 29 delivered; both builders resumed after a usage limit)

- Both builders were stopped by a usage limit mid-edit and resumed from their saved work.
- **Ruling 29 accepted** (03a4107, 5d46504, 5ffcf90, 77b4258). Orchestrator ran the debate integrity and invariant tests (80 passed): the CTO's two leak sentences ("Tourism is 12 percent of India's GDP.", "Nearly 10 percent of students are addicted.") are now refused; the fake closing names a real user argument. Four over-general case-file claims softened; no figures, sources or party/religion/caste framing in any of the 13 motions.
- **Ruling 32 (gap the builder reported):** the user-side off-limits check sees only what the listener recorded, so a paraphrasing listener could hide a hit. The debate builder adds a minimal hook so the check also runs on the raw transcript text, touching `brain/listener.py` only as much as needed (the voice builder is not editing that file), with a test where the listener paraphrases away an off-limits word.
- Voice builder committed V3 item 1 (fbe34b8: current Prisma headers, ordered `commit`); items 2–5 in progress.
- **Ruling 32 delivered** (ddf03e2): off-limits check also runs on the raw transcript via an optional hook on the listener's output model (UPSC model has none, so UPSC is unchanged); test with a paraphrasing fake listener passes and fails when the check is disabled. Debate integrity work complete.

## Round 18 — Oct 30, 2026 (V3 + V4 core accepted: the real board on voice)

- **Accepted.** Orchestrator re-ran: 656 passed / 6 skipped without the voice extra; voice tests 111 passed in the voice venv; lint clean; UPSC `cli-fake` board lines byte-identical (md5 826ef55…). Started `mockboard.voice.server --mode upsc --fake-llm --fake-speech`: page served on localhost:7860 (HTTP 200). Commits fbe34b8, 0e213d6, cfd84b6, cde1330, 638e333.
- Builder's headless WebRTC run found and fixed three bugs (opener captions sent too early, a timer name colliding with Pipecat's `_clock`, Silero state lost mid-speech) with regression tests.
- **Ruling 33:** debate mode needs a `template_line(move)` hook so a streamed debate line cut short still ends with a proper question (builder's open note) → debate builder, small chunk.
- Next: owner's first live voice session; its `make voice-summary` output sets `MOVE_DEADLINE_MS`, Smart Turn and barge-in thresholds (V9). Then V5 masking filler, V6 web polish, V8 EC2 deploy.
- **Ruling 33 delivered** (3dc4c34): debate `template_line(move)` for every debate move in Hinglish, Hindi and English, figure-free and guard-clean; tests cover lines cut short by the number guard, the off-limits guard and a mid-line actor failure. Orchestrator re-ran: tests green, lint clean.

## Round 19 — Oct 31, 2026 (owner's first run of the voice app: free mode on his Mac)

- Owner ran `make voice-fake` on macOS (Python 3.14, Chrome, localhost). Orchestrator read the server log and the traces in his folder through the linked device. Pipeline, WebRTC on localhost without STUN, Silero, Smart Turn v3, BrainProcessor, the commit path and captions all worked: three turns, commit→move ready in under 10 ms in fake mode, no fallbacks. The beeps are the fake TTS; the "You" captions are the fake STT's scripted lines.
- **Defect D (ghost session):** two peer connections were created (the first offer at 20:44:03 never connected and timed out ~80 s later), and a full pipeline was started for it. In real mode that would open a Prisma stream (billed while streaming) and Timbre for a session nobody uses. Fix: build the pipeline and open Gnani connections only after the WebRTC connection reaches `connected`; a new offer from the same page closes the previous connection and pipeline; the Start button is disabled while connecting.
- **Defect E (native library clash):** macOS warns that `av` and `cv2` both bundle `libavdevice` ("may cause spurious casting failures and mysterious crashes"). Find which dependency pulls OpenCV; drop it if unused by our path, or document a safe pin.
- **Defect F (Stop gives no report):** pressing Stop ended the session with no report. On Stop, if at least one user turn happened, run the judge and show/save the report.
- Minor: favicon 404.

## Round 20 — Oct 31, 2026 (owner's FIRST REAL voice session: Prisma + Nova Pro + Timbre)

Orchestrator read trace voice-32a441f916 and the server log from the owner's Mac (linked device). Facts:
- Pipeline healthy: Prisma served prisma-v2.5, 2,830 frames, 0 dropped, 0 reconnects, 0 errors; Timbre first byte 0.20–0.38 s; Nova actor first sentence ~0.95–1.0 s after move ready; board audio began ~2.2 s after the candidate stopped on normal turns (inside the 3.0 s p95 target).
- **Defect G (main cause of "not smart"): commit gets no transcript.** All 8 commits returned reply "none" in 60–90 ms. Prisma's own finals arrive ~1.3 s after speech ends (STT TTFB 1.28–1.76 s). So turns closed before the words arrived: "no words were recognised" twice, `transcripts_dropped: 2`, then the late words appeared as separate bubbles. Ruling 28's assumption (commit returns the open utterance) does not hold in practice.
- **Defect H (transcription accuracy):** `en-IN` Latin output mangled Hinglish (e.g. "no samajhata hoon ki hindi mein baalon ko rehna vahaan pe asaan nahi hota"; "kuchh badh mein" for "kuch baad mein"). The listener then judged turns garbled → R1 clarify twice, R7 pressure on what was an honest deflection.
- **Defect I (synthetic cards leaked into a real run):** `cards_all_synthetic: true`; the move-on line asked "Exampleland mein tourism ka GDP share kya hai?" — a quiz question built from a TEST CARD. Ruling 29 gated synthetic cards for debate only.
- **Defect J (board quotes ASR errors back):** clarify/pressure lines repeated the garbled transcript verbatim ("Aapne kuchh badh mein kaha…").
- Voice judged "robotic" by the owner: Timbre via plugin, voice Poorvi, `TTS_LANGUAGE=auto` on Latin-script Hinglish, one request per sentence at 16 kHz.

### Rulings
- **Ruling 34 (turn-taking, supersedes Ruling 28's reliance on commit):** after the turn stops, wait for Prisma's final for the in-flight speech, bounded by a measured budget (start 1.8 s; STT TTFB measured 1.3 s), counting a "none" commit reply as "pending", not "empty". Lower `x-min-silence-ms` to 300 and measure. "No words recognised" is shown only if nothing arrives within the budget. Keep commit (harmless) but never rely on it. Late finals inside the budget join the turn; regression test built from this trace's event order.
- **Ruling 35 (STT language A/B):** add an opt-in local recording of the candidate's own audio (`--record-audio`, off by default, owner's consent, saved only under traces/) and an offline A/B tool that replays a recording through Prisma with `en-IN`, `hi-IN`, and `prisma-v3.0` + `hi-IN,en-IN` + `format=codemix`, printing the transcripts side by side so the owner picks by reading. Listener must accept Devanagari or Latin input equally.
- **Ruling 36 (no test data in real runs, UPSC side):** synthetic cards never enter any prompt (opener, actor, listener, librarian) outside `--fake-llm`; with no real cards, UPSC runs without fact checks and the report says so. Topic questions are always open-ended; the actor never asks the candidate a quiz-style number question.
- **Ruling 37 (never quote ASR errors):** clarify/pressure/probe lines never repeat the candidate's transcript verbatim; clarify asks generically ("Maaf kijiye, main theek se sun nahi paaya. Kya aap phir se bata sakte hain?"). A garbled transcript that contains an honest-unknown marker in either script ("nahi pata", "baad mein", "नहीं पता") goes to reassure/move-on, never pressure.
- **Ruling 38 (voice quality levers, owner picks by ear):** check Gnani's TTS docs for Timbre v3.0 (Gnani's site now announces "Timbre v3.0: Expressive text-to-speech") and whether the API exposes it; build a local "voice lab" page that renders the same three board lines with each available model, a few voices, `auto` vs `hi-IN`, Latin vs Devanagari text, whole-line vs per-sentence requests, and 16 vs 24 kHz output, so the owner chooses. Default to one request per whole guarded line if prosody improves.

## Round 21 — Oct 31, 2026 (Rulings 36–37 and Defects D, E delivered)

- **Accepted** (1654cd3, 5db42c9, 784ad7d, 82111ec). Orchestrator re-ran: 717 passed / 7 skipped; UPSC `cli-fake` board lines byte-identical (md5 826ef55…); only `prompt_versions` changed (listener 3, actor 3).
- Ruling 36: synthetic cards reach prompts only on the fake LLM, in both modes; with no real cards there is no librarian call and no fact challenge, and report/CLI say fact checks are off; actor v3 asks open questions only, with an in-stream guard that stops quiz-number questions and quoted candidate words; clarify is a fixed line in three languages.
- Ruling 37: honest-unknown markers checked on the raw transcript in both scripts; Devanagari supported in word counts, numbers ("१५ लाख"), the garbled check and listener prompts.
- Builder choices accepted: "baad mein" counts as a deflection only in short answers or "I'll tell you later" phrasing; the eval runners keep the synthetic cards (evals are not user-facing and stay labelled synthetic).
- Defect D fixed (no pipeline for a connection that never connects); Defect E fixed (OpenCV removed from the voice extra). The voice builder's `test_cv2_shim` is reported flaky in a subprocess; the voice builder fixes it in its current chunk.

## Round 22 — Nov 1, 2026 (Rulings 34, 35, 38 and Defect F delivered)

- **Accepted** (2054d23, 9f98ea1, 2d1aa75, 6461022, c91c7c1, da4d917, 58480d1). Orchestrator re-ran: 722 passed / 9 skipped, lint clean, UPSC `cli-fake` byte-identical (md5 826ef55…). Voice builder reports 141 voice tests passing in its venv. Timbre v3.0 is available through the API as `model: "timbre-v3.0"` (13 voices, all 10 languages; sources: Gnani TTS websocket/SSE/voice-catalog docs, read Oct 31); the SDK refuses it, so our service builds the request itself.
- Builder kept `TTS_REQUEST_UNIT=sentence` as default (a whole line waits for the full actor text and may push first audio past 3 s); `first_rest` is available as a middle option.
- **Ruling 39:** TTS defaults (model, voice, language, script, request unit, sample rate) and STT language mode are decided by the owner's ears and eyes, from `make voice-lab` and `make stt-ab` on his own voice, then set as defaults with the evidence recorded here. If `hi-en` wins in the lab, Ruling 25 is revisited then. Whether the board writes Devanagari or Latin follows the same decision (brain prompts change only after it).

## Round 23 — Nov 1, 2026 (owner approves the Panel phase; work moves to Claude Code on the owner's Mac)

- Owner's real session: the board returned to tourism after he asked to change topic. Orchestrator confirmed the causes in code: topics hard-coded in `config.py`; no candidate-request understanding; actor sees only the last Q/A; no panel structure. Owner approved the Panel phase (docs/tasks/PANEL_PHASE.md): a LangGraph interview-director graph, form-driven agenda with Chairman + 4 domain members and spoken hand-offs, candidate-request handling, conversation memory, UPSC ritual, plus "likely questions from your form" and a report by panel member.
- **Ruling 40:** development continues in Claude Code on the owner's Mac, where real Gnani and Bedrock calls work. `CLAUDE.md` holds the project memory; `.claude/agents/builder.md` and `.claude/agents/cto-reviewer.md` define the two agents; the main session orchestrates and verifies, and the CTO reviews every chunk before acceptance.

## Round 24 — Nov 1, 2026 (CTO_NOTES_06 received; design rulings; web work split out)

- CTO design for the Panel phase is in `docs/CTO_NOTES_06.md`. No real calls were made. The CTO reports 821 tests passing with the voice extra.
- **Ruling 41 (owner's rule):** no real Gnani or Bedrock call that costs credits without the owner's approval. This covers subagents and overrides the CLAUDE.md line allowing direct real-service checks. Free targets stay allowed (CTO_NOTES_06 section 0).
- **Ruling 42:** D1, D2, D3, D6, D7 and D8 of CTO_NOTES_06 are accepted as designed. The director's per-turn graph stays model-free, and the agenda has a deterministic fallback. D4 (20-minute limit) and D5 (replay audio) stay open for the owner; the build keeps the current limit and a text-only replay until the owner decides. D9 (CLAUDE.md wording) and D10 (stale test count) wait for the owner's agreement to edit CLAUDE.md.
- **Ruling 43 (owner's split):** the website and access limits move to a second agent on arena.ai, on branch `web-frontend`, under `docs/tasks/WEB_HANDOFF.md`. Agent work stays on `panel-phase`. Nothing merges to `main` without orchestrator review. The per-interview answer cap is wired on the panel side when requested through `docs/tasks/WEB_REQUESTS.md`.

## Round 25 — Nov 1, 2026 (P0 accepted; P0.1 required before P1)

- **Accepted** P0 (e8a933e, 724fea7, 2ea6558); review in `docs/CTO_NOTES_07.md`. Orchestrator re-ran on panel-phase at 2ea6558: `make test` 840 passed, `make lint` exit 0, UPSC board-line md5 826ef550..., debate f2a23f33..., and read the `--form-file` fake opener. It uses the form. No real calls.
- The builder's reported lint slip on e8a933e did not reproduce (CTO: exit 0 on an export of each commit). Builders report real exit codes (`set -o pipefail`).
- **Ruling 44:** P0.1 comes before P1. Must-fix: the debate lock hashes the ordered fake-LLM calls of the scripted debate, because per-call user messages and `max_tokens` currently escape it (five mutations passed). Should-fixes included in the same chunk: freeze lock cards so real cards do not break debate tests; `load_form_file` refuses more than 3 hobbies (leave `_form_values`); assert the real-path prefix; answers-only example script.
- **Ruling 45:** commit trailer is the CLAUDE.md one, `Co-Authored-By: Claude <noreply@anthropic.com>`. The orchestrator's brief gave a different line; that was the orchestrator's error. History is not rewritten.

## Round 26 — Nov 1, 2026 (P0.1 accepted; P0.2 required before P4)

- **Accepted** P0.1 (d83def0, c003187, fb85f16); review in `docs/CTO_NOTES_08.md`. M1 is closed: all five mutations are caught. Orchestrator re-ran at fb85f16: `make test` 851 passed, `make lint` exit 0, UPSC and form-file sessions md5 826ef550..., debate f2a23f33..., and a four-hobby form is refused.
- **Ruling 46:** P1 to P3 may proceed. P0.2 (tests only) must land before P4: fingerprint tool name and temperature, because wiring debate to the UPSC listener tool passes all 851 tests but would lose debate fields on real Nova; add two test-only scripts in `tests/data/` reaching rebut, cross-question and fact_challenge, with no golden change. Should-fixes in the same chunk: the `"dd"` sentinel that fails on temp paths containing "dd", and pinning the `stream_line` cap.
- `fake_panel.txt` must not have its md5 updated just to pass in P4. Python 3.12 is UNVERIFIED (not runnable offline here; 3.14 only). Check the Python version when deploying to EC2.

## Round 27 — Nov 1, 2026 (owner decides session length, replay and test-user limits)

- **Ruling 47 (owner, resolves D4):** the interview limit is 17 minutes, not 20. Rotation at 12-14 minutes then runs live for the first time, so the paid real session in P12 must exceed it. The close is a spoken wrap-up, not the abrupt hard stop.
- **Ruling 48 (owner, resolves D5, amends Ruling 5):** replay is text-only now, built behind a pluggable interface so audio replay can be added at any time without changing callers. No audio cache is built.
- **Ruling 49 (owner, amends Ruling 43's defaults):** test users get 10 candidate messages per interview, 5 interviews and the daily cap. The owner and reviewers, via access code, have no limit. Limits are shown up front and explained politely and truthfully. The cap is enforced on the agent side: the Chairperson closes courteously at the cap, never mid-line. Consequence: a capped interview cannot reach all five segments at the design's 15 answers, so the panel must shorten the agenda or budgets for a capped session, so that it still reaches a proper close. The CTO designs this before P4.
- D9 and D10 are done: CLAUDE.md and `.claude/agents/builder.md` now carry Ruling 41, and the stale test count is corrected, with the owner's approval.

## Round 28 — Nov 1, 2026 (P1 rejected; P1.1 required)

- **Rejected** P1 (613c162); review in `docs/CTO_NOTES_09.md`. Orchestrator reproduced the failures: "public administration ke alawa kisi aur topic pe baat karte hain" returns None; "parliament ka session band karo" and "campus placements mein hum interview khatam karte hain" return end; "corruption ko chhodiye asli samasya gareebi hai" returns change_topic and bans corruption; "agla sawal puch lijiye" returns repeat. The builder's 913 tests, lint and md5 claims were confirmed; the code is unwired, so no user impact.
- **Ruling 50:** P1.1 fixes M1 to M5 and the note's should-fixes. Priority order: end > strong change > skip > weak change > repeat > clarify.
- **Ruling 51 (amends A1 of PANEL_PHASE.md):** "every time" applies only to listed request sentences in tests. Any public wording uses the CTO_NOTES_09 section 4 text until P12 gives a real figure. The session report lists each detected request and its source.
- **Ruling 52 (reverses D7's "no confirmation"):** `end` gets a one-line spoken confirmation; only a clear yes closes, anything else resumes uncounted. Built in P3 with `pending_end`.
- **Ruling 53:** a healthy listener may veto weak-pattern hits on turns over 8 words, traced as `request_disagree`. Built in P5. Strong patterns and listener failure are unchanged.
- **Ruling 54:** the phrase that always works is taught as a hint on the form screen, not a spoken ritual line, until the owner hears both. The orchestrator writes a third holdout, kept out of the repo until P1.1 is scored, so no builder sees it. Real recall is measured once at P12 with owner approval.

## Round 29 — Nov 1, 2026 (P1.1 accepted; P1.2 closes the detector)

- **Accepted** P1.1 (8625bd9 to f4f7aca); review in `docs/CTO_NOTES_10.md`. Orchestrator re-ran at f4f7aca: `make test` 1069 passed, `make lint` exit 0, UPSC and form-file md5 826ef550..., debate f2a23f33.... The CTO confirmed the new tests are real (46 loop, 110 hand-written; 12 of 15 mutations caught). The orchestrator reproduced F1 and F2 below.
- Third holdout (Ruling 54): 28/33 requests caught, 0/15 negatives fired; change_topic 9/12. This is the last synthetic figure and is now spent. No fourth set.
- **Ruling 55:** P1.2 fixes F1, F2 and F3 and the CTO_NOTES_10 table items (section 8 items 2, 3 thank-you, 6, 7; the verb-tail gap; "what exactly"; "prefer a different topic"). Item 4 goes to P5. `sorry`, `kya/na`, "tourism nahi" and the section 6 low lines are waived. Dev `end` must be 21/21.
- **Ruling 56 (amends Ruling 53):** the listener veto covers seven weak patterns, adding `change_kuch_baat`. P5 uses a shared word count.
- **Ruling 57:** after P1.2 the detector is frozen until P12's real transcripts. Next: P2, P3, then P0.2 before P4.

## Round 30 — Nov 1, 2026 (P1.2 rejected on one defect; F4)

- **Rejected** P1.2 (5d43ca6 to d47a2cf) on F4 only; review in `docs/CTO_NOTES_11.md`. All other claims were confirmed by the CTO: 1194 tests, lint, md5s, F1 to F3, dev `end` 21/21. All five judgement calls are accepted; `end_en_modal` accepting `here|now` holds only with `pending_end` (Ruling 52).
- **Ruling 58:** F4: `cheez` in `change_other_topic` moves to a strong pattern requiring an imperative, using the exact code in CTO_NOTES_11 section 3, plus one test pinning `change_kuch_baat` in `_MUST_CLOSE`. The CTO has specified and trialled the fix, so no further CTO round is needed. The orchestrator confirms the diff matches and re-runs the checks. The freeze of Ruling 57 starts after F4.

## Round 31 — Nov 1, 2026 (P1 closed; detector frozen)

- **Accepted** F4 (bd44810), and with it P1.2. The orchestrator compared the diff with CTO_NOTES_11 section 3 and re-ran it: `make test` 1203 passed, `make lint` exit 0, UPSC and form-file md5 826ef550..., debate f2a23f33.... No separate CTO round, per Ruling 58. The detector is frozen until P12 (Ruling 57). Known cost, accepted: "kisi aur cheez par chalte hain" returns none.
- Standing synthetic figures: 28/33 on the orchestrator's holdout before P1.2's changes; recall on real speech is not measured.
- Next: P2, P3, then P0.2 before P4.

## Round 32 — Nov 1, 2026 (P2 accepted; P2.1 required before P4; capped interview designed)

- **Accepted** P2 (d113e7e, 469a254, 40567c2); review and design in `docs/CTO_NOTES_12.md`. Orchestrator re-ran at 40567c2: `make test` 1427 passed, `make lint` exit 0, UPSC and form-file md5 826ef550..., debate f2a23f33.... It read the sample agenda: formal, form-based, open-ended, no invented fact. d113e7e alone fails one test (the only red commit; history not rewritten).
- **Ruling 59:** P2.1 fixes M1 (undotted names, because "Smt." splits hand-offs into fragments), M2 (seeds that dangle after a skip), M3 (`capped_budgets`, budgets summing to the cap: 1/2/2/2/3 at 10), plus section 4's cheap should-fixes.
- **Ruling 60 (commit discipline):** a versioned pre-commit hook tests the index, `--no-verify` is banned, and `make verify-range` checks each commit. It goes in with P2.1.
- **Ruling 61 (provisional, owner to confirm):** "10 messages" means 10 answers; repeat, clarify and silence are free, up to 4 (`PANEL_FREE_TURNS`, 0 = strict). The cap comes only from server admission and fails closed; uncapped sessions get 15. New page events `answers` and `closing`. Closes cannot be interrupted.
- **Ruling 62:** section 12 items 2 to 5 are accepted: garbled clarify and silence use no budget; `must_ask` sits inside the budget; wrap-up at 960 s and hard stop at 1020 s; Nova writes no `current_affairs` seeds, and `hi` keeps templates. P3 builds section 9.4.

## Round 33 — Nov 1, 2026 (Arena's web branch rejected on a small fix list)

- **Rejected** `web-frontend` (852a7d1 to e16a665); review in `docs/CTO_NOTES_13.md`. The structure and design stay. Fix list in `docs/tasks/WEB_FIX_01.md`. Live attacks on fake servers held (forged cookies, clearing, parallel commits, daily cap, lockout, blank secret, traversal). M1: limits silently stayed off with settings in `.env`. Not verified: browser rendering; the orchestrator has no headless browser on this Mac.
- **Ruling 63:** S2: IP hash gets its own limit, `ACCESS_IP_SESSIONS`, default 15. S3: `POST /api/access` is allowed as a second route.
- **Ruling 64:** panel side takes the `WEB_REQUESTS.md` items: `work_experience` now, `members` on `session`, `segment` on captions, report `members` and `practice_plan`. Likely-questions spends Nova credits, so it goes through admission. `answer_cap` is declined as superseded by `closing`.
- **Deployment gate (V8):** no STUN or TURN exists, so WebRTC will not connect from real networks on EC2 until added. CTO_NOTES_13 section 8 lists the checks. Limits are not public until they pass.

## Round 34 — Nov 1, 2026 (PR #1 and the presence ring)

- PR #1 (`web-frontend` into `panel-phase`) is open at 2770c84 and conflicting. The orchestrator confirmed the head and read `presence.js`: mint and violet, a radial glow, up to 2,800 points per frame. CTO_NOTES_13's review covers e16a665 only; 2770c84 was read, not run.
- **Ruling 65:** the ring leaves the interview room. On the home page it stays only in the warm accent with no glow. This is M4 of `docs/tasks/WEB_FIX_01.md`; the owner may overrule it, since design is his call. PR #1 merges into `panel-phase` before P7, which edits the same `server.py` lines; nothing merges until M1 to M4 pass review.

- **Ruling 65 on hold:** the CTO subagent reports that the owner wants the ring kept and the other problems fixed later. That reached the orchestrator second-hand, so M4 is paused, not reversed, until the owner confirms. M1 stays required before any public deployment, and M3 before submission (contest honesty rule).

## Round 35 — Nov 1, 2026 (P2.1 accepted; P3 starts)

- **Accepted** P2.1 (effefa9, bc800c1, cadca61, 9a09569); review in `docs/CTO_NOTES_14.md`. Orchestrator re-ran: `make verify-range BASE=b3e9c4a` exit 0 on every commit (1685 passed at the tip), `make lint` exit 0, UPSC and form-file md5 826ef550..., debate f2a23f33..., `capped_budgets(10)` is 1/2/2/2/3.
- **Ruling 66 (narrow exception to Ruling 52):** if an `end` turn would spend the last budget unit with no free turn left, close at once with `end_request` and no confirmation, since nothing is left to resume. Elsewhere the confirmation stands.
- **Ruling 67:** the hook gains `pre-merge-commit` and a voice-extra check, and may skip tests for commits touching only an explicit path allowlist of docs (never `*.md`). Builders never use `--no-verify`; that is in every brief until the owner agrees to add it to `.claude/agents/builder.md`.
- **Ruling 68:** suite time is 88% voice waiting. Parameters are not trimmed. One time-boxed voice teardown chunk may follow P3.

## Round 36 — Nov 1, 2026 (owner: keep the ring, merge, finish; credits are short)

- **Ruling 69 (owner, direct, supersedes Ruling 65):** the owner approved Arena's work including the ring effects. M4 is closed. Fixes M1 to M3 stay required for the reasons already logged, and are made by our builder, since Arena is finished.
- **Ruling 70 (owner told the orchestrator to run this and said credits are being overspent):** work moves to larger chunks with one CTO review per milestone, not per chunk: merge plus web fixes; P0.2 plus P4 to P6; P7; P8 to P11. The orchestrator still runs tests, a fake session and logs a ruling at each. Reviews are narrow, with no mutation sweeps or tuning rounds, and cut order in CTO_NOTES_06 section 14 applies early: `summary.v1`, page buttons for Utility A and replay, then P8 if needed.
- Ruling 41 stands: real calls still need the owner's explicit yes.

## Round 37 — Nov 1, 2026 (P3 fix and web merge as one milestone)

- P3 (b8feacb, e585ef8, 50f3411) reviewed in `docs/CTO_NOTES_15.md`: rejected on M1 (`is_clear_yes` closes on a question) and M2 (unguarded `adelete_thread` can lose the report), with fixes specified and trialled; builder's calls upheld. CTO confirmed 1862 cases, lint, verify-range, md5s.
- **Ruling 71:** one milestone: P3 M1, M2, S1, S2 with tests; then `github/web-frontend` merges into `panel-phase` with `--no-ff` and `docs/tasks/WEB_FIX_01.md` is applied there by our builder, with `work_experience` added. Arena's branch itself is not changed. No further CTO round on the P3 fix; the merged web fixes get one narrow review.

## Round 38 — Nov 1, 2026 (site merged into panel-phase)

- **Accepted** the milestone (db47110, 2e09963, 34dc9e6). Orchestrator re-ran: lint clean, UPSC and debate md5s unchanged, `make test` on the merged tree (recorded in this round's commit message), and confirmed M1 directly: `ACCESS_ENABLED=true` in a `.env` file turns limits on, and an unrecognised value is refused. Builder showed both M2 mutations fail.
- The orchestrator rewrote the last section of `WEB_REQUESTS.md`. PR #1 is merged by this push; Arena's branch is unchanged.
- Not verified: no browser has opened the page since the fixes, and the server emits neither `answers` nor `closing` until P7. Accepted builder choices: IP limit is lifetime with reason `ip_full`, reviewer cookie 30 days, `MAX_OFFERS_PER_IP = 4`, `python-dotenv` direct dependency.
- **Ruling 72:** the narrow web review of Ruling 71 moves into the P7 milestone review, when the site is connected to the panel, to save credits. Next milestone: P0.2 + P4.

