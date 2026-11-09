"""Claude as the board's brain, run locally through the person's own Claude Code login (Nov 9).

The Claude Agent SDK starts Claude Code with ONLY Mock Board's tools, served in this process
(`create_sdk_mcp_server`), so Claude drives the same `BoardService` and Gnani voice bridge the
browser is connected to. No built-in tool (no shell, no files, no web), no other MCP server, no
personal settings. Mock Board's code still decides every move; Claude prepares the board, words
the lines, reads the answers and scores the report.

`ClaudeRun.agent` is injectable: tests pass a scripted stand-in, so no Claude call is made there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mockboard.mcp import guidance
from mockboard.mcp.service import BoardService
from mockboard.mcp.tools import TOOLS, call_tool

logger = logging.getLogger("mockboard.claude_room")

SERVER = "mockboard"
# The tools Claude may use here: the voice sitting, the report and its checks. Not the text-only
# starts (the person is in the voice room), not the panel's own ticket.
RUN_TOOLS = (
    "start_voice_interview", "start_voice_partner", "prepare_board", "say", "wait_for_answer",
    "next_turn", "end_session", "check_scores", "check_notes",
)
MAX_TURNS = 400  # one sitting is about fifteen answers, three or four tool calls each
DEFAULT_MODEL = "opus"  # the strongest model of the person's plan; MOCKBOARD_CLAUDE_MODEL overrides

Notify = Callable[[str, dict[str, Any], dict[str, Any], bool], Awaitable[None]]
Call = Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any], bool]]]
Agent = Callable[[str, Any, Call], AsyncIterator[Any]]


def system_prompt() -> str:
    return "\n".join([
        guidance.voice_prompt(),
        "",
        "In this app the person is already in Mock Board's voice room in their browser. They "
        "cannot see this conversation: everything they hear and see goes through `say` and the "
        "room. So do not write messages for them, and never ask them anything in text.",
        "Their form (or study topic) is in the first message: call the start tool with it as "
        "given, then prepare_board with a careful, specific plan, then run the sitting with say, "
        "wait_for_answer and next_turn until `finished`.",
        "Be the toughest fair board you can be: in your own words, make each question specific "
        "to what the person just said, name a gap plainly when Mock Board reports one, and never "
        "praise a weak answer. Keep every line within its max_words.",
        "Use `proposal` on next_turn to lead: dig, elaborate, confront (with their exact words) or "
        "pivot when you judge it right; Mock Board rules on each and the board speaks its move.",
        "When the sitting is finished, call end_session, then score it with check_scores "
        "(interview) or check_notes (study partner), quoting the person word for word. Then stop.",
    ])


def first_message(mode: str, payload: dict[str, Any]) -> str:
    if mode == "partner":
        return ("Start a spoken study session with Mitra (start_voice_partner) on this topic, in "
                f"this language: {json.dumps(payload, ensure_ascii=False)}")
    return ("Start a spoken UPSC interview (start_voice_interview) with this application form: "
            f"{json.dumps(payload, ensure_ascii=False)}")


def make_call(service: BoardService, notify: Notify) -> Call:
    """One tool call, then the notice. Claude's tools and the tests' stand-in share it."""

    async def call(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if name not in RUN_TOOLS:
            return {"error": f"{name} is not available here"}, True
        result, is_error = await call_tool(service, name, args)
        with contextlib.suppress(Exception):
            await notify(name, args, result, is_error)
        return result, is_error

    return call


def build_server(call: Call) -> Any:
    from claude_agent_sdk import create_sdk_mcp_server, tool

    by_name = {t["name"]: t for t in TOOLS}

    def make(name: str) -> Any:
        spec = by_name[name]

        @tool(name, spec["description"], spec["inputSchema"])
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            result, is_error = await call(name, args)
            return {"content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False)}],
                    "is_error": is_error}

        return handler

    return create_sdk_mcp_server(SERVER, "1.0.0", [make(n) for n in RUN_TOOLS])


def agent_options(server: Any) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=system_prompt(),
        mcp_servers={SERVER: server},
        strict_mcp_config=True,  # none of the person's other MCP servers
        tools=[],  # no built-in tool: no shell, no files, no web
        allowed_tools=[f"mcp__{SERVER}__{n}" for n in RUN_TOOLS],
        setting_sources=[],  # none of the person's own settings or memory files
        max_turns=MAX_TURNS,
        model=os.environ.get("MOCKBOARD_CLAUDE_MODEL", DEFAULT_MODEL),
        cwd=tempfile.gettempdir(),
    )


async def claude_agent(prompt: str, options: Any, call: Call) -> AsyncIterator[Any]:
    from claude_agent_sdk import ClaudeSDKClient

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            yield message


def message_notes(message: Any) -> list[str]:
    """Claude's short visible notes in a message (text blocks only, never tool payloads)."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    if not isinstance(message, AssistantMessage):
        return []
    return [b.text.strip() for b in message.content
            if isinstance(b, TextBlock) and b.text.strip()]


@dataclass
class ClaudeRun:
    """One sitting run by Claude. `notify` hears every tool call (to find the room and report)."""

    service: BoardService
    mode: str
    payload: dict[str, Any]
    notify: Notify
    on_note: Callable[[str], Awaitable[None]]
    agent: Agent = claude_agent
    status: str = "starting"
    error: str = ""
    session_id: str | None = None
    task: asyncio.Task | None = None
    notes: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async def tell(name: str, args: dict, result: dict, is_error: bool) -> None:
            if not is_error and isinstance(result, dict) and result.get("session_id"):
                self.session_id = self.session_id or result["session_id"]
            await self.notify(name, args, result, is_error)

        try:
            call = make_call(self.service, tell)
            self.status = "running"
            async for message in self.agent(first_message(self.mode, self.payload),
                                            agent_options(build_server(call)), call):
                for note in message_notes(message):
                    self.notes.append(note)
                    await self.on_note(note)
            self.status = "done"
        except asyncio.CancelledError:
            self.status = "stopped"
            raise
        except Exception as exc:  # shown to the person plainly; details only in the log
            logger.exception("claude run failed")
            self.status, self.error = "error", f"{type(exc).__name__}: {exc}"[:300]
            await self.on_note("Claude could not continue: " + self.error)

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
