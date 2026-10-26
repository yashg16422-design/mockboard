"""Amazon Nova Pro probe (Bedrock, ap-southeast-2): streaming latency and forced-tool JSON validity.

Phase A: N ConverseStream calls. For each call it measures time to first token (request
sent -> first text delta) and total latency (request sent -> stream end), then reports
p50/p95.

Phase B: N Converse calls with forced tool use (`toolChoice = {"tool": {"name": ...}}`,
temperature 0), the same request shape the app uses. Each tool input is validated
against a small Pydantic model; the probe counts valid, schema-invalid and missing-tool
answers, and records latency.

Both phases put a synthetic system prefix of about 1,500 tokens behind a cache checkpoint
and add up `cacheReadInputTokens` / `cacheWriteInputTokens`, so the owner can see
whether prompt caching kicks in (and whether it still works with a toolConfig, which
src/mockboard/brain/llm.py marks UNVERIFIED).

Mode `--app-prompts` (orchestrator ruling 15) replaces both synthetic phases with the
app's REAL requests, built by the app's own builders from the real persona pack and fact
cards (the cached shared prefix):

* listener: `build_listener_user` + `kit.system("listener")` + `LISTENER_TOOL`, sent through
  `BedrockNovaClient.build_tool_request` (forced tool, temperature 0, topK 1, `</tool>`
  stop), validated against the app's `ListenerOutput` model;
* actor: `build_actor_user` + `line_system(kit, ...)`, sent through
  `BedrockNovaClient.build_stream_request` (ConverseStream), for a cycle of realistic board
  moves (probe, counter, fact challenge, reassure, acknowledge, move on, steelman).

For every call it records time to first token and to the end of the first sentence
(actor), total time, input / output / cache-read / cache-write tokens, JSON validity
(listener) and throttles (with how fast each throttle came back). Sample turns are
synthetic Hinglish about the fictional Exampleland. Per-call rows go to
`probes/results/nova_app_<time>.jsonl` (git-ignored); the summary table is appended to
RESULTS.md labelled with the origin host, region and mode.

Credentials come from the default AWS chain (`aws configure` profile, env vars, or the
EC2 instance role); `--profile` picks a named profile. ThrottlingException is retried with
exponential backoff plus jitter, and every throttle is counted. botocore's own retries are
off, so the counts are honest.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mockboard.brain.llm import BedrockNovaClient, PromptBlock, ToolSpec
from mockboard.config import get_settings
from probes.common import (
    PROBES_DIR,
    RESULTS_DIR,
    append_results,
    fmt_ms,
    origin_host,
    percentile,
)

# ---------------------------------------------------------------------------
# Small schema for the JSON-validity test (flat, as PLAN_v2_DELTA §5 requires)
# ---------------------------------------------------------------------------


class ProbeAnalysis(BaseModel):
    """Pydantic model the forced tool input must satisfy."""

    model_config = ConfigDict(extra="forbid")
    stance: Literal["for", "against", "balanced"]
    claim_en: str = Field(min_length=3)
    value: float
    unit: str
    year: int = Field(ge=1900, le=2100)
    reasons: list[str] = Field(min_length=1, max_length=5)


TOOL = ToolSpec(
    name="record_analysis",
    description="Record the stance and the single numeric claim in the candidate's answer.",
    input_schema={
        "type": "object",
        "properties": {
            "stance": {"type": "string", "enum": ["for", "against", "balanced"]},
            "claim_en": {"type": "string", "description": "The numeric claim, in English."},
            "value": {"type": "number", "description": "The number in the claim."},
            "unit": {"type": "string", "description": "Unit, e.g. percent."},
            "year": {"type": "integer", "description": "Year the figure refers to."},
            "reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-5 short reasons.",
            },
        },
        "required": ["stance", "claim_en", "value", "unit", "year", "reasons"],
    },
)

# Obviously fictional content (integrity rule: no real statistics).
CANDIDATE_ANSWER = (
    "Dekhiye, mera maanna hai ki Exampleland mein tourism ka GDP share lagbhag twelve "
    "percent tha 2023 mein, aur isse rozgaar badhta hai. Lekin over-tourism se local "
    "logon ko problem bhi hoti hai, toh balance chahiye."
)
STREAM_USER = (
    "Ask the candidate one short follow-up question, in Hinglish, about: " + CANDIDATE_ANSWER
)


def synthetic_system_prefix(approx_tokens: int = 1500) -> str:
    """A deterministic, clearly synthetic system prefix long enough to pass the 1K-token
    minimum for a cache checkpoint (approximation: ~4 characters per token)."""
    head = (
        "You are a member of a mock interview board (TEST PROMPT FOR LATENCY PROBING). "
        "Speak briefly and politely. The following numbered rules are filler used only to "
        "make this prefix long enough to be cached.\n"
    )
    lines, i = [head], 1
    while sum(len(x) for x in lines) < approx_tokens * 4:
        lines.append(
            f"Rule {i}: keep each question under thirty words and ask only one thing at a time.\n"
        )
        i += 1
    return "".join(lines)


# ---------------------------------------------------------------------------
# Throttling-aware call helpers
# ---------------------------------------------------------------------------

THROTTLE_CODES = {"ThrottlingException", "throttlingException", "TooManyRequestsException"}


class Throttled(Exception):
    pass


@dataclass
class Counters:
    throttles: int = 0
    other_errors: list[str] = field(default_factory=list)
    # How long each throttled attempt took to fail (request sent -> throttle raised). A
    # throttle that comes back within a few hundred ms could be retried once inside the
    # actor's budget (ruling 17, `actor_fast_throttle_retry`); this is the data for that.
    throttle_ms: list[float] = field(default_factory=list)


def _error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


def call_with_backoff(
    fn, counters: Counters, max_retries: int = 6, base_s: float = 0.5, cap_s: float = 20.0
):
    """Run fn(); on throttling, sleep base*2^attempt with full jitter and retry."""
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            return fn()
        except Throttled:
            pass
        except Exception as exc:
            if _error_code(exc) not in THROTTLE_CODES:
                raise
        counters.throttles += 1
        counters.throttle_ms.append((time.monotonic() - t0) * 1000)
        if attempt == max_retries:
            raise RuntimeError(f"still throttled after {max_retries} retries")
        time.sleep(random.uniform(0, min(cap_s, base_s * 2**attempt)))
    raise AssertionError("unreachable")


@dataclass
class StreamSample:
    ttft_ms: float | None
    total_ms: float
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    text: str = ""
    input_tokens: int = 0
    # Request sent -> the delta that completes the first sentence (., ?, ! or ।). In voice
    # mode the first sentence is what Timbre can start speaking (CTO_NOTES_03 rule N1).
    first_sentence_ms: float | None = None
    stop_reason: str | None = None


_SENTENCE_END = re.compile(r"[.?!।]")


def consume_stream(events, t_req: float, clock=time.monotonic) -> StreamSample:
    """Read a ConverseStream event iterator. Raises Throttled on an in-stream throttle."""
    ttft, first_sentence, stop_reason, parts, usage = None, None, None, [], {}
    for ev in events:
        if "contentBlockDelta" in ev:
            text = ev["contentBlockDelta"].get("delta", {}).get("text")
            if text:
                now = None
                if ttft is None:
                    now = clock()
                    ttft = (now - t_req) * 1000
                parts.append(text)
                if first_sentence is None and _SENTENCE_END.search(text):
                    now = clock() if now is None else now
                    first_sentence = (now - t_req) * 1000
        elif "messageStop" in ev:
            stop_reason = ev["messageStop"].get("stopReason")
        elif "metadata" in ev:
            usage = ev["metadata"].get("usage", {}) or {}
        elif "throttlingException" in ev:
            raise Throttled(ev["throttlingException"].get("message", ""))
        else:
            for k in (
                "serviceUnavailableException",
                "internalServerException",
                "modelStreamErrorException",
                "validationException",
            ):
                if k in ev:
                    raise RuntimeError(f"{k}: {ev[k].get('message', '')}")
    return StreamSample(
        ttft_ms=ttft,
        total_ms=(clock() - t_req) * 1000,
        output_tokens=int(usage.get("outputTokens", 0) or 0),
        cache_read=int(usage.get("cacheReadInputTokens", 0) or 0),
        cache_write=int(usage.get("cacheWriteInputTokens", 0) or 0),
        text="".join(parts),
        input_tokens=int(usage.get("inputTokens", 0) or 0),
        first_sentence_ms=first_sentence,
        stop_reason=stop_reason,
    )


ValidityKind = Literal["valid", "schema_invalid", "no_tool_use"]


def classify_tool_response(response: dict[str, Any]) -> tuple[ValidityKind, str]:
    """Validate a forced-tool Converse response against ProbeAnalysis."""
    payload = BedrockNovaClient.extract_tool_input(response, TOOL.name)
    if payload is None:
        return "no_tool_use", f"stopReason={response.get('stopReason')}"
    try:
        ProbeAnalysis.model_validate(payload)
        return "valid", ""
    except ValidationError as e:
        return "schema_invalid", "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
        )[:300]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


FATAL_CODES = {
    "AccessDeniedException",
    "ValidationException",
    "ResourceNotFoundException",
    "UnrecognizedClientException",
    "ExpiredTokenException",
}


def is_fatal(exc: BaseException) -> bool:
    """Errors that will not go away by retrying: stop the phase instead of repeating N times."""
    from botocore.exceptions import NoCredentialsError, NoRegionError

    return isinstance(exc, NoCredentialsError | NoRegionError) or _error_code(exc) in FATAL_CODES


def make_clients(region: str, model_id: str, read_timeout_s: float, profile: str | None = None):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ProfileNotFound

    try:
        session = boto3.Session(profile_name=profile)
    except ProfileNotFound as exc:
        raise SystemExit(
            f"AWS profile {profile!r} not found. List profiles with `aws configure list-profiles`; "
            "on EC2 leave --profile out so the instance role is used."
        ) from exc
    if session.get_credentials() is None:
        raise SystemExit(
            "No AWS credentials found. On the laptop run `aws configure` (or pass --profile); "
            "on EC2 attach an instance role with bedrock:InvokeModel* permissions."
        )
    raw = session.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=read_timeout_s,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    # Reuse the app's request builder so the probe measures the app's real request shape.
    app = BedrockNovaClient(region=region, model_id=model_id, client=raw)
    return raw, app


def run(
    n: int,
    region: str,
    model_id: str,
    max_tokens: int,
    gap_s: float,
    cache: bool,
    skip_stream: bool,
    skip_tool: bool,
    profile: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    raw, app = make_clients(region, model_id, read_timeout_s=60, profile=profile)
    system = [PromptBlock(synthetic_system_prefix(), cache=cache)]
    sys_blocks = app.system_blocks(system)
    rows, notes = [], []

    if not skip_stream:
        c = Counters()
        samples: list[StreamSample] = []
        req = {
            "modelId": model_id,
            "system": sys_blocks,
            "messages": [{"role": "user", "content": [{"text": STREAM_USER}]}],
            "inferenceConfig": {"temperature": 0.3, "maxTokens": max_tokens},
        }
        for i in range(n):

            def once():
                t_req = time.monotonic()
                resp = raw.converse_stream(**req)
                return consume_stream(resp["stream"], t_req)

            try:
                s = call_with_backoff(once, c)
                samples.append(s)
                print(
                    f"stream {i + 1}/{n}: ttft={fmt_ms(s.ttft_ms)} ms total={s.total_ms:.0f} ms "
                    f"cache_read={s.cache_read} cache_write={s.cache_write}",
                    flush=True,
                )
            except Exception as exc:
                c.other_errors.append(f"{type(exc).__name__}: {exc}"[:200])
                print(f"stream {i + 1}/{n}: ERROR {c.other_errors[-1]}", flush=True)
                if is_fatal(exc):
                    print("non-retryable error: stopping this phase", flush=True)
                    break
            time.sleep(gap_s)
        ttfts = [s.ttft_ms for s in samples if s.ttft_ms is not None]
        totals = [s.total_ms for s in samples]
        rows.append(
            {
                "phase": "ConverseStream",
                "ok": len(samples),
                "of": n,
                "ttft p50 ms": fmt_ms(percentile(ttfts, 50)),
                "ttft p95 ms": fmt_ms(percentile(ttfts, 95)),
                "total p50 ms": fmt_ms(percentile(totals, 50)),
                "total p95 ms": fmt_ms(percentile(totals, 95)),
                "throttles": c.throttles,
                "errors": len(c.other_errors),
                "cache_read_tokens": sum(s.cache_read for s in samples),
                "cache_write_tokens": sum(s.cache_write for s in samples),
                "valid_json": "",
            }
        )
        if samples:
            notes.append(f"Sample streamed reply: {samples[0].text[:200]!r}")
        notes += [f"stream error: {e}" for e in c.other_errors[:3]]

    if not skip_tool:
        c = Counters()
        kinds: dict[str, int] = {"valid": 0, "schema_invalid": 0, "no_tool_use": 0}
        lat: list[float] = []
        cache_read = cache_write = 0
        first_bad = ""
        req = app.build_tool_request(system, CANDIDATE_ANSWER, TOOL, max_tokens=400)
        for i in range(n):

            def once():
                t_req = time.monotonic()
                resp = raw.converse(**req)
                return resp, (time.monotonic() - t_req) * 1000

            try:
                resp, ms = call_with_backoff(once, c)
                kind, why = classify_tool_response(resp)
                kinds[kind] += 1
                lat.append(ms)
                u = resp.get("usage", {}) or {}
                cache_read += int(u.get("cacheReadInputTokens", 0) or 0)
                cache_write += int(u.get("cacheWriteInputTokens", 0) or 0)
                if kind != "valid" and not first_bad:
                    first_bad = f"{kind}: {why}"
                print(f"tool {i + 1}/{n}: {kind} {ms:.0f} ms {why}", flush=True)
            except Exception as exc:
                c.other_errors.append(f"{type(exc).__name__}: {exc}"[:200])
                print(f"tool {i + 1}/{n}: ERROR {c.other_errors[-1]}", flush=True)
                if is_fatal(exc):
                    print("non-retryable error: stopping this phase", flush=True)
                    break
            time.sleep(gap_s)
        done = sum(kinds.values())
        rows.append(
            {
                "phase": "Converse forced tool",
                "ok": done,
                "of": n,
                "ttft p50 ms": "",
                "ttft p95 ms": "",
                "total p50 ms": fmt_ms(percentile(lat, 50)),
                "total p95 ms": fmt_ms(percentile(lat, 95)),
                "throttles": c.throttles,
                "errors": len(c.other_errors),
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "valid_json": (
                    f"{kinds['valid']}/{done} (invalid {kinds['schema_invalid']}, "
                    f"no tool {kinds['no_tool_use']})"
                ),
            }
        )
        if first_bad:
            notes.append(f"First invalid tool answer: {first_bad}")
        notes += [f"tool error: {e}" for e in c.other_errors[:3]]
    return rows, notes


# ---------------------------------------------------------------------------
# --app-prompts: the app's real listener and actor requests (ruling 15)
# ---------------------------------------------------------------------------

# The candidate form used for the actor's board context. Same shape as the app's own example
# session (examples/fake_session.txt); no personal identifiers.
SAMPLE_FORM = {
    "home_state": "Uttar Pradesh",
    "district": "Kanpur Nagar",
    "degree_subject": "Electronics",
    "hobbies": ["chess", "cycling"],
    "interview_language": "hinglish",
}


@dataclass(frozen=True)
class SampleTurn:
    """One realistic turn: the question asked, one final-transcript segment of the answer
    (synthetic Hinglish; every figure is about the fictional Exampleland), and the board
    move the actor must phrase next."""

    topic: str
    question: str
    answer: str
    move: str  # a `Move` value
    member: str  # persona id
    target: str | None = None
    preface: str | None = None
    # For a fact challenge: the earlier claim being revisited and the card it is checked
    # against (a synthetic card from data/cards).
    claim: dict[str, Any] | None = None
    card_id: str | None = None


SAMPLE_TURNS: tuple[SampleTurn, ...] = (
    SampleTurn(
        topic="background",
        question=(
            "Namaste, baithiye. Aap Kanpur Nagar se hain aur Electronics padha hai. Wahan "
            "rehte hue aapne kaunsi ek samasya dekhi, aur use kaise suljhayenge?"
        ),
        answer=(
            "Kanpur mein maine dekha ki small electronics repair shops bahut hain, lekin "
            "skilled training kam hai. Main ITI ke saath tie-up karke repair skilling badhaunga."
        ),
        move="probe",
        member="chairman",
        target="ITI tie-up kaise kaam karega",
    ),
    SampleTurn(
        topic="tourism",
        question="Exampleland ki economy mein tourism ka kya role hai?",
        answer=(
            "Exampleland mein tourism GDP ka 30 percent hai in 2023, isliye tourism hi growth "
            "ka main engine hai and jobs bhi wahi deta hai."
        ),
        move="counter",
        member="economist",
        target="over-dependence on tourism",
    ),
    SampleTurn(
        topic="tourism",
        question="Lekin doosra paksh bhi hai. Over-tourism ke baare mein aap kya kahenge?",
        answer=(
            "Mujhe lagta hai tourism se local logon ko rozgaar milta hai. Lekin environment "
            "par bhi asar padta hai, so carrying capacity rules zaroori hain."
        ),
        move="fact_challenge",
        member="economist",
        target="t1.s0.c0",
        claim={
            "id": "t1.s0.c0",
            "text": "Exampleland mein tourism GDP ka 30 percent hai in 2023",
            "claim_en": "Exampleland tourism share of GDP is 30 percent in 2023",
            "kind": "numeric",
            "value": 30,
            "unit": "percent",
            "year": 2023,
            "turn_id": "t1",
            "segment_id": "s0",
            "turn_index": 1,
        },
        card_id="test_tourism_gdp_share",
    ),
    SampleTurn(
        topic="tourism",
        question=(
            "Aapne 30 percent kaha. Exampleland test data ke 2023 ke figure ke hisaab se yeh "
            "12 percent hai. Aapka figure kahan se hai?"
        ),
        answer=(
            "Haan, shayad mera figure galat tha. Main card wala data check karunga, lekin mera "
            "point ki tourism important hai abhi bhi valid hai."
        ),
        move="probe",
        member="economist",
        target="why tourism still matters",
        preface="acknowledge",
    ),
    SampleTurn(
        topic="tourism",
        question="Theek hai. Homestays ke liye aap kya karenge?",
        answer=(
            "Homestays aur local guides ko train karna chahiye. Saath hi waste management par "
            "cess lagana chahiye."
        ),
        move="move_on",
        member="chairman",
        target="internal_security",
    ),
    SampleTurn(
        topic="internal_security",
        question="Exampleland mein Left-wing extremism ki sthiti kaisi hai?",
        answer=(
            "Exampleland ne 37 districts ko affected list kiya tha in 2024. Community policing "
            "aur development dono saath chalne chahiye, lekin rights ka dhyan rakhna zaroori hai."
        ),
        move="steelman",
        member="ips_officer",
        target="strict security-first view",
    ),
    SampleTurn(
        topic="internal_security",
        question="Intelligence agencies ke beech coordination kaise sudhrega?",
        answer="Mujhe exact pata nahi hai, but I think intelligence sharing improve karni chahiye.",
        move="reassure",
        member="ips_officer",
    ),
)


@dataclass
class AppPromptContext:
    """The app's own prompt kit, personas, cards and settings, built once."""

    kit: Any
    personas: dict[str, Any]
    cards: Any
    settings: Any


