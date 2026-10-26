# ADR-005: Three Bedrock lanes and short hot-lane timeouts

- **Status:** Accepted and implemented. All timeout values are UNVERIFIED placeholders until the Nova probe runs from EC2.
- **Date:** October 26, 2026
- **Source:** [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), rulings 13 and 14; [CTO_NOTES_02.md](../CTO_NOTES_02.md), M5.

## Context

boto3 is synchronous, so every Bedrock call holds a worker thread for its whole duration, and a streamed actor line holds one until its last word. Originally every call used Python's default thread pool (about six threads on a two-core server) and one boto3 client. With two or three sessions fanning out fact checks, the actor's call, the only one on the critical path, could wait behind background work. That is priority inversion on the exact latency number we publish.

A second problem: Bedrock's default read timeout is long (30 seconds in our settings for background work). If a listener call hung, it would block that session's later segments for the whole timeout. And if the actor failed, a botocore retry could add many more seconds of silence.

## Decision

Every Bedrock call runs on one of three lanes, chosen from the call's name, and each lane has its own thread pool and its own boto3 client, with its own connection pool and timeouts:

- **Actor lane** (the actor and the opener): short connect and read timeouts (2 and 4 seconds), and **one attempt, with no retry anywhere** (ruling 14). A first-token budget (`actor_first_token_budget_s`, 2.5 seconds) bounds the wait; if the call fails or misses the budget, the board says a cached fallback line: the speaking persona's own line from its YAML file, or the deterministic template for a fact challenge or a topic change.
- **Fast lane** (the listener): short timeouts (2 and 5 seconds; the read timeout must cover a whole listener call), botocore retries off, and at most two attempts in total through the fast graph's own retry policy, without LangGraph's jitter (which adds up to a second). The listener node also stops waiting after `fast_listener_timeout_s` (3 seconds) or when the turn's deadline passes, records a fallback analysis, and parks the call; if it returns, its claims are still fact-checked (ruling 13).
- **Background lane** (the librarian and the judge): the long timeouts (5 and 30 seconds) and three attempts with backoff and jitter.

A call name that is not listed goes to the background lane, so a new kind of call can never take a critical-path thread by accident. botocore's own retry is off on every lane, so retries happen in exactly one place per call.

## Consequences

- A burst of fact checks cannot delay the listener or the actor. A test proves this with fake Bedrock calls that really block their threads, and a control run on one shared pool shows the delay the lanes prevent.
- A hung listener costs at most a few seconds of one segment's analysis, never the session's later segments or the next turn's move.
- A failing actor produces a short, natural pause line instead of a long silence. The trade-off is that a transient error the retry might have fixed now costs one turn's tailored line; the move itself is still applied, and the trace records `actor_fallback`, `actor_fallback_kind` and `actor_error`.
- Every value is a setting in `config.py`, so the numbers can be set from probe data without code changes. Until then they are guesses and are marked as such.

## Alternatives considered

- **One shared pool with a priority queue.** More complex, and a long call already running would still hold its thread.
- **A capped botocore retry on the actor** (the earlier design). Rejected by ruling 14: up to around 8 seconds of silence is worse than a graceful filler line.
