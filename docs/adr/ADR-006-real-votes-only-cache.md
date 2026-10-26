# ADR-006: A vote cache that stores only real librarian votes

- **Status:** Accepted and implemented.
- **Date:** October 25, 2026
- **Source:** [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), ruling 10 (which withdraws the node-cache part of ruling 4); [CTO_NOTES_02.md](../CTO_NOTES_02.md), M1.

## Context

The same claim is often checked many times: a candidate repeats a figure, many candidates quote the same popular figure, and every eval run re-checks the same items. Caching librarian verdicts saves Nova calls and money. The first version used LangGraph's node cache on the librarian node, keyed by claim and card-set version, with one cache shared by every session in the process.

The CTO found, and reproduced, a serious bug. LangGraph's node cache stores the node's whole return value, including a fallback vote produced when Bedrock throttled every attempt. The workaround meant to skip a cached fallback depended on one session's own state, so a different session hit the poisoned entry. One throttling burst on launch day would have silently switched off fact-checking of that claim for every later user, until the process restarted.

## Decision

We use our own small cache, `VoteCache` in `brain/librarian.py`, instead of LangGraph's node cache:

- It is a bounded least-recently-used map (2,048 entries by default), shared by all sessions in a process through `BrainDeps`.
- The key is a hash of the normalised claim content (English wording, kind, value in base units, unit, year) plus the card-set version. Turn, segment and session ids are left out, so the same claim hits the cache across sessions, and editing any card makes every old entry unreachable.
- **`put` refuses fallback votes.** Only a verdict the model really gave is ever stored, and therefore only real verdicts are ever served to another session.
- The librarian node reads the cache first and writes back only after a successful call. The old nonce workaround and the node cache policy were deleted.

## Consequences

- A throttled session can no longer harm any other session: its fallback votes are unverifiable for that session only, and the next session asks the librarian again.
- A vote carries no session data, so sharing it across sessions leaks nothing about another candidate.
- The cache lives in memory and empties on restart, which is acceptable for a demo.
- Tests prove the property directly: a two-session test with a throttled first session shows the second session calls the librarian and gets a real verdict, and the chaos test checks after 24 sessions that no fallback vote is in the cache.

## Alternatives considered

- **LangGraph's `CachePolicy` with a nonce to bypass fallbacks** (the first version). Rejected: the nonce came from per-session state and failed across sessions.
- **No cache.** Safe but wasteful, especially for repeated eval runs.