def load_app_context(settings: Any) -> AppPromptContext:
    """Build the real persona pack + fact cards prefix exactly as the app does at start-up."""
    from mockboard.brain.cards import load_cards
    from mockboard.brain.personas import load_personas
    from mockboard.brain.prompts import build_prompt_kit, check_prefix_budget

    personas = load_personas()
    cards = load_cards(settings.cards_dir, settings.topics)
    kit = build_prompt_kit(personas, cards, settings.prompt_versions())
    check_prefix_budget(kit.shared_prefix, settings.cache_prefix_token_budget)
    return AppPromptContext(kit=kit, personas=personas, cards=cards, settings=settings)


def listener_request(app: BedrockNovaClient, ctx: AppPromptContext, turn: SampleTurn, k: int):
    """The listener's Converse request for one segment, built by the app's builders (the
    same calls as `listener.analyse_segment` -> `BedrockNovaClient._tool_call`)."""
    from mockboard.brain.listener import build_listener_user
    from mockboard.brain.schemas import Segment
    from mockboard.brain.tools import LISTENER_TOOL

    segment = Segment(segment_id=f"s{k}", segment_index=k, text=turn.answer)
    return app.build_tool_request(
        ctx.kit.system("listener"),
        build_listener_user(segment, turn.question),
        LISTENER_TOOL,
        max_tokens=ctx.settings.structured_max_tokens,
    )


