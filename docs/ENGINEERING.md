# Mock Board — Engineering Workflow and Standards

Owner: solo builder. Window: Oct 25 to Nov 9, 2026. This document says how we work. PLAN_v2_DELTA.md says what we build; if the two conflict, PLAN_v2_DELTA.md wins and this file gets fixed.

Every shell command in our docs names the machine and the directory it runs from, for example "Laptop (WSL), `~/mockboard`" or "EC2, `/opt/mockboard`". Commands are Linux format.

## 1. Repository layout

This is the layout as built in phase 1 (reviewed in CTO_NOTES_02). Where the code and this file disagree, the code wins and this file is fixed (orchestrator ruling 7).

```
mockboard/
  src/mockboard/
    brain/schemas.py   Pydantic models (Claim, FactCard, Analysis, BoardMove, SessionReport). No I/O.
    brain/tools.py     Forced-tool schemas (flat, hand-written) and their Pydantic validators.
    brain/llm.py       Bedrock Nova client plus FakeLLMClient. The only module that imports boto3.
    brain/graphs.py    LangGraph graphs: turn analysis and the post-session report.
    brain/listener.py, librarian.py, strategist.py, judge.py, numbers.py, cards.py   Node logic.
    brain/actor.py     The plain async streaming actor call and its guards (not a graph node).
    brain/personas/    One YAML file per board member.
    prompts/           One file per prompt version (<id>.v<N>.md), plus CHANGELOG.md.
    session.py         Session controller (text mode now; the voice path will reuse it).
    obs/               Per-turn JSONL trace recorder, Langfuse stub.
    config.py          One settings object, loaded from environment.
    cli.py             Text-mode interview (fake or real Nova).
  data/cards/        One JSON file per topic (<topic>.json) plus schema.json and an authoring README.
  eval/              Loader, metrics, runner; examples/ now, golden dev and held-out sets to come.
  probes/            Day-1 probes for Prisma, Timbre and Nova, plus an offline self-test.
  tests/             Offline unit tests (FakeLLM only). Contract tests go in tests/contract/ with fixtures/.
  examples/          The scripted fake session used by `make cli-fake`.
  docs/              PLAN files, ENGINEERING.md, CTO notes, ORCHESTRATOR_LOG.md, WALKTHROUGH.md, adr/.
```

The dependency rule is that `brain/schemas.py` imports nothing from the project, every model call goes through `brain/llm.py`, and only the future `speech/` and `realtime/` (Pipecat) modules know about audio. Run commands from the repository root with `make` (the Makefile sets `PYTHONPATH=src:.`), or install the package once with `make install`.

## 2. Branch, commit and review loop

We use trunk-based development with short-lived branches. `main` must always pass `make test` and `make lint`. The repository must be under git before any of this works.

1. The Builder works on a branch named `feat/<short-name>` or `fix/<short-name>`, with commits in Conventional Commit style (`feat(brain): add librarian node`). A branch should stay under roughly 400 changed lines so that a review can actually be careful.
2. The Builder writes a PR description containing: what changed, why, how to run it, the order in which to read the files, test and eval results, and any `UNVERIFIED` markers added.
3. The CTO reviews against the definition of done and the review checklist in CTO_NOTES.
4. The orchestrator checks that the change matches PLAN_v2_DELTA.md and the owner's constraints.
5. The owner reads the "how to read this PR" section, runs it locally, and merges. Nothing merges that the owner cannot explain in his own words.

## 3. Definition of done (every task)

A task is done when: it runs from a clean checkout with the documented command; unit tests pass offline with the FakeLLM; Pydantic validates every LLM output; no secret, key or account ID is in code, logs or fixtures; every external call emits a timed trace span; a prompt change carries an eval result (Section 5); WALKTHROUGH.md covers any new module; a non-obvious decision has an ADR; and anything unchecked against documentation is marked `# UNVERIFIED:`.

## 4. Test pyramid

- **Unit tests (many, run in seconds).** Nodes, the strategist rules, the numeric tolerance check, reducers and Pydantic parsing are tested with a FakeLLM that returns scripted tool-call payloads, including malformed ones, so that the retry-then-default path is tested on purpose. Tests assert behaviour (what the board does or says), not implementation details such as exact stage lists or constant values.
- **Board-behaviour invariants (a handful, offline).** Scripted fake sessions are checked for properties over the whole conversation, for example "no pressure after an honest 'I don't know'" and "a fact challenge quotes both figures and ends with a question". Each defect found by watching a session becomes one invariant.
- **Contract tests (a few dozen, offline).** We record real responses once (Bedrock Converse tool-use payloads, Prisma `connected`/`processing`/`transcript`/`error` frames, Timbre audio chunk framing) and save them as fixtures with keys removed. A small fake WebSocket server replays them, so our adapters are tested against the real wire format without spending credits. Each fixture is re-recorded only on purpose, and the date is written into the file.
- **Eval runs (on every prompt or model-facing change).** See Section 5.
- **Live smoke tests (three to five, run by hand before a deploy).** One Nova call, one Prisma stream of a known clip, one Timbre synthesis, and one full ten-turn text session. These cost real money and credits, so they are never run in a loop.

## 5. Eval-driven development

We write the test before we tune the prompt. The golden sets from PLAN_v2_DELTA.md (60 scripted answers split 30 dev and 30 held-out, about 150 hand-checked true claims, 30 audio clips) are written and frozen before any prompt tuning starts.

The merge gate for any change to a prompt, a schema, a model parameter or the strategist rules is as follows. On the dev set, the count of planted errors caught must not go down, and the count of false corrections on the true-claim set must not go up. We gate on counts, not percentages, because each item in a small set is worth several percentage points. The held-out set is run only at milestones (about Nov 3 and Nov 7), so that we do not slowly tune against it.

