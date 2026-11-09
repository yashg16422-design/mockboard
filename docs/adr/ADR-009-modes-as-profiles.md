# ADR-009: Modes are profiles, not forks

- **Status:** Accepted and implemented (DB0).
- **Date:** October 29, 2026
- **Source:** [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 19, 21 and 22; [CTO_NOTES_04.md](../CTO_NOTES_04.md), §2.

## Context

The owner added a second mode (ruling 19): `debate`, where the board argues the other side of a motion the user picks. The UPSC board is the product, and it must not change. The voice loop, which another builder is writing at the same time, must not need to know which mode it is in. Copying the brain into a second package would double every fix and every test.

## Decision

A mode is a **profile**, a piece of configuration that the same engine reads:

- `ModeProfile` (`src/mockboard/modes/registry.py`) bundles the strategist's rule table (`decide`), the deadline-fallback and turn-cap rules, the moves the rule table may emit (`allowed_moves`), the prompt id for each role (shared prefix, listener, actor, judge, opener), the persona folder, the rubric, the rounds (the session's "topics") and per-move word caps.
- Profiles register themselves in a small registry. `upsc_interview` is the default and wraps today's behaviour exactly (`modes/upsc.py`): its rule table **is** `strategist.decide_move`, and its fallback and cap rules are the session's old code, moved without change.
- `BrainDeps` builds one `ModeRuntime` per registered mode at start-up: the profile, its own `PromptKit` (so one cached prefix per mode), its persona pack and its strategy wiring. The default mode's runtime reuses the existing `kit`, `personas` and `strategy` objects, so every existing caller is unchanged.
- The session stores `mode` (also on `SessionState` and `StrategyContext`, defaulting to `upsc_interview`). The fast graph's strategist node looks the profile up from `ctx.mode`; the session asks the profile for its fallback and cap moves. `Settings.prompt_versions(mode)` returns the UPSC map unchanged by default.
- The CLI takes `--mode` (default `upsc_interview`).

## Consequences

- The UPSC transcript of `make cli-fake` is byte-identical before and after the change. `tests/test_modes.py` locks it with a golden file, so a later mode cannot change it by accident. **Ended on purpose at P7 (ADR-010):** UPSC became the five-member panel and its golden was replaced once, with the debate golden and hashes left as they were.
- Adding a mode means adding a profile module, prompt files, personas and tests; no `if mode == ...` branches in the UPSC code paths. Where a mode needs genuinely new session behaviour (debate's rounds and case file, DB5), the session asks a mode hook rather than branching on the UPSC path.
- The voice loop calls the same three session methods in every mode.
- A rule table that emits a move outside its profile's `allowed_moves` is a bug; each mode has a test for it (DB3).

## Alternatives considered

- **A fork (a second session and brain for debate).** Rejected: two copies of the deadline, fallback, lane and fact-check logic would drift.
- **`if mode == "debate"` checks scattered through the brain.** Rejected: every branch is a chance to change UPSC behaviour, and the voice loop would start to need them too.
