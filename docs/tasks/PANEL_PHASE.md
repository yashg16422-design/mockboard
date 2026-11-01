# Task brief: the Panel phase (approved by the owner, Nov 1, 2026)

## Why
In the owner's real session the board kept returning to tourism after he asked to change topic, replies didn't connect to what he had said, and it did not feel like a UPSC interview. Root causes (verified in code by the orchestrator):
1. Topics are hard-coded (`config.py`: `["tourism", "internal_security"]`).
2. The listener extracts stance/reasons/claims only; it cannot recognise a candidate's request ("can we move on?", "please repeat", "I'd rather not attempt this").
3. The actor sees only the last question and last answer (no conversation memory).
4. Three personas speak in turn without a panel structure, hand-offs or domains.

## Goal
A real UPSC-style panel, driven by a LangGraph interview-director graph, personalised from the candidate's application form, that understands candidate requests and remembers the conversation. Plus two utility features. Everything stays honest (no invented facts) and fast (the voice path's latency target stays p50 ≈ 2 s, p95 ≈ 3 s from end of speech to board audio).

## Scope (in order)
1. **Interview director graph (LangGraph).** A session-level graph whose state holds: the agenda (ordered segments), current segment and member, per-segment question count, conversation memory (rolling summary + last few exchanges), candidate requests, and fact-check verdicts. Nodes (suggested; the CTO finalises):
   - `plan_agenda` (once, at session start, background-friendly): from the application form, build 5 segments — Chairman opener (form/HR), Member 1 home state and district, Member 2 degree subject, Member 3 hobbies/optional subject, Member 4 current affairs + one "you are the DM/SP…" situational question — each with 2–3 open-ended seed questions. Open-ended only; no facts about places unless a real fact card backs them.
   - `understand_turn` (wraps the existing listener; adds `candidate_request`: none | change_topic | repeat | clarify_question | skip | end).
   - `route` (conditional edges): candidate request → `handle_request` (acknowledge politely; repeat/rephrase, or hand off to the next member/segment); segment budget reached → `handoff`; else → `next_question` (existing strategist rules for probe/counter/steelman/fact_challenge/reassure stay as the move policy within a segment).
   - `handoff`: spoken transition ("Dhanyavaad. Dr. Iyer, please.") and member switch; members may refer back to earlier answers.
   - `close`: the Chairman's closing line ("Thank you, that will be all."), then the existing report graph.
   - The existing fast analysis graph and fact-check graph become subgraphs or are called from these nodes; the strategist stays deterministic within a segment. Use a checkpointer so state survives the whole session.
2. **Conversation memory for the actor**: the actor prompt gets the agenda item, the member's domain, a short rolling summary and the last 2–3 exchanges, so each question builds on what the candidate actually said. New prompt versions (actor, listener), never edit old ones.
3. **Panel personas**: Chairman + 4 members with domains, distinct Timbre voices (owner's voice-lab choice when available), formal UPSC register ("aap", courteous), ritual opening ("Please be seated") and closing. Session length ≈ 15–20 minutes, well under Prisma's 15-minute stream cap via rotation (already supported).
4. **Utility A — "Likely questions from your form"**: a pre-interview page/CLI that, from the form alone, lists likely board questions per segment with short hints on how to approach them (no invented facts; hints are about structure and angle).
5. **Utility B — Report by panel member**: the report grouped by segment/member, the weakest question with a model answer outline, and a short practice plan; plus "replay this moment" for the weakest question (Ruling 5).

## Acceptance (the CTO verifies each)
- Saying "tourism ke alawa kisi aur topic pe baat karte hain" (or English equivalents) in a scripted fake session moves to the next segment with an acknowledgement, every time (invariant test).
- No hard-coded topic list remains; the agenda comes from the form.
- Each board line in a scripted session can be traced to its agenda item and member; hand-offs are spoken.
- The actor never repeats the candidate's words verbatim, never quizzes numbers, never states an uncarded fact (existing guards keep passing).
- Latency: the fast path still adds only the listener + actor after end of turn; `plan_agenda` runs before the first question, not per turn.
- UPSC and debate tests, invariants and chaos test keep passing; debate mode is unaffected.
- A real voice session on the owner's Mac (`make voice-upsc`) shows the panel structure and topic-change handling; the orchestrator reads the trace to confirm.

## Then
EC2 deploy (V8 in docs/CTO_NOTES_03.md), demo video, submission write-up. Real fact cards from official sources if time allows.