def actor_request(app: BedrockNovaClient, ctx: AppPromptContext, turn: SampleTurn):
    """The actor's ConverseStream request for one move, built by the app's builders (the
    same calls as `actor.compose_line` -> `stream_line` -> `BedrockNovaClient.stream_text`)."""
    from mockboard.brain.actor import (
        build_actor_user,
        count_words,
        language_mix,
        line_system,
        preface_text,
    )
    from mockboard.brain.schemas import BoardMove, CandidateForm, Claim, Move

    s = ctx.settings
    form = CandidateForm(**SAMPLE_FORM)
    persona = ctx.personas[turn.member]
    move = BoardMove(
        move=Move(turn.move),
        target=turn.target,
        member=turn.member,
        preface=turn.preface,
        reason="PROBE: sample move for latency measurement",
        rule_id="PROBE",
    )
    claim = Claim(**turn.claim) if turn.claim else None
    card = ctx.cards.get(turn.card_id) if turn.card_id else None
    if move.move == Move.fact_challenge and (claim is None or card is None):
        raise ValueError(f"sample fact challenge needs a claim and card {turn.card_id!r}")
    # compose_line reserves the code-side acknowledgement's words from the model's cap.
    max_words = s.actor_max_words - count_words(preface_text(move, language_mix(persona, form)))
    topic = turn.target if move.move == Move.move_on and turn.target else turn.topic
    user = build_actor_user(
        persona,
        move,
        topic=topic,
        form=form,
        last_question=turn.question,
        last_answer=turn.answer,
        claim=claim,
        card=card,
    )
    return app.build_stream_request(
        line_system(ctx.kit, max_words=max_words), user, max_tokens=s.actor_max_tokens
    )


