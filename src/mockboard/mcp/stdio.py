"""A small MCP server over stdio, with the standard library only (CTO_NOTES_18 §7).

MCP's stdio transport: one JSON-RPC 2.0 message per line on stdin and stdout, UTF-8; nothing else
may be written to stdout (logs go to stderr). This serves the subset Mock Board needs:
`initialize`, `ping`, `tools/list`, `tools/call`, `prompts/list`, `prompts/get`, `resources/list`,
`resources/read`, `resources/templates/list`, and ignores notifications. Requests run
concurrently; a per-session lock in the service keeps one sitting's turns in order.

If the owner approves the official Python SDK (`mcp` on PyPI), it can serve the same table
(`tools.py`); this module stays as the no-dependency fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from mockboard.mcp import guidance
from mockboard.mcp.service import BoardService
from mockboard.mcp.tools import (
    PROMPTS,
    RESOURCES,
    TOOLS,
    UnknownName,
    call_tool,
    prompt_text,
    resource_text,
)

logger = logging.getLogger("mockboard.mcp")

SERVER_NAME = "mockboard"
SERVER_VERSION = "0.1.0"
# MCP protocol revisions this server answers to; the client's choice is echoed when listed, else
# the newest here. Only features common to all of them are used (tools, prompts, resources).
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = PROTOCOL_VERSIONS[0]

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = (
    -32700, -32600, -32601, -32602, -32603,
)


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StdioServer:
    def __init__(self, service: BoardService | None = None) -> None:
        self.service = service or BoardService()
        self.protocol_version = LATEST

    # -- one message ------------------------------------------------------------

    async def handle(self, message: Any) -> dict[str, Any] | None:
        """The response to one message, or None for a notification (or a stray response)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        has_id = "id" in message
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None  # a response; this server sends no requests, so there is none to match
            return _error(message.get("id"), INVALID_REQUEST, "missing method")
        if not has_id:  # notifications/initialized, notifications/cancelled, ...
            return None
        rid = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(rid, INVALID_PARAMS, "params must be an object")
        try:
            result = await self._dispatch(method, params)
        except RpcError as exc:
            return _error(rid, exc.code, exc.message)
        except Exception:  # never leak a traceback to the client; log it to stderr
            logger.exception("mcp: %s failed", method)
            return _error(rid, INTERNAL_ERROR, "internal error in Mock Board")
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.protocol_version = asked if asked in PROTOCOL_VERSIONS else LATEST
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                },
                "serverInfo": {"name": SERVER_NAME, "title": "Mock Board",
                               "version": SERVER_VERSION},
                "instructions": guidance.INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                raise RpcError(INVALID_PARAMS, "tools/call needs a name and an arguments object")
            try:
                result, is_error = await call_tool(self.service, name, args)
            except UnknownName:
                raise RpcError(INVALID_PARAMS, f"unknown tool: {name}") from None
            text = json.dumps(result, ensure_ascii=False, indent=1)
            out: dict[str, Any] = {"content": [{"type": "text", "text": text}],
                                   "isError": is_error}
            if not is_error:
                out["structuredContent"] = result
            return out
        if method == "prompts/list":
            return {"prompts": PROMPTS}
        if method == "prompts/get":
            name = params.get("name")
            try:
                text = prompt_text(name) if isinstance(name, str) else ""
            except UnknownName:
                text = ""
            if not text:
                raise RpcError(INVALID_PARAMS, f"unknown prompt: {name}")
            description = next(p["description"] for p in PROMPTS if p["name"] == name)
            return {"description": description,
                    "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if method == "resources/list":
            return {"resources": RESOURCES}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            uri = params.get("uri")
            try:
                text = resource_text(self.service, uri) if isinstance(uri, str) else ""
            except UnknownName:
                text = ""
            if not text:
                raise RpcError(INVALID_PARAMS, f"unknown resource: {uri}")
            mime = next(r["mimeType"] for r in RESOURCES if r["uri"] == uri)
            return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}
        raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    # -- the loop ---------------------------------------------------------------

    async def serve(
        self,
        read_line: Callable[[], Awaitable[bytes]],
        write: Callable[[bytes], None],
    ) -> None:
        """Read lines until EOF; answer each request as it finishes (requests may overlap)."""
        pending: set[asyncio.Task] = set()

        async def answer(message: Any) -> None:
            response = await self.handle(message)
            if response is not None:
                write(_encode(response))

        try:
            while True:
                line = await read_line()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    write(_encode(_error(None, PARSE_ERROR, "not valid JSON")))
                    continue
                task = asyncio.create_task(answer(message))
                pending.add(task)
                task.add_done_callback(pending.discard)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await self.service.aclose()


def _encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def run_stdio(make_server: Callable[[], StdioServer] = StdioServer) -> None:
    """Serve on this process's stdin/stdout. Anything printed by mistake goes to stderr."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print() must never corrupt the protocol stream

    def write(data: bytes) -> None:
        out.write(data)
        out.flush()

    async def read_line() -> bytes:
        return await asyncio.to_thread(sys.stdin.buffer.readline)

    async def main() -> None:
        server = make_server()
        start = getattr(server, "start", None)
        if start is not None:  # e.g. the probe's localhost echo endpoint
            await start()
        await server.serve(read_line, write)

    asyncio.run(main())
