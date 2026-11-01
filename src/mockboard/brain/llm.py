"""The only module that talks to a language model (and the only one that imports boto3).

Two implementations share one protocol:

* `BedrockNovaClient` calls Amazon Nova Pro through the Bedrock Converse / ConverseStream API.
  Structured outputs use forced tool use (`toolChoice = {"tool": {"name": ...}}`) at
  temperature 0, and the tool input is validated with Pydantic, retried once, then a typed
  `LLMOutputError` is raised (the caller turns that into a safe default).
* `FakeLLMClient` returns scripted or rule-based responses, so every test and the
  `--fake` CLI run offline.

Every call produces a `CallStats` record (latency, time to first token, token counts
including prompt-cache reads/writes, attempts, lane), which the trace recorder stores.

Execution lanes (CTO_NOTES_02 M5). boto3 is synchronous, so every Bedrock call holds a
worker thread for its whole duration (a streamed actor line holds one until its last token).
With one shared pool, a burst of fact checks could make the actor's call wait for a free
thread: priority inversion on the one number we publish. So every call runs on one of three
lanes, chosen from the call's name (`lane_for`), and each lane has its own small thread pool
and its own boto3 client (own connection pool, own timeouts, own retry setting):

* `actor`      - the spoken line and the opener: the critical path.
* `fast`       - the fast analysis graph's listener: the only work between end of turn and
                 the actor.
* `background` - fact checks (librarian) and the judge: never awaited by a move.

Think of three checkout counters in a shop: a queue of trolleys at the "background" counter
never holds up the express counter.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

MAX_CACHE_POINTS = 4  # Bedrock prompt-caching guide: at most 4 checkpoints per request.
FORBIDDEN_SCHEMA_KEYS = {"$ref", "$defs", "definitions", "anyOf", "oneOf", "allOf", "not"}


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------


class Lane(StrEnum):
    actor = "actor"
    fast = "fast"
    background = "background"


# Call name -> lane. A name not listed here goes to the background lane, so a new kind of
# call can never take a critical-path thread by accident.
LANE_BY_CALL: dict[str, Lane] = {
    "actor": Lane.actor,
    "opener": Lane.actor,
    "listener": Lane.fast,
    "librarian": Lane.background,
    "judge": Lane.background,
}


def is_offline_llm(llm: object) -> bool:
    """True for the offline fake LLM (and wrappers around it), False for Bedrock."""
    return bool(getattr(llm, "offline", False))


def lane_for(name: str) -> Lane:
    return LANE_BY_CALL.get(name, Lane.background)


@dataclass(frozen=True)
class LaneConfig:
    """One lane's thread pool and botocore client settings.

    `max_attempts` is botocore's `total_max_attempts` for this lane's client. It is 1 on
    every lane: the fast and background lanes are retried by a LangGraph `RetryPolicy`
    (so attempts never multiply across layers), and the actor lane is not retried by
    botocore at all (ruling 14: a failed actor call is answered with a cached fallback
    line). The only actor retry is the optional fast-throttle retry in `actor.py`
    (ruling 17, `actor_fast_throttle_retry`, off by default).
    """

    workers: int
    connect_timeout_s: float
    read_timeout_s: float
    max_attempts: int = 1


def lane_configs(settings: Any) -> dict[Lane, LaneConfig]:
    """Per-lane pool sizes, timeouts and retries from Settings (ruling 13: the actor and fast
    lanes get short timeouts; the background lane keeps the long ones)."""
    return {
        Lane.actor: LaneConfig(
            workers=settings.bedrock_actor_workers,
            connect_timeout_s=settings.bedrock_actor_connect_timeout_s,
            read_timeout_s=settings.bedrock_actor_read_timeout_s,
            max_attempts=1,  # ruling 14: one attempt; failure -> persona fallback line
        ),
        Lane.fast: LaneConfig(
            workers=settings.bedrock_fast_workers,
            connect_timeout_s=settings.bedrock_fast_connect_timeout_s,
            read_timeout_s=settings.bedrock_fast_read_timeout_s,
        ),
        Lane.background: LaneConfig(
            workers=settings.bedrock_background_workers,
            connect_timeout_s=settings.bedrock_connect_timeout_s,
            read_timeout_s=settings.bedrock_read_timeout_s,
        ),
    }


# ---------------------------------------------------------------------------
# Request / result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptBlock:
    """One block of system prompt. `cache=True` places a cache checkpoint after it."""

    text: str
    cache: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """A forced tool whose input schema is the structured output we want."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        assert_flat_schema(self.input_schema)