ListenerKind = Literal["valid", "schema_invalid", "no_tool_use", "truncated"]


def classify_listener_response(response: dict[str, Any]) -> tuple[ListenerKind, str]:
    """Validate a listener response with the app's own `ListenerOutput` model."""
    from mockboard.brain.tools import LISTENER_TOOL, ListenerOutput

    if response.get("stopReason") == "max_tokens":
        return "truncated", "stopReason=max_tokens"
    payload = BedrockNovaClient.extract_tool_input(response, LISTENER_TOOL.name)
    if not isinstance(payload, dict):
        return "no_tool_use", f"stopReason={response.get('stopReason')}"
    try:
        ListenerOutput.model_validate(payload)
        return "valid", ""
    except ValidationError as e:
        return "schema_invalid", "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
        )[:300]


def _usage(resp: dict[str, Any]) -> dict[str, int]:
    u = resp.get("usage", {}) or {}
    return {
        "input_tokens": int(u.get("inputTokens", 0) or 0),
        "output_tokens": int(u.get("outputTokens", 0) or 0),
        "cache_read": int(u.get("cacheReadInputTokens", 0) or 0),
        "cache_write": int(u.get("cacheWriteInputTokens", 0) or 0),
    }


def _p50_p95(values: list[float]) -> str:
    return f"{fmt_ms(percentile(values, 50))} / {fmt_ms(percentile(values, 95))}"


