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