@dataclass
class CallStats:
    """Timing and usage for one model call. Times are `time.monotonic()` seconds."""

    name: str
    model_id: str
    tag: str = ""
    started: float = field(default_factory=time.monotonic)
    first_token: float | None = None
    ended: float | None = None
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None
    error: str | None = None
    fallback: bool = False
    lane: str = ""  # filled from the call name when not given

    def __post_init__(self) -> None:
        self.lane = self.lane or lane_for(self.name).value

    @property
    def latency_ms(self) -> float | None:
        return None if self.ended is None else (self.ended - self.started) * 1000

    @property
    def ttft_ms(self) -> float | None:
        return None if self.first_token is None else (self.first_token - self.started) * 1000

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        if not usage:
            return
        self.input_tokens += int(usage.get("inputTokens", 0) or 0)
        self.output_tokens += int(usage.get("outputTokens", 0) or 0)
        self.cache_read_tokens += int(usage.get("cacheReadInputTokens", 0) or 0)
        self.cache_write_tokens += int(usage.get("cacheWriteInputTokens", 0) or 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tag": self.tag,
            "lane": self.lane,
            "model_id": self.model_id,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "ttft_ms": None if self.ttft_ms is None else round(self.ttft_ms, 1),
            "attempts": self.attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "stop_reason": self.stop_reason,
            "error": self.error,
        }


CallSink = Callable[[CallStats], None]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for every model-call failure."""


class LLMTransientError(LLMError):
    """Throttling, timeouts and 5xx errors. Safe to retry (graph RetryPolicy does so)."""


class LLMThrottledError(LLMTransientError):
    """Bedrock's ThrottlingException (before or inside a stream). A subclass, so every
    `LLMTransientError` handler still catches it; the actor's optional fast-throttle retry
    (ruling 17) needs to tell a throttle apart from a timeout."""


class LLMOutputError(LLMError):
    """The model's tool input failed validation twice (first try plus one retry)."""

    def __init__(self, message: str, raw: Any = None, errors: str = "") -> None:
        super().__init__(message)
        self.raw = raw
        self.errors = errors


class LLMTruncatedError(LLMOutputError):
    """The model hit `maxTokens` (stopReason "max_tokens") even after one retry with a larger
    limit, so its tool input is incomplete. Not transient: retrying the same limit would
    truncate again."""


class LLMConfigError(LLMError):
    """Non-retryable request problems: access denied, bad model id, validation errors."""


def is_transient(exc: BaseException) -> bool:
    """Retry predicate for LangGraph `RetryPolicy(retry_on=...)`."""
    return isinstance(exc, LLMTransientError)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


TOP_LEVEL_SCHEMA_KEYS = {"type", "properties", "required"}


