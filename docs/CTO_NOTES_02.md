# CTO Notes 02: Review of the Phase-1 Code (Oct 25, 2026, evening)

**Scope.** I reviewed everything under `src/mockboard/`, `tests/`, `eval/`, `probes/` and `data/cards/`, plus the `Makefile` and `pyproject.toml`, against the checklist in CTO_NOTES_01 §7 and the orchestrator's rulings 1 to 8. I did not edit any code.

**What I ran.**
- `make test` passes 155 out of 155 tests.
- `make lint` is clean, and so is `ruff check probes`.
- `make cli-fake` runs a full session and writes a report and a trace.
- `python -m probes.selftest` passes 18 out of 18 checks.
- `make eval` runs on the three example items.

I also ran small offline experiments with the fake LLM. They lived in my scratchpad and touched nothing in the repo. Their results are quoted where they matter.

**Doc claims.** The claims about AWS behaviour below come from the Bedrock prompt-caching guide and from the Amazon Nova "Choosing a tool" and "Troubleshooting tool calls" pages, all read today. Anything I could not confirm is marked "(unverified)".

## 1. What is genuinely good

This is careful, honest engineering, and it is much better than the usual first cut of a hackathon project. These are the things worth naming.

- **The safety rules live in code, not in prompts.** `librarian.gate_verdict` is a readable truth table. In it, "contradicted" stands only when the model and the numeric check agree, a model-only contradiction on a non-numeric claim becomes `possible_conflict`, and no card always means unverifiable. The strategist adds the numeric-or-date restriction from ruling 1 as a second, independent check (`challengeable_claims`). Because there are two gates, a single bug cannot produce a wrong challenge.
- **The numeric checker treats Indian numbers properly.** It handles lakh, crore and Hindi number words such as "dedh" and "saade". It refuses to compare percent with percentage points. When years differ it returns "incomparable" rather than "mismatch". In `numbers.py`, the decision that "only mismatch can confirm" is exactly the right asymmetry.
- **The LangGraph usage is real, not decorative.** Per-claim fan-out with `Send` is used, each list in the state has its own reducer, the conditional edge skips the librarian when there are no claims, and every node catches its own errors. That last point matters because the Builder verified against langgraph 1.2.17 that `error_handler` does not rescue several parallel branches, and wrote that finding into the module docstring. Testing the framework's behaviour before relying on it is the habit I most want the owner to learn.
- **Retries happen in exactly one place.** botocore's own retries are switched off (`total_max_attempts: 1`), transient errors are mapped to `LLMTransientError`, and `RetryPolicy(retry_on=is_transient)` retries only those. As a result, attempts never multiply across layers.
- **The Nova request shape matches the AWS docs.**
  - It uses `toolChoice: {"tool": {...}}`, temperature 0, and a flat top-level schema.
  - It sets `stopSequences: ["</tool>"]`, which the Nova "Choosing a tool" page recommends when setting a tool choice.
  - The `cachePoint` comes after a stable shared prefix in `system`, which the Bedrock guide lists as cacheable for Nova Pro.
  - Validation errors are fed back to the model on the retry instead of repeating an identical request.
  - Cache read and write token counts are recorded on every call.
- **The probes are the best part of the repo.** `gnani_ws.py` keeps every wire detail in one place and marks nine items `# UNVERIFIED:`. `nova_probe.py` builds its requests with the app's own `build_tool_request`, so it measures the exact request shape the app will send. The Prisma probe measures "last audio frame sent to final transcript", which is the number a candidate actually feels. The README says which machine and folder each command runs from. Every check in the self-test passes offline.
- **Integrity is enforced, not just promised.** `FactCard` refuses a real card without a source and refuses a synthetic card with a URL. Every card in `data/cards/` is an obviously fake "Exampleland" test card. The fallback report omits scores instead of guessing them. The judge's citations are verified word for word, with one bounded revision.
- **Tests mostly check behaviour.** Examples are "one librarian call per claim", "a throttled call is retried and then succeeds", "a failing branch leaves the other claim's verdict intact" and "no card means the challenge is never spoken". The FakeLLM returns malformed payloads on purpose, so the retry-then-default path is exercised rather than assumed.

## 2. Must-fix before the voice loop

Each item gives the location, why it matters, and the fix. They are in priority order.

