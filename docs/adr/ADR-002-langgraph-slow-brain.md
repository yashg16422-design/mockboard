# ADR-002: LangGraph for the slow brain only, split into a fast graph and a fact-check graph

- **Status:** Accepted and implemented.
- **Date:** October 25, 2026 (the two-graph split was accepted on October 25 and built on October 26, 2026).
- **Source:** [PLAN_v2_DELTA.md](../PLAN_v2_DELTA.md), architecture decision 2; [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 4, 9 and 16; [CTO_NOTES_02.md](../CTO_NOTES_02.md), M4; [CTO_NOTES_03.md](../CTO_NOTES_03.md) §1 and §4.
- **Revised:** October 26, 2026 (ruling 16): the last segment's listener call is on the critical path.

## Context

The board's thinking (what did the candidate claim, is a figure wrong, what should the board do next) is a small branching workflow: typed state, several model calls, retries, and checking several claims in parallel. LangGraph is good at that, it has documented tracing integrations, and the owner wants to learn it. But the board's speech is on a strict latency budget (a target of 2 seconds at the median), so anything placed between "candidate stopped" and "board speaks" must be as short as possible.

The first design was a single graph per segment: listener, then librarian for every claim, then strategist. The CTO's code review showed that this puts the librarian on the critical path for no benefit. Fact challenges are a turn late by design, so the move for the current turn never needs the current turn's fact checks, yet the strategist still waited for every librarian call. In voice mode, the last segment's transcript only arrives after the candidate stops, so that design would make two model round trips from Mumbai to Sydney happen before the actor could even start.

## Decision

LangGraph is kept out of the audio pipeline, and the slow brain is split into separate graphs so that the only graph work that can sit between end of turn and the actor is one fast analysis run (one listener call and the pure strategist) for the last segment:

1. The **fast analysis graph** (`listener → strategist`) runs once per transcript segment, in segment order, on a per-session checkpointer thread. It is the only work allowed between end of turn and the actor call, and it always leaves a move ready.
2. The **fact-check graph** (one `librarian` branch per claim via `Send`, then a `gate`) runs in the background for segments that have claims. No move ever waits for it. Its verdicts go into the session's claim ledger and reach the strategist on a later turn.
3. A small **report graph** (`judge → citation_check → report`, with at most one revision) runs after the interview.

The actor, voice-activity detection, barge-in and the speech streams are not LangGraph nodes. The actor is a plain streamed call fed by the precomputed move. The session gains `submit_segment` and `end_of_turn`, and `end_of_turn` waits at most a deadline, then uses the latest ready move or a deterministic fallback.

## Consequences

- **The last segment's listener call is on the critical path.** Listener calls for earlier segments run while the candidate is still talking, but Prisma sends a segment's final transcript only after the candidate stops and the server's voice detector closes that segment, so the last segment's listener call cannot start before the candidate has finished. Between end of turn and the actor there can therefore be two model calls: the last segment's listener, bounded by `move_deadline_ms` (after which the move computed for the previous segment is used, or a deterministic fallback move if no segment finished), and the actor. Fact checks can be slow without delaying any move; a latency-budget test proves this with injected delays.
- **The latency budget is listener-last plus actor, not the actor alone.** Our own p95 target is 3.0 seconds from the candidate stopping to the board's first audio. Split into stages (these are budgets we chose, not measurements; CTO_NOTES_03 §4):

  | Stage | Budget |
  |---|---|
  | Turn detection (Silero 0.2 s plus Smart Turn) | 0.3 s |
  | Last final transcript plus the last segment's listener call | 1.0 s |
  | Actor's first sentence | 1.2 s |
  | Timbre first audio byte | 0.3 s |
  | Transport | 0.2 s |

  The current `move_deadline_ms` of 1,500 ms does not fit this budget and stays provisional until the Prisma and Nova probes (`nova_probe.py --app-prompts`) measure the real numbers from EC2.
- A config validator refuses `fact_challenge_min_delay_turns < 1`, so the split can never be made unsafe by a same-turn challenge that would need verdicts the fast graph does not have.
- The ledger snapshot is frozen per turn, so the move never depends on how fast a fact check happened to be, which keeps behaviour reproducible.
- We must be honest in the submission: this is a deterministic multi-stage workflow with named patterns, not "autonomous agents" (ruling 4).
- Every node catches its own errors, because the builder verified against LangGraph 1.2.17 that one failing parallel branch fails the whole superstep.

## Alternatives considered

- **One graph with the librarian before the strategist** (the original plan). Simpler to draw, but it puts fact checks on the critical path for no benefit.
- **No LangGraph, plain asyncio.** Possible, but we would rebuild typed state, reducers, retries and fan-out ourselves, and lose the documented tracing integration.
