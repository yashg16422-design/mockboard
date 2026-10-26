# ADR-004: JSON from Nova Pro through forced tool use

- **Status:** Accepted and implemented.
- **Date:** October 25, 2026
- **Source:** [PLAN_v2_DELTA.md](../PLAN_v2_DELTA.md), architecture decision 5; [ORCHESTRATOR_LOG.md](../ORCHESTRATOR_LOG.md), round 3 (the Nova request fixes) and ruling 16; [CTO_NOTES_02.md](../CTO_NOTES_02.md), should-fix items 1 to 3; [CTO_NOTES_03.md](../CTO_NOTES_03.md) §1.
- **Revised:** October 26, 2026 (ruling 16): the stop-sequence wording.

## Context

The listener, the librarian and the judge must return structured data (claims with values and units, a verdict with a card id, scores with evidence) that code can rely on. The project is constrained to Amazon Nova Pro on Bedrock in Sydney, and Nova Pro has no dedicated structured-output mode. Asking for JSON in plain text invites extra prose, broken brackets and missing fields. AWS announced that Nova supports the `tool` option of `toolChoice` in the Converse API, which forces the model to call one named tool.

## Decision

Every structured call gives Nova exactly one tool whose input schema is the output we want, and forces it with `toolChoice = {"tool": {"name": ...}}`. The tool's input is our JSON. Around that:

- **Deterministic decoding.** Temperature 0, plus `topK: 1` sent as `additionalModelRequestFields.inferenceConfig`, the placement shown on Nova's "Troubleshooting tool calls" page.
- **Flat schemas.** The top level may contain only `type`, `properties` and `required`, because that page lists other top-level fields as unsupported. `assert_flat_schema` refuses any tool schema that breaks this, so a mistake fails at start-up rather than at runtime.
- **A stop sequence** (`</tool>`). Nova's "Choosing a tool" page recommends it whenever any tool choice is set (`auto`, `any` or `tool`), not only in the forced `tool` mode we use, so the model stops right after the tool call.
- **Validate, retry once, then a safe default.** Pydantic validates the tool input. If it is invalid, the call is retried once with the validation errors fed back to the model (an identical retry at temperature 0 tends to repeat itself). If it is still invalid, a typed error lets the graph node use its safe default: no claims, an unverifiable vote, or a report without scores.
- **Truncation is handled separately.** If Nova stops at the token limit (`stopReason` "max_tokens"), the call is retried once with double the limit, up to a ceiling, because telling the model about validation errors cannot fix a cut-off answer.

The actor and the opener are the exception: they stream plain text, because they produce a spoken line, not data.

## Consequences

- Every model output that code depends on is typed and validated, and the failure path is tested on purpose with malformed fake payloads.
- Tool schemas are not cacheable in Bedrock's prompt cache (the Bedrock caching guide lists `system` and `messages`, not `tools`), so schemas are kept small.
- The probes reuse the app's own request builder, so they measure exactly the request shape the app sends; `nova_probe.py --app-prompts` sends the real listener request with the real cached prefix. Whether prompt caching still works when a tool is attached is marked UNVERIFIED until that probe runs (rule N5 in CTO_NOTES_03 §4).

## Alternatives considered

- **"Reply in JSON" in the prompt.** Rejected: nothing forces the shape, so parse failures would be common.
- **A different model with a native structured-output mode.** Not allowed by the project's constraints.