def assert_flat_schema(schema: Mapping[str, Any]) -> None:
    """Enforce the 'flat schema' rule for Nova tool inputs.

    Amazon Nova's "Troubleshooting tool calls" page (read Oct 25, 2026): the top level must be
    an object with only `type` ("object"), `properties` and `required`; it names `$schema`,
    `description`, `title` and `additionalProperties` as common unsupported top-level fields.
    We also forbid `$ref`/`$defs`/`anyOf`/`oneOf`/`allOf`/`not` and type unions anywhere, and
    require every object's `required` names to exist in its `properties`, so a typo cannot
    make the model chase a field the schema never defines. Array items may be objects of
    primitive fields (one level), which keeps schemas readable.
    """
    extra = set(schema) - TOP_LEVEL_SCHEMA_KEYS
    if extra:
        raise ValueError(f"tool schema top level has unsupported keys: {sorted(extra)}")
    if schema.get("type") != "object":
        raise ValueError("tool schema top level must be type=object")
    if not isinstance(schema.get("properties"), Mapping) or not schema["properties"]:
        raise ValueError("tool schema top level needs a non-empty properties object")

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            bad = FORBIDDEN_SCHEMA_KEYS & set(node)
            if bad:
                raise ValueError(f"tool schema uses {sorted(bad)} at {path}")
            if isinstance(node.get("type"), list):
                raise ValueError(f"tool schema uses a type union at {path}")
            props = node.get("properties")
            required = node.get("required")
            if node.get("type") == "object" and required is not None:
                if not isinstance(required, list) or not all(
                    isinstance(r, str) for r in required
                ):
                    raise ValueError(f"tool schema `required` must be a list of names at {path}")
                missing = set(required) - set(props or {})
                if missing:
                    raise ValueError(
                        f"tool schema requires undefined properties {sorted(missing)} at {path}"
                    )
            for k, v in node.items():
                # Inside `properties`, keys are field names, so a field literally called
                # "not" or "description" is fine; only its sub-schema is walked.
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(schema, "$")


def system_text(blocks: Sequence[PromptBlock]) -> str:
    return "\n\n".join(b.text for b in blocks)


def approx_tokens(text: str) -> int:
    """APPROXIMATION: tokens ~= characters / 4. Good enough for budget guards, not billing."""
    return (len(text) + 3) // 4


# ---------------------------------------------------------------------------
# Protocol and shared validation logic
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    model_id: str

    async def structured(
        self,
        *,
        name: str,
        system: Sequence[PromptBlock],
        user: str,
        tool: ToolSpec,
        output_model: type[T],
        tag: str = "",
        max_tokens: int | None = None,
    ) -> T: ...

    def stream_text(
        self,
        *,
        name: str,
        system: Sequence[PromptBlock],
        user: str,
        max_tokens: int,
        tag: str = "",
        temperature: float = 0.3,
    ) -> AsyncIterator[str]: ...


class _BaseClient:
    """Implements validate -> retry once -> raise, shared by the real and fake clients."""

    model_id: str = "unknown"
    default_max_tokens: int = 1200
    # When a structured call is truncated (stopReason "max_tokens"), it is retried once
    # with double the limit, but never above this ceiling.
    # The Nova Pro model card gives a 10K-token output maximum (CTO_NOTES_03 §1), so this
    # conservative 5000 ceiling is safe; raise it only with a reason.
    max_tokens_ceiling: int = 5000

    def __init__(self, on_call: CallSink | None = None) -> None:
        self._sinks: list[CallSink] = [on_call] if on_call else []

    def add_sink(self, sink: CallSink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: CallSink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def _emit(self, stats: CallStats) -> None:
        stats.ended = stats.ended or time.monotonic()
        for sink in list(self._sinks):
            sink(stats)

    async def _tool_call(
        self,
        system: Sequence[PromptBlock],
        user: str,
        tool: ToolSpec,
        max_tokens: int,
        stats: CallStats,
    ) -> Any:
        raise NotImplementedError

    async def structured(
        self,
        *,
        name: str,
        system: Sequence[PromptBlock],
        user: str,
        tool: ToolSpec,
        output_model: type[T],
        tag: str = "",
        max_tokens: int | None = None,
    ) -> T:
        stats = CallStats(name=name, model_id=self.model_id, tag=tag)
        prompt = user
        raw: Any = None
        errors = ""
        limit = max_tokens or self.default_max_tokens
        try:
            for attempt in (1, 2):
                stats.attempts = attempt
                raw = await self._tool_call(system, prompt, tool, limit, stats)
                if stats.stop_reason == "max_tokens":
                    # Truncated JSON is not a validation problem the model can fix by
                    # being told; only a larger limit helps. Retry once with one.
                    bigger = min(limit * 2, self.max_tokens_ceiling)
                    if attempt == 2 or bigger <= limit:
                        stats.error = "truncated"
                        raise LLMTruncatedError(
                            f"{name}: tool output truncated at maxTokens={limit} "
                            f"(stopReason max_tokens); raise the limit for this call",
                            raw=raw,
                            errors="truncated",
                        )
                    limit = bigger
                    prompt = user
                    continue
                try:
                    return output_model.model_validate(raw)
                except ValidationError as exc:
                    errors = _short_errors(exc)
                    # At temperature 0 an identical retry tends to repeat itself, so the
                    # retry tells the model what was wrong.
                    prompt = (
                        f"{user}\n\nYour previous `{tool.name}` input was invalid: {errors}. "
                        f"Call `{tool.name}` again with input that matches its schema exactly."
                    )
            stats.error = "output_invalid"
            raise LLMOutputError(f"{name}: invalid tool input after retry", raw=raw, errors=errors)
        except LLMError as exc:
            stats.error = stats.error or type(exc).__name__
            raise
        finally:
            self._emit(stats)


def _short_errors(exc: ValidationError, limit: int = 4) -> str:
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Bedrock / Nova Pro
# ---------------------------------------------------------------------------

_THROTTLE_CODES = {"ThrottlingException", "throttlingException"}
_TRANSIENT_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelNotReadyException",
    "ModelTimeoutException",
    "throttlingException",
    "serviceUnavailableException",
    "internalServerException",
    "modelStreamErrorException",
}


