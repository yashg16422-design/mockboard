# ADR-010: The interview director

- **Status:** Draft (chunk P0). Finalised in chunk P11, when the design is built and measured.
- **Date:** November 1, 2026
- **Source:** [CTO_NOTES_06.md](../CTO_NOTES_06.md), sections 2, 4, 7 and 11; [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rounds 23 and 24, rulings 41 to 43.

## Context

The owner's first real session (Round 20) showed four faults: fixed topics in `config.py`, no understanding of what the candidate asks for, an actor that sees only the last question and answer, and no panel structure. He asked to change topic and the board returned to tourism. The Panel phase (Round 23) replaces the UPSC turn flow with a Chairperson and four members who work from an agenda built from the candidate's form.

Three things constrain the design. The move must stay instant (Ruling 9): analysis runs during speech, and a turn must never wait on a model or a fact check. Debate must not change. A failure in any new part must not stop an interview.

## Decision

**One director graph on LangGraph, invoked once per candidate turn inside `end_of_turn`.** Its nodes are `commit_last`, `understand_turn`, `handle_request`, `handoff`, `next_question` and `close`. Every node is plain code, so the graph cannot add model latency, and a test proves this with an LLM client that raises on any call.

- **The director sits on top of the existing move.** `end_of_turn` builds `base_move` as it does today, then calls an optional hook, `hooks.decide_turn(...)`. Debate has no such hook.
- **State lasts the session.** `InMemorySaver`, with `thread_id = session_id`. The thread is deleted at the report. A crashed voice session cannot resume anyway, because its WebRTC call is gone.
- **Reopened turns are safe (Ruling 31).** Deciding writes only `decision`. Memory changes only in `commit_last`, for turns that really finished.
- **Failure falls back.** If the graph raises, `decide_turn` calls the same pure function, `decide_panel(...)`, and records `director_error`.
- **The fast and fact-check graphs are not subgraphs (D1).** Inside a superstep a turn would wait on them. They feed the director's input instead.
- **The report graph stays outside (D2).** `session.finish()` keeps it, because the voice loop must speak first.
- **Agenda planning is a separate graph on the background lane (D3).** A deterministic agenda from the form always exists first, and Nova only upgrades seed text. If planning is slow, fails or returns a bad seed, the template stands.
- **Fixed segment ids, form-driven content (D8).** The five ids (`form_hr`, `home_district`, `degree`, `interests`, `current_affairs`) are the panel's structure. They are not topics.
- **Modes stay profiles (ADR-009).** The panel is built dark behind `PanelSetup` and `session_hooks`, then becomes the UPSC path in one cutover commit (P7).

## Consequences

- **ADR-009's "UPSC transcript is byte-identical" ends at the cutover, deliberately.** The UPSC golden stays identical through P6 and changes exactly once, in P7. The board lines are then reviewed side by side, and the new md5 is recorded in the orchestrator log.
- **Debate stays locked.** P0 adds a debate golden and hashes of its shared prefix, tool schemas and prompts (`tests/test_modes.py`). Any change to them is a defect to explain, not a hash to update.
- **The guards are a limit, not a guarantee.** `# UNVERIFIED:` the place-fact patterns are conservative, so a plain invented sentence can pass them. The prompt forbids it, but the guard cannot prove it.
- **Timing is unmeasured.** `# UNVERIFIED:` Nova's time for the agenda call. The 1.5 s wait is a guess until a real run the owner approves (Ruling 41).

## Open questions

- **Session length (D4) and replay audio (D5)** stay with the owner. The build keeps the current limit and a text-only replay until he decides (Ruling 42).

## Alternatives considered

- **Listener or fact checks as director nodes or subgraphs.** Rejected: waiting would enter the turn, against Ruling 9.
- **Agenda planning as a director node.** Rejected: the per-turn graph could no longer be shown to be model-free.
- **A durable checkpointer (SQLite).** Rejected: `langgraph-checkpoint-sqlite` is not installed, and durability buys nothing when a dropped call cannot resume.