### M1. The librarian cache leaks a throttled fallback across sessions (bug, confirmed by experiment)

**Where.** `graphs.py`: the `CachePolicy(key_func=librarian_cache_key)` on the `librarian` node (lines 386–391), the nonce in `route_after_listener` (lines 283–295), and `compile_turn_graph`, where one `InMemoryCache` is shared by every session in the process.

**Why.** LangGraph's node cache stores the node's whole return value, including a fallback vote. The nonce that is meant to bypass a cached fallback is computed from `lib_failures`, and `lib_failures` lives in *one session's* thread state. A different session therefore computes nonce 0 and hits the poisoned entry. I reproduced this:
- Session A was throttled on every attempt and fell back to "unverifiable", as designed.
- Session B then analysed the same claim and received the cached fallback **without any librarian call**. The total call count stayed at 3, and B's trace even reported a librarian fallback that never happened in B.
- The cache has no TTL, so the poisoned entry lives until the process restarts.

On launch day, one throttling burst would silently switch off fact-checking for that claim for every later user.

**Fix.** Replace the LangGraph node cache with our own small vote cache held in `BrainDeps`. This should be a bounded LRU keyed by `claim_cache_key`, written **only** when a vote is not a fallback, and read at the top of the librarian node. Then delete the nonce logic and `librarian_cache_key`. Add a test with two sessions, a throttled first run, and an assertion that the second session calls the librarian and gets a real verdict. This is simpler to explain to the owner than the nonce.

I recommended the LangGraph node cache in CTO_NOTES_01, so this correction is mine (see Section 5).

### M2. The strategist penalises honesty and can whiplash (Defect A and its relatives)

**Where.** `strategist.py`, rules R3 and R7 (lines 174–222).

**Why.** R3 reassures after an honest "I don't know" only when the previous move was a counter, steelman, pressure or fact challenge (`CHALLENGE_AFTER`). In every other case the answer is short, so it falls through to R7 and gets `pressure`. I checked all the cases offline:
- An honest unknown after `None`, `probe`, `reassure` or `move_on` gets `pressure`.
- A thin answer right after `reassure` also gets `pressure`, so the board is kind and then immediately harsh.
- Nothing stops `pressure` from repeating turn after turn.
- A candidate who concedes an error ("shayad mera figure galat tha") gets a bare `probe` with no acknowledgement.

A real board would not behave like this, and a judge watching the demo would notice.

**Fix.** These are rule changes plus one listener field. Keep the nine moves from PLAN_v2_DELTA unchanged.
1. An honest unknown *never* receives `pressure`. If it is the first honest unknown in this topic, the board should `reassure` with a simpler related question. If it is the second, the board should `move_on`.
2. No `pressure` immediately after `reassure`, and no `pressure` twice in a row. In those cases use `probe`.
3. Add `concedes_error: bool` to the listener tool and to `Analysis`. When it is true, set a `preface="acknowledge"` field on `BoardMove`, so the actor starts with a short acknowledgement ("Theek hai, yeh maanna achhi baat hai.") before the move.

Then add a **board-behaviour invariants** test file. It should run four or five scripted fake sessions and assert properties over the whole conversation rather than single rows: "no pressure after an honest unknown", "no pressure after reassure", "no move three times in a row", and "a concession is acknowledged". This is the kind of test that would have caught Defect A, and it is how we keep catching that class of bug.

### M3. The fact-challenge wording reads the card aloud (Defect B), and the test locks the defect in

**Where.**
- `actor.py`: `fact_challenge_template` (lines 131–135), `claim_figure` (lines 119–128), `source_label` (lines 113–116), `guard_fact_challenge` (lines 165–168), and `build_actor_user`, which passes only `card.statement` (lines 231–241).
- `fake_responders.py`: `actor_responder` (lines 249–254).
- `prompts/actor.v1.md`, line 21.
- `tests/test_actor_opener.py`, line 126.

**Why.**
- Both the template and the fake actor speak `card.statement` verbatim, including the "TEST CARD:" prefix, and the second sentence starts in lower case.
- `claim_figure` renders numbers in a different scale from the card. My experiment produced "Aapne 1,500,000 visitors kaha tha … 15 lakh foreign tourist arrivals". That is the same quantity, but it sounds like a contradiction, and "1,500,000" read out by a TTS engine sounds absurd in Hinglish.
- The guard accepts any line that ends with "?" and contains no blacklisted phrase. A model line could therefore quote the wrong figure and still pass.
- The test asserts `card.statement in line.text`, which means it *requires* the defect.

