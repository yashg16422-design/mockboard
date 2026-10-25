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

Credentials come from the default AWS chain (`aws configure` profile, env vars, or the
EC2 instance role). ThrottlingException is retried with exponential backoff plus jitter,
and every throttle is counted. botocore's own retries are off, so the counts are honest.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mockboard.brain.llm import BedrockNovaClient, PromptBlock, ToolSpec
from mockboard.config import get_settings
from probes.common import append_results, fmt_ms, percentile

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
        try:
            return fn()
        except Throttled:
            pass
        except Exception as exc:
            if _error_code(exc) not in THROTTLE_CODES:
                raise
        counters.throttles += 1
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


def consume_stream(events, t_req: float, clock=time.monotonic) -> StreamSample:
    """Read a ConverseStream event iterator. Raises Throttled on an in-stream throttle."""
    ttft, parts, usage = None, [], {}
    for ev in events:
        if "contentBlockDelta" in ev:
            text = ev["contentBlockDelta"].get("delta", {}).get("text")
            if text:
                if ttft is None:
                    ttft = (clock() - t_req) * 1000
                parts.append(text)
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


def make_clients(region: str, model_id: str, read_timeout_s: float):
    import boto3
    from botocore.config import Config

    if boto3.Session().get_credentials() is None:
        raise SystemExit(
            "No AWS credentials found. On the laptop run `aws configure` (or set AWS_PROFILE); "
            "on EC2 attach an instance role with bedrock:InvokeModel* permissions."
        )
    raw = boto3.client(
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
) -> tuple[list[dict[str, Any]], list[str]]:
    raw, app = make_clients(region, model_id, read_timeout_s=60)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-n", type=int, default=50, help="calls per phase (default 50)")
    p.add_argument("--region", default=None, help="default from settings (ap-southeast-2)")
    p.add_argument("--model-id", default=None, help="default from settings (amazon.nova-pro-v1:0)")
    p.add_argument("--max-tokens", type=int, default=120, help="maxTokens for the streaming phase")
    p.add_argument("--gap-s", type=float, default=0.2, help="pause between calls")
    p.add_argument("--no-cache", action="store_true", help="omit the cache checkpoint")
    p.add_argument("--skip-stream", action="store_true")
    p.add_argument("--skip-tool", action="store_true")
    p.add_argument("--no-results", action="store_true", help="do not append to RESULTS.md")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    s = get_settings()
    region, model_id = args.region or s.aws_region, args.model_id or s.nova_model_id
    print(f"Nova probe: model={model_id} region={region} n={args.n}", flush=True)
    try:
        rows, notes = run(
            args.n,
            region,
            model_id,
            args.max_tokens,
            args.gap_s,
            not args.no_cache,
            args.skip_stream,
            args.skip_tool,
        )
    except Exception as exc:  # credentials / access problems surface here
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    if not args.no_results:
        params = {
            "model": model_id,
            "region": region,
            "n": args.n,
            "cache": not args.no_cache,
            "stream_max_tokens": args.max_tokens,
        }
        print("appended to", append_results("Nova Pro probe", rows, notes, params))


if __name__ == "__main__":
    main()
