# Mock Board — project memory for Claude Code

Mock Board is a voice-first, Hinglish UPSC Personality Test (interview) board, with a Debate mode, built for Gnani.ai's Great Indian AI Internship Challenge. Submission: **Nov 9, 2026** (contest deadline Nov 10, no extensions). Owner: Yash Pratap Singh ("Aloo"), solo builder, learning agentic AI. He wants complete plain-language explanations, commands that state which machine and folder they run from, and honest status (never fake a result).

## How work is done here (the team)
- **You (the main session) are the orchestrator.** You plan, delegate, verify, and decide. You never accept a subagent's report without re-running the checks yourself (`make test`, `make lint`, the relevant `make` target, and reading the diff).
- **`builder` subagent** (`.claude/agents/builder.md`) writes code in small commits.
- **`cto-reviewer` subagent** (`.claude/agents/cto-reviewer.md`) designs before big features and reviews every builder chunk before it is accepted. It never edits code. Its notes go in `docs/CTO_NOTES_NN.md`.
- Loop for every feature: CTO design note → builder chunk(s) → CTO review → orchestrator verifies and records a ruling in `docs/ORCHESTRATOR_LOG.md` → next chunk.
- Rulings in `docs/ORCHESTRATOR_LOG.md` are binding until the owner overrides them. Read the last three rounds before starting work.

## Hard constraints (owner's)
- LLM: **Amazon Nova Pro only**, Bedrock **ap-southeast-2 (Sydney)**, model id `amazon.nova-pro-v1:0`. AWS credentials come from the owner's AWS CLI profile (`export AWS_PROFILE=default`), never from code.
- Speech: **Gnani Prisma** (STT, `wss://api.vachana.ai/stt/v3/stream`) and **Gnani Timbre** (TTS, `wss://api.vachana.ai/api/v1/tts`). Key in `.env` as `GNANI_API_KEY` (never commit `.env`, never print the key).
- Contest rules: name every Gnani model used; **synthetic data only** (no real people's recordings or identifiers); a demo that fakes a result is worse than an honest limitation.
- Never invent statistics, sources, URLs, voice ids or API fields. Mark unknowns `# UNVERIFIED:`.
- Synthetic test fact cards (`data/cards/*.json`, "TEST CARD: Exampleland…") must never reach prompts outside `--fake-llm` (Rulings 29, 36).

## Architecture (as built, see docs/WALKTHROUGH.md and docs/adr/)
- **Fast mouth, slow brain.** Real-time voice on **Pipecat 1.5.0** (SmallWebRTC, Silero VAD, Smart Turn v3) → `src/mockboard/voice/brain_processor.py` → Session API (`submit_segment`, `end_of_turn`, `speak_stream`) in `src/mockboard/session.py`.
- **Slow brain on LangGraph** (`src/mockboard/brain/graphs.py`): fast analysis graph (listener → strategist), fact-check graph (per-claim librarian fan-out with `Send`, numeric gate, real-votes-only vote cache), report graph (judge → citation check → one revision → report).
- Strategist is **deterministic rules** (`brain/strategist.py`, debate: `modes/debate/strategist.py`); actor is one streamed Nova call with guards (`brain/actor.py`).
- Modes are profiles, not forks (`src/mockboard/modes/`, ADR-009): `upsc_interview` (default) and `debate`.
- Speech adapters: `src/mockboard/speech/prisma_stt.py` (our 1,024-byte framing, waits for Prisma's in-flight final — commit gets no reply in practice, Ruling 34), `speech/timbre_tts.py` (interruption fix, serialised requests, Timbre v2.5 or v3.0).
- Three Bedrock lanes (actor / fast / background) with short hot-lane timeouts; chaos test proves sessions survive throttling.

## Known state (Nov 1, 2026)
- `make test` passes offline; 913 tests with the voice extra installed at P1, Nov 1 (the count grows, so trust the run, not this line). Only Python 3.14 has been run; 3.12 is unverified.
- Owner's first real session (Round 20) exposed: fixed topics ("tourism", "internal_security" hard-coded in `config.py`), no understanding of candidate requests ("let's talk about something else"), actor sees only the last Q/A, no panel structure. **Next phase fixes these: see `docs/tasks/PANEL_PHASE.md`.**
- Owner still to report: voice lab choice (`make voice-lab`), Prisma mode choice (`make stt-ab`).
- No real fact cards yet; real sessions run without fact checks and say so.

## Commands (owner's Mac, in the repo folder, inside `.venv`)
- `make venv` then `make voice-install` — environment (Python 3.12+).
- `make test`, `make lint`, `make eval-gate`, `make eval-debate`.
- `make cli-fake`, `make cli-fake-debate` — offline text runs; `make cli-real` — text on Nova.
- `make voice-fake` — free voice run; `make voice-upsc`; `make voice-debate MOTION=1 SIDE=for`.
- `make voice-record`, `make stt-ab`, `make voice-lab`, `make voice-summary`.
- Traces: `traces/*.jsonl` (read them to diagnose real sessions; they contain no secrets).

## Engineering rules
- Small commits, each with `make test` and `make lint` green; stage files by path; never commit `.env`, `traces/` or `.venv/`.
- Commit trailer: `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Prompts are versioned files in `src/mockboard/prompts/` (never edit an old version; add `.vN+1` and log in `prompts/CHANGELOG.md`).
- Every new behaviour gets a test; board-behaviour invariants (`tests/test_board_invariants.py`, `tests/test_debate_invariants.py`) must keep passing.
- Real-service checks: never make a real Gnani or Bedrock call that costs credits without the owner's approval first (Ruling 41). This covers subagents. Say what the call does and what it costs when asking. Free targets (`make test`, `lint`, `cli-fake*`, `voice-fake`, `eval*`) need no approval.