def _mean(values: list[int]) -> str:
    return "n/a" if not values else f"{sum(values) / len(values):.0f}"


def _throttle_summary(c: Counters) -> str:
    if not c.throttles:
        return "0"
    return (
        f"{c.throttles} (fail after min {min(c.throttle_ms):.0f} / "
        f"p50 {fmt_ms(percentile(c.throttle_ms, 50))} ms)"
    )


def run_app_prompts(
    raw: Any,
    app: BedrockNovaClient,
    ctx: AppPromptContext,
    n: int,
    gap_s: float,
    *,
    skip_listener: bool = False,
    skip_actor: bool = False,
    jsonl_path: Path | None = None,
    backoff_base_s: float = 0.5,
) -> tuple[list[dict[str, Any]], list[str]]:
    """N listener calls and N actor calls with the app's real requests, cycling through
    `SAMPLE_TURNS`. Returns summary rows (one per call kind) and notes."""
    s = ctx.settings
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    per_call: list[dict[str, Any]] = []
    prefix_tokens = ctx.kit.prefix_tokens_approx
    if prefix_tokens < 1000:
        notes.append(
            f"The cached prefix is ~{prefix_tokens} tokens (chars/4 approximation), below "
            "Nova's 1K-token checkpoint minimum, so zero cache reads are expected until real "
            "cards lengthen it."
        )

    if not skip_listener:
        c = Counters()
        kinds: dict[str, int] = {"valid": 0, "schema_invalid": 0, "no_tool_use": 0, "truncated": 0}
        lat: list[float] = []
        toks: dict[str, list[int]] = {
            k: [] for k in ("input_tokens", "output_tokens", "cache_read", "cache_write")
        }
        first_bad = ""
        for i in range(n):
            turn = SAMPLE_TURNS[i % len(SAMPLE_TURNS)]
            req = listener_request(app, ctx, turn, k=i % 3)

            def once(req=req):
                t_req = time.monotonic()
                resp = raw.converse(**req)
                return resp, (time.monotonic() - t_req) * 1000

            throttles_before = c.throttles
            try:
                resp, ms = call_with_backoff(once, c, base_s=backoff_base_s)
            except Exception as exc:
                c.other_errors.append(f"{type(exc).__name__}: {exc}"[:200])
                print(f"listener {i + 1}/{n}: ERROR {c.other_errors[-1]}", flush=True)
                per_call.append({"call": "listener", "i": i, "error": c.other_errors[-1]})
                if is_fatal(exc):
                    print("non-retryable error: stopping this phase", flush=True)
                    break
                time.sleep(gap_s)
                continue
            kind, why = classify_listener_response(resp)
            kinds[kind] += 1
            lat.append(ms)
            u = _usage(resp)
            for k, v in u.items():
                toks[k].append(v)
            if kind != "valid" and not first_bad:
                first_bad = f"{kind}: {why}"
            per_call.append(
                {
                    "call": "listener",
                    "i": i,
                    "move_for": turn.move,
                    "kind": kind,
                    "total_ms": round(ms, 1),
                    "throttles": c.throttles - throttles_before,
                    **u,
                }
            )
            print(
                f"listener {i + 1}/{n}: {kind} total={ms:.0f} ms in={u['input_tokens']} "
                f"out={u['output_tokens']} cache_read={u['cache_read']} "
                f"cache_write={u['cache_write']} {why}",
                flush=True,
            )
            time.sleep(gap_s)
        done = sum(kinds.values())
        fast_read_ms = s.bedrock_fast_read_timeout_s * 1000
        rows.append(
            {
                "call": "listener (app prompt, forced tool)",
                "ok": done,
                "of": n,
                "ttft p50/p95 ms": "",
                "first sentence p50/p95 ms": "",
                "total p50/p95 ms": _p50_p95(lat),
                "mean in/out tokens": (
                    f"{_mean(toks['input_tokens'])} / {_mean(toks['output_tokens'])}"
                ),
                "cache read/write tokens": (
                    f"{sum(toks['cache_read'])} / {sum(toks['cache_write'])}"
                ),
                "valid_json": (
                    f"{kinds['valid']}/{done} (invalid {kinds['schema_invalid']}, no tool "
                    f"{kinds['no_tool_use']}, truncated {kinds['truncated']})"
                ),
                "over app budget": (
                    f"{sum(v > s.move_deadline_ms for v in lat)} > move_deadline "
                    f"{s.move_deadline_ms} ms; {sum(v > fast_read_ms for v in lat)} > fast read "
                    f"timeout {fast_read_ms:.0f} ms"
                ),
                "throttles": _throttle_summary(c),
                "errors": len(c.other_errors),
            }
        )
        if first_bad:
            notes.append(f"First invalid listener answer: {first_bad}")
        notes += [f"listener error: {e}" for e in c.other_errors[:3]]

    if not skip_actor:
        c = Counters()
        samples: list[StreamSample] = []
        for i in range(n):
            turn = SAMPLE_TURNS[i % len(SAMPLE_TURNS)]
            req = actor_request(app, ctx, turn)

            def once(req=req):
                t_req = time.monotonic()
                resp = raw.converse_stream(**req)
                return consume_stream(resp["stream"], t_req)

            throttles_before = c.throttles
            try:
                sm = call_with_backoff(once, c, base_s=backoff_base_s)
            except Exception as exc:
                c.other_errors.append(f"{type(exc).__name__}: {exc}"[:200])
                print(f"actor {i + 1}/{n}: ERROR {c.other_errors[-1]}", flush=True)
                per_call.append({"call": "actor", "i": i, "error": c.other_errors[-1]})
                if is_fatal(exc):
                    print("non-retryable error: stopping this phase", flush=True)
                    break
                time.sleep(gap_s)
                continue
            samples.append(sm)
            per_call.append(
                {
                    "call": "actor",
                    "i": i,
                    "move": turn.move,
                    "ttft_ms": sm.ttft_ms,
                    "first_sentence_ms": sm.first_sentence_ms,
                    "total_ms": round(sm.total_ms, 1),
                    "input_tokens": sm.input_tokens,
                    "output_tokens": sm.output_tokens,
                    "cache_read": sm.cache_read,
                    "cache_write": sm.cache_write,
                    "throttles": c.throttles - throttles_before,
                    "text": sm.text,
                }
            )
            print(
                f"actor {i + 1}/{n} [{turn.move}]: ttft={fmt_ms(sm.ttft_ms)} ms "
                f"first_sentence={fmt_ms(sm.first_sentence_ms)} ms total={sm.total_ms:.0f} ms "
                f"in={sm.input_tokens} out={sm.output_tokens} cache_read={sm.cache_read} "
                f"cache_write={sm.cache_write}",
                flush=True,
            )
            time.sleep(gap_s)
        ttfts = [x.ttft_ms for x in samples if x.ttft_ms is not None]
        firsts = [x.first_sentence_ms for x in samples if x.first_sentence_ms is not None]
        totals = [x.total_ms for x in samples]
        budget_ms = s.actor_first_token_budget_s * 1000
        rows.append(
            {
                "call": "actor (app prompt, streamed)",
                "ok": len(samples),
                "of": n,
                "ttft p50/p95 ms": _p50_p95(ttfts),
                "first sentence p50/p95 ms": (
                    f"{fmt_ms(percentile(firsts, 50))} / {fmt_ms(percentile(firsts, 95))}"
                ),
                "total p50/p95 ms": (
                    f"{fmt_ms(percentile(totals, 50))} / {fmt_ms(percentile(totals, 95))}"
                ),
                "mean in/out tokens": (
                    f"{_mean([x.input_tokens for x in samples])} / "
                    f"{_mean([x.output_tokens for x in samples])}"
                ),
                "cache read/write tokens": (
                    f"{sum(x.cache_read for x in samples)} / {sum(x.cache_write for x in samples)}"
                ),
                "valid_json": "",
                "over app budget": (
                    f"{sum(v > budget_ms for v in ttfts)} > first-token budget {budget_ms:.0f} ms"
                ),
                "throttles": _throttle_summary(c),
                "errors": len(c.other_errors),
            }
        )
        if samples:
            notes.append(f"Sample actor line: {samples[0].text[:200]!r}")
        notes += [f"actor error: {e}" for e in c.other_errors[:3]]

    if jsonl_path is not None and per_call:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("w", encoding="utf-8") as f:
            for row in per_call:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        notes.append(
            f"Per-call rows: {jsonl_path.relative_to(PROBES_DIR.parent)}"
            if jsonl_path.is_relative_to(PROBES_DIR.parent)
            else f"Per-call rows: {jsonl_path}"
        )
    return rows, notes


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "-n",
        type=int,
        default=None,
        help="calls per phase (default 50; 20 with --app-prompts to keep the cost small)",
    )
    p.add_argument(
        "--app-prompts",
        action="store_true",
        help="time the app's real listener (forced tool) and actor (streamed) requests",
    )
    p.add_argument(
        "--region",
        default=None,
        help="Bedrock region (default from settings, "
        "ap-southeast-2); the origin machine can be anywhere",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="named AWS profile (laptop); leave out on EC2 so the instance role is used",
    )
    p.add_argument(
        "--origin",
        default=None,
        help="label for where this runs, e.g. laptop-wsl or ec2-mumbai (default: the host name)",
    )
    p.add_argument("--model-id", default=None, help="default from settings (amazon.nova-pro-v1:0)")
    p.add_argument("--max-tokens", type=int, default=120, help="maxTokens for the streaming phase")
    p.add_argument("--gap-s", type=float, default=0.2, help="pause between calls")
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="omit the cache checkpoint "
        "(synthetic mode only; --app-prompts always sends the app's checkpoint)",
    )
    p.add_argument("--skip-stream", action="store_true", help="skip the streaming / actor phase")
    p.add_argument("--skip-tool", action="store_true", help="skip the forced-tool / listener phase")
    p.add_argument("--no-results", action="store_true", help="do not append to RESULTS.md")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    s = get_settings()
    region, model_id = args.region or s.aws_region, args.model_id or s.nova_model_id
    mode = "app-prompts" if args.app_prompts else "synthetic"
    n = args.n if args.n is not None else (20 if args.app_prompts else 50)
    host = origin_host()
    origin = args.origin or host
    print(
        f"Nova probe ({mode}): model={model_id} region={region} n={n} origin={origin} "
        f"profile={args.profile or '(default chain)'}",
        flush=True,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        if args.app_prompts:
            ctx = load_app_context(s)
            raw, app = make_clients(region, model_id, read_timeout_s=60, profile=args.profile)
            rows, notes = run_app_prompts(
                raw,
                app,
                ctx,
                n,
                args.gap_s,
                skip_listener=args.skip_tool,
                skip_actor=args.skip_stream,
                jsonl_path=RESULTS_DIR / f"nova_app_{stamp}.jsonl",
            )
        else:
            rows, notes = run(
                n,
                region,
                model_id,
                args.max_tokens,
                args.gap_s,
                not args.no_cache,
                args.skip_stream,
                args.skip_tool,
                profile=args.profile,
            )
    except Exception as exc:  # credentials / access problems surface here
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    if not args.no_results:
        params: dict[str, Any] = {
            "mode": mode,
            "origin": origin,
            "host": host,
            "region": region,
            "profile": args.profile,
            "model": model_id,
            "n": n,
        }
        if args.app_prompts:
            params |= {
                "prompt_versions": s.prompt_versions(),
                "card_set_version": ctx.cards.version,
                "cached_prefix_tokens_approx": ctx.kit.prefix_tokens_approx,
                "actor_max_tokens": s.actor_max_tokens,
                "listener_max_tokens": s.structured_max_tokens,
            }
        else:
            params |= {"cache": not args.no_cache, "stream_max_tokens": args.max_tokens}
        title = f"Nova Pro probe ({mode}) from {origin} to {region}"
        print("appended to", append_results(title, rows, notes, params))


if __name__ == "__main__":
    main()