def _map_boto_error(exc: Exception) -> LLMError:
    """Translate botocore exceptions into our typed errors."""
    from botocore.exceptions import (  # local import keeps boto3 optional for the fake path
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        EventStreamError,
        ReadTimeoutError,
    )

    if isinstance(exc, ReadTimeoutError | ConnectTimeoutError | EndpointConnectionError):
        return LLMTransientError(f"timeout/connection: {exc}")
    if isinstance(exc, ConnectionClosedError):
        return LLMTransientError(f"connection closed: {exc}")
    if isinstance(exc, ClientError | EventStreamError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE_CODES:
            return LLMThrottledError(f"{code}: {exc}")
        if code in _TRANSIENT_CODES:
            return LLMTransientError(f"{code}: {exc}")
        return LLMConfigError(f"{code}: {exc}")
    return LLMError(f"{type(exc).__name__}: {exc}")


DEFAULT_LANE_WORKERS = {Lane.actor: 4, Lane.fast: 4, Lane.background: 8}


class BedrockNovaClient(_BaseClient):
    """Amazon Nova Pro over the Bedrock Converse API (credentials: default AWS chain).

    One boto3 client and one thread pool per lane (see the module docstring). `client=`
    (tests and probes) uses one stub for every lane; `clients=` gives one per lane.
    """

    def __init__(
        self,
        *,
        region: str,
        model_id: str,
        connect_timeout_s: float = 5.0,
        read_timeout_s: float = 30.0,
        tool_stop_sequences: Sequence[str] = ("</tool>",),
        tool_top_k: int | None = 1,
        default_max_tokens: int = 1200,
        max_tokens_ceiling: int = 5000,
        on_call: CallSink | None = None,
        client: Any = None,
        lanes: Mapping[Lane, LaneConfig] | None = None,
        max_pool_connections: int = 16,
        clients: Mapping[Lane, Any] | None = None,
    ) -> None:
        super().__init__(on_call)
        self.model_id = model_id
        self.default_max_tokens = default_max_tokens
        self.max_tokens_ceiling = max_tokens_ceiling
        self._tool_stop = list(tool_stop_sequences)
        self._tool_top_k = tool_top_k
        # Without explicit lanes, every lane uses the given timeouts and no botocore retry.
        self.lanes: dict[Lane, LaneConfig] = {
            lane: (lanes or {}).get(
                lane, LaneConfig(DEFAULT_LANE_WORKERS[lane], connect_timeout_s, read_timeout_s)
            )
            for lane in Lane
        }
        self._pools = {
            lane: ThreadPoolExecutor(
                max_workers=cfg.workers, thread_name_prefix=f"bedrock-{lane.value}"
            )
            for lane, cfg in self.lanes.items()
        }
        if clients is not None:
            self._clients = {lane: clients[lane] for lane in Lane}
        elif client is not None:
            self._clients = dict.fromkeys(Lane, client)
        else:
            self._clients = {
                lane: self._boto_client(region, cfg, max_pool_connections)
                for lane, cfg in self.lanes.items()
            }

    @staticmethod
    def _boto_client(region: str, cfg: LaneConfig, max_pool_connections: int) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=cfg.connect_timeout_s,
                read_timeout=cfg.read_timeout_s,
                # 1 on every lane: the graph lanes retry in exactly one place (LangGraph
                # RetryPolicy), and the actor lane never retries (ruling 14).
                retries={"total_max_attempts": cfg.max_attempts, "mode": "standard"},
                max_pool_connections=max(max_pool_connections, cfg.workers),
            ),
        )

    @classmethod
    def from_settings(cls, settings: Any, on_call: CallSink | None = None) -> BedrockNovaClient:
        return cls(
            region=settings.aws_region,
            model_id=settings.nova_model_id,
            connect_timeout_s=settings.bedrock_connect_timeout_s,
            read_timeout_s=settings.bedrock_read_timeout_s,
            tool_stop_sequences=settings.nova_tool_stop_sequences,
            tool_top_k=settings.nova_tool_top_k,
            default_max_tokens=settings.structured_max_tokens,
            max_tokens_ceiling=settings.structured_max_tokens_ceiling,
            on_call=on_call,
            lanes=lane_configs(settings),
            max_pool_connections=settings.bedrock_max_pool_connections,
        )

    def client_for(self, lane: Lane) -> Any:
        return self._clients[lane]

    def pool_for(self, lane: Lane) -> ThreadPoolExecutor:
        return self._pools[lane]

    def close(self) -> None:
        """Stop the lane pools (running calls finish; nothing new starts)."""
        for pool in self._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def system_blocks(blocks: Sequence[PromptBlock]) -> list[dict[str, Any]]:
        """Render system blocks, inserting a cache checkpoint after each `cache=True` block.

        Syntax `{"cachePoint": {"type": "default"}}` (no `ttl`) follows the Bedrock prompt
        caching guide, which lists Nova Pro as supporting checkpoints in `system` and
        `messages` (not `tools`), a 1K-token minimum per checkpoint and a 20K-token maximum.
        # UNVERIFIED: whether Nova still reuses the cached system prefix when a toolConfig is
        # present (Nova injects tool definitions into its own prompt). nova_probe.py logs
        # cacheReadInputTokens so the owner can confirm on day 1.
        """
        out: list[dict[str, Any]] = []
        points = 0
        for block in blocks:
            out.append({"text": block.text})
            if block.cache:
                points += 1
                out.append({"cachePoint": {"type": "default"}})
        if points > MAX_CACHE_POINTS:
            raise ValueError(f"{points} cache points; Bedrock allows at most {MAX_CACHE_POINTS}")
        return out

    def build_tool_request(
        self, system: Sequence[PromptBlock], user: str, tool: ToolSpec, max_tokens: int
    ) -> dict[str, Any]:
        """The exact Converse request for a forced-tool structured call (testable offline)."""
        inference: dict[str, Any] = {"temperature": 0.0, "maxTokens": max_tokens}
        if self._tool_stop:
            # Nova's "Choosing a tool" page recommends `</tool>` whenever a tool choice is set.
            inference["stopSequences"] = self._tool_stop
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "system": self.system_blocks(system),
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": tool.name}},
            },
            "inferenceConfig": inference,
        }
        if self._tool_top_k is not None:
            # Greedy decoding for tool calls. Placement copied from Amazon Nova's
            # "Troubleshooting tool calls" page (read Oct 25, 2026), which shows
            # `additionalModelRequestFields={"inferenceConfig": {"topK": 1}}` next to
            # temperature 0: topK is NOT a Converse `inferenceConfig` field.
            request["additionalModelRequestFields"] = {
                "inferenceConfig": {"topK": self._tool_top_k}
            }
        return request

    def build_stream_request(
        self,
        system: Sequence[PromptBlock],
        user: str,
        max_tokens: int,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """The exact ConverseStream request for a streamed text call (the actor and opener).
        Testable offline, and reused by `probes/nova_probe.py --app-prompts`."""
        return {
            "modelId": self.model_id,
            "system": self.system_blocks(system),
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }

    @staticmethod
    def extract_tool_input(response: Mapping[str, Any], tool_name: str) -> Any:
        """Return the forced tool's input dict from a Converse response, or None."""
        content = response.get("output", {}).get("message", {}).get("content", []) or []
        for block in content:
            tool_use = block.get("toolUse") if isinstance(block, Mapping) else None
            if tool_use and tool_use.get("name") == tool_name:
                payload = tool_use.get("input")
                if isinstance(payload, str):  # defensive: some SDKs hand back a JSON string
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        return payload
                return payload
        return None

    async def _tool_call(
        self,
        system: Sequence[PromptBlock],
        user: str,
        tool: ToolSpec,
        max_tokens: int,
        stats: CallStats,
    ) -> Any:
        request = self.build_tool_request(system, user, tool, max_tokens)
        lane = Lane(stats.lane)
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                self._pools[lane], functools.partial(self._clients[lane].converse, **request)
            )
        except Exception as exc:
            raise _map_boto_error(exc) from exc
        stats.first_token = stats.first_token or time.monotonic()
        stats.add_usage(response.get("usage"))
        stats.stop_reason = response.get("stopReason")
        return self.extract_tool_input(response, tool.name)

    async def stream_text(
        self,
        *,
        name: str,
        system: Sequence[PromptBlock],
        user: str,
        max_tokens: int,
        tag: str = "",
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """Stream plain text deltas from ConverseStream.

        boto3 is synchronous, so a worker thread reads the event stream and hands events to
        the event loop through a queue. If the consumer stops early (for example after the
        30-word cap), the stream is closed so the thread exits.
        """
        stats = CallStats(name=name, model_id=self.model_id, tag=tag, attempts=1)
        request = self.build_stream_request(system, user, max_tokens, temperature)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        holder: dict[str, Any] = {}
        stop = False
        lane = Lane(stats.lane)
        client = self._clients[lane]

        def worker() -> None:
            try:
                response = client.converse_stream(**request)
                holder["stream"] = response["stream"]
                for event in response["stream"]:
                    if stop:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        thread_future = loop.run_in_executor(self._pools[lane], worker)
        try:
            while True:
                kind, item = await queue.get()
                if kind == "done":
                    break
                if kind == "error":
                    mapped = _map_boto_error(item)
                    stats.error = type(mapped).__name__
                    raise mapped from item
                if "contentBlockDelta" in item:
                    text = item["contentBlockDelta"].get("delta", {}).get("text")
                    if text:
                        if stats.first_token is None:
                            stats.first_token = time.monotonic()
                        yield text
                elif "messageStop" in item:
                    stats.stop_reason = item["messageStop"].get("stopReason")
                elif "metadata" in item:
                    stats.add_usage(item["metadata"].get("usage"))
                else:
                    for key in ("throttlingException", "serviceUnavailableException",
                                "internalServerException", "modelStreamErrorException",
                                "validationException"):
                        if key in item:
                            stats.error = key
                            msg = item[key].get("message", key)
                            if key == "validationException":
                                raise LLMConfigError(msg)
                            if key == "throttlingException":
                                raise LLMThrottledError(f"{key}: {msg}")
                            raise LLMTransientError(msg)
        except asyncio.CancelledError:
            # The caller gave up waiting (the actor's first-token budget, ruling 14).
            stats.error = stats.error or "cancelled"
            raise
        finally:
            stop = True
            stream = holder.get("stream")
            if stream is not None and hasattr(stream, "close"):
                with contextlib.suppress(Exception):  # best-effort cleanup
                    stream.close()
            self._emit(stats)
            # Do not await the worker here: closing the stream unblocks it.
            thread_future.add_done_callback(lambda f: f.exception())


# ---------------------------------------------------------------------------
# Fake client (tests and offline mode)
# ---------------------------------------------------------------------------

Responder = Callable[[str, str], Any]
"""A rule-based fake: (system_text, user_text) -> tool payload dict, or text for streams."""

ScriptItem = Any
"""A scripted reply: a payload dict, a string (for streams), an Exception to raise, or a
callable taking (system_text, user_text)."""


@dataclass
class FakeCall:
    name: str
    system: str
    user: str
    tag: str
    max_tokens: int | None = None


@dataclass(frozen=True)
class FakeToolReply:
    """A scripted tool reply with an explicit stop reason, e.g. a truncated output:
    `FakeToolReply({"stance": "half"}, stop_reason="max_tokens")`."""

    payload: Any
    stop_reason: str = "tool_use"


class FakeLLMClient(_BaseClient):
    """Scripted responses keyed by call name ("listener", "librarian", "actor", "judge").

    For each call, the next scripted item for that name is used; if the script for that
    name is empty, the responder for that name is used; otherwise a `LookupError` is raised
    so tests notice unexpected calls. Token counts are approximations (chars/4).
    """

    model_id = "fake-llm"
    # Ruling 36: an offline run. Synthetic test cards are allowed into prompts only when the
    # LLM says this (wrappers that delegate attribute lookups to a fake inherit it).
    offline = True

    def __init__(
        self,
        script: Mapping[str, Sequence[ScriptItem]] | None = None,
        responders: Mapping[str, Responder] | None = None,
        on_call: CallSink | None = None,
        stream_chunk_words: int = 3,
    ) -> None:
        super().__init__(on_call)
        self._script: dict[str, deque[ScriptItem]] = {
            k: deque(v) for k, v in (script or {}).items()
        }
        self._responders = dict(responders or {})
        self._chunk_words = stream_chunk_words
        self.calls: list[FakeCall] = []

    def push(self, name: str, *items: ScriptItem) -> None:
        self._script.setdefault(name, deque()).extend(items)

    def calls_named(self, name: str) -> list[FakeCall]:
        return [c for c in self.calls if c.name == name]

    def _next(self, name: str, system: str, user: str) -> Any:
        queue = self._script.get(name)
        if queue:
            item = queue.popleft()
        elif name in self._responders:
            item = self._responders[name]
        else:
            raise LookupError(f"FakeLLMClient: no scripted reply or responder for {name!r}")
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(system, user)
        return item

    async def _tool_call(
        self,
        system: Sequence[PromptBlock],
        user: str,
        tool: ToolSpec,
        max_tokens: int,
        stats: CallStats,
    ) -> Any:
        sys_text = system_text(system)
        self.calls.append(FakeCall(stats.name, sys_text, user, stats.tag, max_tokens))
        await asyncio.sleep(0)
        payload = self._next(stats.name, sys_text, user)
        stop_reason = "tool_use"
        if isinstance(payload, FakeToolReply):
            payload, stop_reason = payload.payload, payload.stop_reason
        stats.first_token = stats.first_token or time.monotonic()
        stats.add_usage(
            {
                "inputTokens": approx_tokens(user),
                "outputTokens": approx_tokens(json.dumps(payload, default=str)),
                "cacheReadInputTokens": approx_tokens(sys_text),
            }
        )
        stats.stop_reason = stop_reason
        return payload

    async def stream_text(
        self,
        *,
        name: str,
        system: Sequence[PromptBlock],
        user: str,
        max_tokens: int,
        tag: str = "",
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        stats = CallStats(name=name, model_id=self.model_id, tag=tag, attempts=1)
        sys_text = system_text(system)
        self.calls.append(FakeCall(name, sys_text, user, tag, max_tokens))
        try:
            reply = self._next(name, sys_text, user)
            words = str(reply).split()
            for i in range(0, len(words), self._chunk_words):
                await asyncio.sleep(0)
                if stats.first_token is None:
                    stats.first_token = time.monotonic()
                chunk = " ".join(words[i : i + self._chunk_words])
                yield chunk if i == 0 else " " + chunk
            stats.add_usage(
                {"inputTokens": approx_tokens(user), "outputTokens": approx_tokens(str(reply))}
            )
            stats.stop_reason = "end_turn"
        except LLMError as exc:
            stats.error = type(exc).__name__
            raise
        except asyncio.CancelledError:
            stats.error = "cancelled"
            raise
        finally:
            self._emit(stats)
