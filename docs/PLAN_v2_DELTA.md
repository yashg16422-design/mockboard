# Mock Board — v2 decisions (supersede PLAN_v1.md where they conflict)

Source of truth for the full plan: the shared doc "Mock Board: Master Plan" (v2). This file is the agents' copy of what changed after the first CTO review, plus the orchestration decisions made on Oct 25, 2026.

## Hard constraints (from the builder/owner, do not change)
- LLM: Amazon Nova Pro only, Bedrock region ap-southeast-2 (Sydney). Model id `amazon.nova-pro-v1:0`.
- Speech: Gnani Prisma v2.5 (STT, streaming WebSocket `wss://api.vachana.ai/stt/v3/stream`), Gnani Timbre v2.5 (TTS, WebSocket `wss://api.vachana.ai/api/v1/tts`, SSE `/api/v1/tts/sse`). Auth header `x-api-key-id`. 4,900 credits (₹1 each).
- Deploy: EC2 (t3.medium, Mumbai ap-south-1) + Caddy HTTPS. AWS creds via IAM role on EC2; locally via the AWS CLI profile.
- Contest: Gnani "Great Indian AI Internship Challenge 2026", deadline Nov 10 2026, submit Nov 9. Synthetic data only. Must name every Gnani model used.

## Architecture decisions (v2)
1. Real-time path = **Pipecat** (pin to the version `pipecat-gnani` was tested with, v1.5.0) with SmallWebRTC transport, Silero VAD + Smart Turn v3 for end-of-turn (gated on Prisma's last transcript), `pipecat-gnani` STT/TTS, Bedrock LLM. Not built in phase 1.
2. **Slow brain = LangGraph** (see ORCHESTRATOR_LOG Ruling 9: now split into a fast analysis graph and a separate fact-check graph) (decision made Oct 25 at the owner's request; the owner's stack uses LangGraph). LangGraph is used ONLY off the real-time critical path:
   - Turn-analysis graph, run as a background task per transcript segment: `listener -> librarian -> strategist` writing into typed session state.
   - Post-session graph: `judge -> report`.
   - The actor (the one streamed Nova call after turn end) is NOT a LangGraph node in the hot path; it is a plain async streaming call fed by the precomputed move. (It may be wrapped for testing.)
   - Strategist is pure deterministic rules (a node with no LLM).
3. Prisma: final transcripts per server-VAD segment only; 1,024-byte PCM s16le mono frames at real-time cadence (32 ms @16 kHz); config only via upgrade headers; 15-minute session cap; set `x-max-speech-s≈15`; `lang_code` fixed per connection (`en-IN` default for Hinglish → Latin script; `hi-IN` → Devanagari); `x-format verbatim` (keep fillers; listener normalises numbers); word boosting via `update_settings` (≤100 single words, letters only). `x-partials` header mentioned once in docs, undocumented — probe it.
4. Timbre: no documented cancel message — barge-in closes and replaces the connection; request raw PCM s16le 16 kHz.
5. Nova Pro: no structured-output mode → JSON via **forced tool use** (`toolChoice` = the tool), temperature 0, flat schema (top-level `type`/`properties`/`required` only), validate with Pydantic, retry once, then a safe default. Prompt caching: stable prefix (persona pack, then fact cards) with cache checkpoints.
6. Fact checking: fact cards (40–60 per topic) live in the **cached prompt**, no BM25. Listener normalises each claim to English with `claim_en`, `value`, `unit`, `year`. Librarian returns supported / contradicted / unverifiable + `card_id`. A deterministic numeric check with tolerance must agree. Two runs at temperature 0 must agree before a contradiction counts. **Ask, never assert**: challenges are questions citing the card. No card ⇒ unverifiable ⇒ never challenged.
7. Moves: probe, counter, steelman, fact_challenge, pressure, reassure, move_on, clarify, interrupt.
8. Side-channel signals: response_latency_s (client playback-ended → VAD onset), articulation rate (syllables per voiced second vs candidate baseline), filler_count (per-script lists, "as transcribed"), long_pause_count, self_corrections. NO pitch.
9. Latency target: p50 2.0 s, p95 3.0 s; false cut-in rate < 5% is a primary metric.
10. Evaluation: 60 scripted text answers (20 planted errors, 20 one-sided, 20 balanced) split 30 dev / 30 held-out, written before prompt tuning; ~150 hand-checked true claims for false-correction bound; 30 audio clips (same 10 answers × Hindi / Hinglish / Awadhi-flavoured); Evon v3.3 as an alternative judge (Cohen's kappa vs Nova).
11. Innovations adopted: dual-decode code-mix fusion (hi-IN + en-IN streams fused by the listener), echo-aware barge-in, board-initiated interruption, Evon judge, "show your work" panel. Lead award recommendation: Mixed Vibes (owner to confirm).

## Integrity rules for all agents
- Never invent real-world statistics, sources, or URLs. Fact cards that are not yet sourced must be marked `"synthetic_example": true` with `source_url: null` and an obviously-fake statement used only for tests. The owner authors the real cards from official sources (MoT India Tourism Statistics, MHA annual report, PIB).
- Never hard-code secrets. Read from environment / AWS default credential chain.
- Mark anything unverified as `# UNVERIFIED:` in code comments and in docs.