**Fix.**
1. Add a `spoken_figure(value, unit, like=card_unit)` helper next to `numbers.py`. It should render a value in the card's own scale and unit ("15 lakh", "12 percent", "saal 2019"), using `canonical_unit` so that claim and card always come out in the same words.
2. Rewrite the template to the orchestrator's shape, adding a short source name. For a real card: "Aapne 30 percent kaha. {source_short} ke {year} ke figure ke hisaab se yeh 12 percent hai. Aapka figure kahan se hai?" For a synthetic card, use "hamare test data" as the source name.
3. Add an optional `source_short` field to `FactCard` (for example "India Tourism Statistics"), because `source_title` with a table and page number is too long to speak.
4. Pass `claim_figure`, `card_figure`, `card_year` and `source_short` to the actor as structured fields, and write `actor.v2.md` describing the required shape. Record it in the prompt CHANGELOG.
5. Strengthen the guard: a fact-challenge line must contain the card's spoken figure and end with a question. Otherwise the template is used.
6. Fix the fake responder to use the same template.
7. Replace the test assertion with shape checks: it contains the claim figure, the card figure and the year, has no "TEST CARD" text, starts with an upper-case letter, and ends with "?".

### M4. The analysis path puts the librarian, and the whole final segment, on the critical path

**Where.** The `graphs.py` topology `listener → librarian×N → gate → strategist` (lines 394–398), `TurnAnalyzer.analyse` (which returns only when the whole run ends), and `session.py`: `answer()` waits on `asyncio.gather` with `analysis_wait_timeout_s = 60` (`config.py` line 63). `submit_segment`, which the `session.py` docstring promises on line 14, does not exist.

**Why.**
- Fact challenges are delayed by at least one turn by design (R4 with `fact_challenge_min_delay_turns ≥ 1`), so the current turn's move *never needs* this turn's librarian verdicts. The graph nevertheless makes the strategist wait for every librarian branch.
- In voice mode, the final segment's transcript only arrives after the candidate stops speaking. If the strategist waits for the final segment's listener *and* librarian calls, that is two sequential Nova round trips from Mumbai to Sydney before the actor even starts, against a 2.0 s p50 target. I do not have Nova latency numbers yet, because the day-1 probe has not run, so the exact cost is unverified. The structure is wrong whatever the numbers turn out to be.
- A 60-second wait is a text-mode value and cannot exist in the voice path.

**Fix.**
1. Split the analysis into two graphs, each with its own per-session lock. The **segment graph** runs `listener → strategist`. It is fast, and the move is ready when it finishes. The **fact-check graph** runs `Send` per claim, then `librarian`, then `gate`, and writes the ledger. Its result feeds R4 on the *next* turn.
2. The strategist reads the latest ledger snapshot. Add a config validator that refuses `fact_challenge_min_delay_turns < 1`, so this split can never become unsafe.
3. Give `InterviewSession` the streaming API that Pipecat will need: `submit_segment(segment)` as each Prisma final arrives, and `end_of_turn()`, which waits at most `move_deadline_ms` for this turn's latest move.
4. If the final segment's move is not ready by then, `end_of_turn()` uses the latest move already computed for this turn from earlier segments. If there is none, it uses a deterministic fast rule: silence gets reassure, otherwise probe. Every use of this fallback is counted in the trace.
5. Set `move_deadline_ms` from probe data, not by guessing.
6. Add the latency-budget test from ruling 4: fake services with injected delays, asserting that the actor request starts within the deadline even when the librarian is slow.

This changes the order written in PLAN_v2_DELTA §2. Section 5 flags it.

### M5. The actor's hot-path thread can be starved by background calls

**Where.** `llm.py`: `asyncio.to_thread(...converse...)` (line 465) and `loop.run_in_executor(None, worker)` for streams (line 514). Both use the default executor, and the `boto3.client` config sets no `max_pool_connections`.

**Why.** Python's default executor has `min(32, cpu_count + 4)` workers, which is 6 on a 2-vCPU t3.medium. botocore's default connection pool is 10. Each actor stream holds a thread for its whole duration, and each librarian branch holds one too. With two or three concurrent sessions fanning out claims, the actor's ConverseStream call, which is the only thing on the critical path, queues behind background fact-checks. This is priority inversion on the exact number we publish.