Each run (`make eval`, which runs `python -m eval.runner --split dev`) writes a small summary file to `eval/results/` recording the prompt ids and versions, the counts, cost and timestamp; the git commit is added once the repo is under git. The held-out split refuses to run unless explicitly allowed. A `make eval-gate` target that compares a run with a committed baseline and fails on regression is still to be built (CTO_NOTES_02 §3). Librarian votes are cached by claim hash and card-set version, and only real votes are ever cached, never fallbacks.

## 6. Prompt management

Each prompt lives in `src/mockboard/prompts/<id>.v<N>.md` (packaged with the code) with a short YAML header giving the id, version, intended model, temperature, the tool schema it expects, and the eval run that justified it. Code loads prompts by id and version, never by inline string. Every trace records which prompt versions were used. `prompts/CHANGELOG.md` gets one line per version saying what changed and what the eval showed. Old versions are never edited, only superseded.

## 7. Configuration and secrets

All settings come from one `pydantic-settings` object reading environment variables. `.env.example` lists every variable with a fake value, and `.env` is git-ignored. AWS access uses the default credential chain (the CLI profile on the laptop, the instance role on EC2). The Gnani key comes from the environment locally and from SSM Parameter Store on EC2. A pre-commit hook runs a formatter, a linter and a secret scanner. Feature flags (`FEATURE_*` in `config.py`, see CTO_NOTES_01 §4) are ordinary settings that default to off. Fact cards live in `data/cards/<topic>.json` and are validated against both `schema.json` and the `FactCard` model at load time; a real card without `source_title` and `source_url` is refused, and an unsourced card must be marked `synthetic_example: true`.

## 8. Observability

Our own per-turn trace (`obs/trace.py`, written to `traces/<session_id>.jsonl`) is the source of truth for every latency number we publish. Text mode records only the stages it really has; voice stages are added in phase 2 with the same `mark()` call, never faked. For each turn, the trace recorder appends one JSON line containing the session and turn ids and monotonic timestamps for: VAD end of speech, Prisma final transcript received, end-of-turn decision, actor request sent, first token, first full sentence, Timbre first audio byte, and browser playback start. The same line records the move chosen, the prompt versions, token counts (including cache read and cache write tokens), credits used, and any fallback that fired.

For visual traces we recommend **Langfuse Cloud on the free Hobby plan**. It has a documented LangGraph integration (`from langfuse.langchain import CallbackHandler`, passed in the graph config), and it accepts OpenTelemetry over HTTP at `/api/public/otel`, which is the documented route for Pipecat's built-in tracing, so both paths appear in one tool. The free tier lists 50k units per month, 30 days of data access and two users; whether that covers our demo traffic is unverified, so we check usage after ten sessions. Self-hosting Langfuse on the t3.medium is not realistic, because its documented minimums (web, worker, Postgres, Redis and ClickHouse, with ClickHouse alone at 8 GiB of memory) exceed that machine. Arize Phoenix, documented as running in a single container, is the offline fallback (its LangGraph coverage is unverified). Our Bedrock calls go through our own boto3 wrapper, so the wrapper must put token usage on the span itself; we assume the LangGraph callback does not see raw boto3 calls (unverified). Only synthetic data is ever sent to a tracing service, and never audio.

## 9. Error budgets and fallbacks

Every external dependency has a written fallback, and every fallback is counted in the turn trace.

- Listener or librarian returns invalid JSON: retry once with the validation errors fed back, then use the safe default (no claims, no challenge). Transient Bedrock errors are retried only by the graph's `RetryPolicy`; botocore's own retries are off, so attempts never multiply.
- The move for this turn is not ready by the end-of-turn deadline: use the latest move already computed for this turn, or a deterministic fast rule (CTO_NOTES_02, M4).
- Actor has no first sentence within the masking deadline: play a cached persona filler line.
- Bedrock throttling: back off with jitter; in the hot path, at most one retry, then the filler line and a neutral move.
- Timbre fails: reconnect once, otherwise show the board's line as on-screen text.
- Prisma disconnects: reconnect and tell the candidate to repeat the last sentence.

The budget is our own choice, not an industry figure. If more than one turn in twenty in a test session uses a fallback, or if any test session fails to finish, feature work stops until the cause is fixed.

## 10. Cost tracking

The cost meter converts each session's Prisma seconds, Timbre characters and Bedrock tokens into rupees, using rates kept in config with their source and date. Bedrock rates for Sydney must be copied from the AWS pricing page, not guessed. Eval runs are metered separately from user sessions. A global daily cap and a kill switch stop new sessions when the cap is hit, and the remaining Gnani credits are shown on an admin page.

## 11. Release checklist for the public demo

Before the public link goes out: tests and the eval gate pass; the submission quotes the held-out numbers; five live sessions in a row complete on the deployed box over HTTPS from a phone and a laptop on different networks; the kill switch, daily cap and invite code are tested; risky feature flags are at the safe setting; no secrets are in the repo or logs; the consent and "synthetic data only" notices are visible; every Gnani model used is named; the README states known limitations honestly; and the demo video comes from the deployed build.

## 12. Keeping the code explainable

`docs/WALKTHROUGH.md` has one section per module, written for the owner: what it does in plain words, a simple analogy, the order to read its files, and the command to run it. `docs/adr/NNNN-title.md` records each significant decision (context, decision, alternatives, consequences); the first ADRs cover Pipecat for the real-time path, LangGraph for the slow brain only, fact cards in the cached prompt instead of BM25, forced tool use for JSON, and keeping the librarian off the critical path. Code that cites an ADR number must have that ADR file in `docs/adr/`. Comments explain why, not what. Code handed to the owner comes in reading order with a line-by-line explanation, so he learns the steps rather than memorising code.
