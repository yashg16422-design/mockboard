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
