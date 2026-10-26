# ADR-003: Ask, never assert, with the numeric check as the second vote

- **Status:** Accepted and implemented.
- **Date:** October 25, 2026
- **Source:** [PLAN_v2_DELTA.md](../PLAN_v2_DELTA.md), architecture decision 6; [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 1 and 11; [CTO_NOTES_01.md](../CTO_NOTES_01.md), section 2.

## Context

A practice board that wrongly corrects a candidate is worse than one that says nothing: it teaches a wrong fact and destroys trust in everything else the board says. Language models sometimes say a claim is contradicted when it is not, for example by mixing up years, units, or "percent" and "percentage points". The plan originally asked for two runs of the librarian at temperature 0 to agree before a contradiction counted. The CTO pointed out that two identical requests at temperature 0 usually return the same answer, so the second run adds cost and delay but almost no independent evidence.

## Decision

1. **Ask, never assert.** The board never tells a candidate they are wrong. A fact challenge is a question that quotes the candidate's own figure, then the card's figure with its year and a short source name, and then asks where the candidate's figure came from, for example: "Aapne 30 percent kaha. Exampleland test data ke 2023 ke figure ke hisaab se yeh 12 percent hai. Aapka figure kahan se hai?" (ruling 11).
2. **No card, no challenge.** A claim that no fact card covers is unverifiable and is never challenged.
3. **The second vote is the deterministic numeric check.** A claim counts as `contradicted` only when the librarian model says contradicted **and** `numeric_check` (plain code comparing value, unit and year with tolerance) says mismatch. The numeric check returns "incomparable" rather than "mismatch" whenever the comparison would be unfair (different years, different units, percent versus percentage points).
4. **Live challenges only on numeric or date claims.** A model-only contradiction on a qualitative claim becomes `possible_conflict`: it appears in the report but is never spoken.
5. **The rule is enforced in code in three places**: `librarian.gate_verdict` (the truth table), `strategist.challengeable_claims` (an independent second check), and the actor's guard, which replaces any challenge line that does not end with a question, asserts, leaves out the card's figure, or reads the card text aloud.

## Consequences

- A wrong challenge needs the model and the arithmetic to be wrong in the same direction, and must then slip past two more code checks. A single bug cannot produce one.
- The board will miss some real errors on qualitative claims, and on figures for a different year than the card. This is deliberate: precision matters more than recall here, and the eval counts false corrections separately from catches.
- Every challengeable card must carry `value`, `unit` and `year`, which shapes how the owner writes real cards (`data/cards/README.md`).
- A differently framed second prompt remains a possible later experiment behind the `FEATURE_SECOND_PROMPT_VOTE` flag, measured on the eval set; it is not built.

## Alternatives considered

- **Two identical librarian runs must agree** (the original plan). Rejected: little independence, double cost.
- **Let the model's verdict stand alone.** Rejected: one model mistake would be spoken aloud as a correction.