**Fix.** Use two dedicated `ThreadPoolExecutor`s in the Bedrock client: a small one reserved for the actor (hot path) and a larger one for background structured calls. Set `max_pool_connections` to at least the sum of the two pools. This is about ten lines, plus a test asserting that the actor uses the hot-path pool.

### M6. The project is not under version control

**Where.** The repo root. `git status` reports "not a git repository", and `README.md` is referenced by `pyproject.toml` but missing.

**Why.** The review loop in ENGINEERING.md §2 (branches, PR descriptions, CTO review, then merge) cannot happen without git. Nothing can be rolled back, and the owner cannot see what changed between rounds.

**Fix.** Run `git init`, commit the current state as the phase-1 baseline, and push to a private GitHub repo. Add the README the orchestrator already asked for, including the `PYTHONPATH=src` note and the `make` targets. This takes 15 minutes.

## 3. Should-fix soon (this week, before the public demo)

1. **Nova greedy decoding.** The Nova "Troubleshooting tool calls" page says tool calling works better with greedy decoding and shows `additionalModelRequestFields={"inferenceConfig": {"topK": 1}}` alongside temperature 0. `build_tool_request` (`llm.py` lines 412–437) sets temperature only. Add `topK: 1`, and let the nova probe confirm it changes nothing harmful.
2. **The flat-schema check is too permissive.** The same page lists `description` among unsupported top-level fields, but `assert_flat_schema` allows it (`llm.py` line 158). No current tool uses it, but the guard should match the docs.
3. **Truncated tool output.** `structured()` never checks `stopReason`. If the judge hits `maxTokens` (2,500 in `judge.py` line 116, which may be tight for a Hindi report in Devanagari: unverified), the truncated JSON fails validation and the retry repeats the same limit. When `stopReason == "max_tokens"`, raise `LLMOutputError` immediately with a clear note, or retry once with a larger limit. Make the judge's limit a setting.
4. **Calibrate the prompt-size budget.** `approx_tokens` uses characters divided by 4. That is reasonable for Latin-script Hinglish, but it can undercount Devanagari substantially (unverified for Nova's tokenizer). Once real cards exist, read the exact cached-prefix size from `cacheWriteInputTokens` on one probe call and record the ratio. Also note that today's synthetic prefix is about 929 tokens by the approximation, which is below Nova's documented 1K-token minimum per checkpoint. Expect zero cache reads from the app until real cards are added, and do not misread that as a bug.
5. **Tag escaping.** `prompts.tag` neutralises only the exact lower-case closing tag. `</CANDIDATE_SPEECH>` and `</candidate_speech >` pass through unchanged (I checked). Escape `<` and `>` inside untrusted content, or use a case-insensitive regex, and add a test with the injection string from CTO_NOTES_01 §6.
6. **A real-prompt Nova probe.** `nova_probe.py` measures a 1,500-token synthetic prefix with a toy schema. Add a `--app-prompts` mode that times the real listener, librarian and actor calls through the app's `PromptKit` on five sample segments. Run every probe (Prisma, Timbre and Nova) from the EC2 box in Mumbai as well as the laptop. Those numbers set `move_deadline_ms` in M4.
7. **An eval regression gate.** `eval/runner.py` prints counts but compares nothing. Add `make eval-gate`. It should compare the run against a committed baseline summary and exit non-zero if planted-error catches go down or false corrections go up (ENGINEERING.md §5).
8. **Session hygiene for the public demo.** Sessions whose browser closes never call `finish()`, so their checkpoint thread, lock and tasks stay in memory. Add an idle-session sweeper. Traces contain full transcripts, so add a retention rule (for example, delete after 7 days), show it in the consent notice, and keep `LANGFUSE_ENABLED` off for public users. The contest requires synthetic data, and public visitors are real people.
9. **WALKTHROUGH.md and ADRs.** The code cites "ADR-003", but `docs/adr/` is empty and there is no WALKTHROUGH. The owner's learning goal depends on these. Write ADR-001 to ADR-005: Pipecat for real time, LangGraph for the slow brain, cards in the cached prompt, forced tool use, and the two-graph split from M4. Write one WALKTHROUGH section per module, in reading order.
10. **Tests that pin implementation.** `test_default_budget_is_18000` pins a constant rather than a behaviour. `test_results_carry_ids_and_stage_timings` asserts an exact stage list, which M4 will legitimately change. The fact-challenge test pins Defect B (see M3). Rewrite these to assert properties.

## 4. Nice-to-have

- `BrainDeps.retry_max_attempts` is unused. Remove it.
- The `test_cli_fake_end_to_end` test could also assert the wording shape of the challenge line once M3 lands.
- `split_segments` treats " | " as a forced boundary, which is handy. Document it in the CLI help.
- Print the card-set version and "all synthetic" in the report footer as well as at startup, so screenshots can never be mistaken for real facts.
- The fake actor repeats identical probe lines. That is fine offline, but `actor.v2.md` should tell Nova not to repeat the previous board line, and the board-behaviour invariants could check that.

## 5. Disagreements with the rulings

1. **Ruling 4, librarian node cache (I am correcting my own recommendation).** CTO_NOTES_01 suggested LangGraph's `CachePolicy` for librarian votes. I now recommend against it: the node cache stores whatever the node returns, fallbacks included, and the Builder's nonce workaround fails across sessions (M1). Use our own bounded vote cache that stores only real votes. The *intent* of ruling 4 (cache by claim hash and card-set version) is unchanged.
2. **PLAN_v2_DELTA §2, graph order (CONFLICT).** The delta specifies `listener → librarian → strategist` as one background graph. I recommend splitting it into a segment graph (`listener → strategist`) and a fact-check graph (`librarian → gate`), so that the librarian is never on the critical path (M4). Safety is unchanged: R4 already waits at least one turn, and a config validator will enforce that. The orchestrator should decide this before the voice loop starts, because it shapes the Pipecat integration.
3. **Defect B wording (a refinement, not a conflict).** The orchestrator's example, "mere paas 2023 ka figure 12 percent hai", drops the source. PLAN_v2_DELTA §6 says challenges cite the card, so I recommend adding a short spoken source name. That needs the new optional `source_short` card field (M3).
4. **Defect A remedy (a refinement).** I agree with "reassure or move on". I suggest the concrete rule of reassuring on the first honest unknown in a topic and moving on at the second, plus an acknowledgement preface after a concession. The latter adds a `preface` field to `BoardMove` rather than a tenth move, so the nine moves stay as the delta lists them.

I have no disagreement with rulings 1, 2, 3, 5, 6, 7 or 8. Ruling 7 is applied in my ENGINEERING.md update today.

## 6. The next chunk for the Builder, in priority order

1. **M6:** `git init`, baseline commit, README (15 minutes).
2. **M1:** replace the node cache with a bounded vote cache that stores only real votes, plus a cross-session test.
3. **M2:** the honest-unknown, anti-whiplash and concession rules, plus the board-behaviour invariants test file.
4. **M3:** `spoken_figure`, the new template, `source_short`, `actor.v2.md` with a CHANGELOG entry, the stronger guard, the fake responder, and the corrected test.
5. **M4:** the two-graph split, the min-delay validator, `submit_segment`/`end_of_turn` with `move_deadline_ms`, and the latency-budget test with fake delays.
6. **M5** together with should-fix items 1 to 3: split executors and the connection pool, `topK: 1`, stricter schema guard, `max_tokens` handling.
7. **Should-fix 6:** the `--app-prompts` Nova probe mode. The owner runs all probes from the laptop and from EC2, and the numbers go into RESULTS.md and set `move_deadline_ms`.
8. **Should-fix 9:** WALKTHROUGH.md and ADR-001 to ADR-005, written for the owner in reading order.
9. **Should-fix 7:** `make eval-gate`.

Then the Pipecat voice loop starts on top of `submit_segment`/`end_of_turn`. Two decisions belong in ADRs before any Pipecat code is written:
- **Our actor as a custom frame processor.** It should run as our own Pipecat frame processor, not as Pipecat's Bedrock LLM service, so the guards and templates stay ours.
- **Guards versus streaming.** Fact challenges and the opener are spoken from a fully buffered, guarded line (at most 30 words). Every other move streams sentence by sentence to Timbre.

In parallel, the owner's own critical path is still the human data: real sourced cards for topic one, and the frozen dev and held-out text sets. No Builder work can substitute for that.
