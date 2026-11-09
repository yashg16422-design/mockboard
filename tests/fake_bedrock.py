"""A fake boto3 `bedrock-runtime` client for tests: the real `BedrockNovaClient` (lanes, thread
pools, error mapping, stream reader) runs on top of it, and nothing leaves the machine.

Replies come from the same rule-based responders as `--fake` mode, wrapped in the Converse /
ConverseStream response shapes. Each call can be delayed with a BLOCKING sleep (so it really
holds a lane thread, like a slow Bedrock call does) or fail with a real botocore exception.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError, ReadTimeoutError

from mockboard.brain.cards import CardSet
from mockboard.brain.fake_responders import fake_responders
from mockboard.brain.tools import (
    AGENDA_TOOL,
    JUDGE_TOOL,
    LIBRARIAN_TOOL,
    LISTENER_TOOL,
    PANEL_LISTENER_TOOL,
    PARTNER_SUMMARY_TOOL,
    REPAIR_TOOL,
)

TOOL_TO_CALL = {
    LISTENER_TOOL.name: "listener",
    PANEL_LISTENER_TOOL.name: "listener",
    AGENDA_TOOL.name: "plan_agenda",
    LIBRARIAN_TOOL.name: "librarian",
    JUDGE_TOOL.name: "judge",
    REPAIR_TOOL.name: "repair",  # likely-misheard words, once at the report (repair.v1)
    PARTNER_SUMMARY_TOOL.name: "partner_summary",  # the study partner's notes (partner_summary.v1)
}
MIDSTREAM = "midstream-throttle"  # a fault value: the stream starts, then Bedrock throttles


def throttling(operation: str = "Converse") -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}, operation
    )


def read_timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url="https://bedrock-runtime.ap-southeast-2.amazonaws.com")


@dataclass
class StubCall:
    name: str
    user: str
    thread: str
    start: float
    end: float | None = None
    fault: str | None = None


Fault = BaseException | str | None


class FakeBedrockRuntime:
    """Stands in for `boto3.client("bedrock-runtime")`.

    `delay(name, user) -> seconds` blocks the calling (lane) thread; `fault(name, user) ->
    exception | MIDSTREAM | None` decides a failure. `user` is the request's user message.
    Both default to "none". Thread-safe.
    """

    def __init__(
        self,
        cards: CardSet,
        *,
        delay: Callable[[str, str], float] | None = None,
        fault: Callable[[str, str], Fault] | None = None,
    ) -> None:
        self._responders = fake_responders(cards)
        self._delay = delay or (lambda name, user: 0.0)
        self._fault = fault or (lambda name, user: None)
        self._lock = threading.Lock()
        self.calls: list[StubCall] = []

    def named(self, name: str) -> list[StubCall]:
        with self._lock:
            return [c for c in self.calls if c.name == name]

    def _begin(self, name: str, user: str) -> tuple[StubCall, Fault]:
        with self._lock:
            fault = self._fault(name, user)
            delay = self._delay(name, user)
            call = StubCall(name, user, threading.current_thread().name, time.monotonic())
            call.fault = None if fault is None else (
                fault if isinstance(fault, str) else type(fault).__name__
            )
            self.calls.append(call)
        if delay:
            time.sleep(delay)
        return call, fault

    @staticmethod
    def _texts(request: dict[str, Any]) -> tuple[str, str]:
        system = "\n\n".join(b["text"] for b in request.get("system", []) if "text" in b)
        user = request["messages"][-1]["content"][0]["text"]
        return system, user

    def converse(self, **request: Any) -> dict[str, Any]:
        tool = request["toolConfig"]["tools"][0]["toolSpec"]["name"]
        name = TOOL_TO_CALL[tool]
        system, user = self._texts(request)
        call, fault = self._begin(name, user)
        try:
            if isinstance(fault, BaseException):
                raise fault
            payload = self._responders[name](system, user)
            return {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"toolUse": {"toolUseId": "x", "name": tool, "input": payload}}
                        ],
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": len(user) // 4, "outputTokens": 50},
            }
        finally:
            call.end = time.monotonic()

    def converse_stream(self, **request: Any) -> dict[str, Any]:
        system, user = self._texts(request)
        opening = "Say the opening line now." in user or "Say the opening question now." in user
        name = "opener" if opening else "actor"
        call, fault = self._begin(name, user)
        if isinstance(fault, BaseException):
            call.end = time.monotonic()
            raise fault
        words = str(self._responders[name](system, user)).split()

        def events() -> Iterator[dict[str, Any]]:
            try:
                for i in range(0, len(words), 3):
                    chunk = " ".join(words[i : i + 3])
                    text = chunk if i == 0 else " " + chunk
                    yield {"contentBlockDelta": {"delta": {"text": text}}}
                    if fault == MIDSTREAM:
                        yield {"throttlingException": {"message": "Too many requests"}}
                        return
                yield {"messageStop": {"stopReason": "end_turn"}}
                yield {"metadata": {"usage": {"inputTokens": len(user) // 4, "outputTokens": 30}}}
            finally:
                call.end = time.monotonic()

        return {"stream": events()}
