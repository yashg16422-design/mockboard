"""`python -m mockboard.mcp.probe`: a free MCP App probe for Claude Desktop (Option 2 checkpoint).

It answers one question before we build the Gnani voice room as an MCP App: on the owner's Mac,
does Claude Desktop render an app view from a LOCAL stdio server, and may that view use the
microphone, reach our localhost server (fetch and WebSocket), and post a message into the chat?

* Tools: `mic_probe` (the model calls it; its view is `ui://mockboard-probe/probe.html`),
  `probe_report` (app-only: the view sends its readout), `probe_result` (the model reads the
  readout, plus what the server saw: the client, its ui extension, whether the view was fetched).
* A localhost echo endpoint on 127.0.0.1 (first free port from 8790): `GET /ping` answers "pong",
  `/ws` echoes "ping" with "pong" over a WebSocket. Nothing else; nothing is stored.
* No Gnani, no Bedrock, no outside host. The view stops the microphone stream at once.

Unknowns (CTO_NOTES_18 addendum): the server-side shape of the ui extension declaration is not in
the verbatim spec text we read (`# UNVERIFIED`); we send the documented `mimeTypes` list.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
from typing import Any

from mockboard.mcp.probe_page import PAGE
from mockboard.mcp.stdio import RpcError, StdioServer, run_stdio

logger = logging.getLogger("mockboard.mcp.probe")

VERSION = "v1"
UI_EXTENSION = "io.modelcontextprotocol/ui"
APP_MIME = "text/html;profile=mcp-app"
VIEW_URI = "ui://mockboard-probe/probe.html"
FIRST_PORT = 8790
PORT_TRIES = 10
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455's fixed key suffix
MAX_REPORT_CHARS = 4000


def view_meta(port: int) -> dict[str, Any]:
    origins = [f"{scheme}://{host}:{port}" for scheme in ("http", "ws")
               for host in ("127.0.0.1", "localhost")]
    return {"ui": {"csp": {"connectDomains": origins}, "permissions": {"microphone": {}},
                   "prefersBorder": True}}


def probe_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "mic_probe",
            "title": "Mock Board probe",
            "description": (
                "Show Mock Board's probe panel. It checks whether this app can show an "
                "interactive view, use the microphone, reach Mock Board's local server, and post "
                "a message to the chat. Free; nothing is recorded."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            # The current key, and the deprecated flat key for hosts that still read it.
            "_meta": {"ui": {"resourceUri": VIEW_URI}, "ui/resourceUri": VIEW_URI},
        },
        {
            "name": "probe_report",
            "title": "Probe report (from the panel)",
            "description": "Used by the probe panel only: stores its readout.",
            "inputSchema": {
                "type": "object",
                "properties": {"readout": {"type": "string", "maxLength": MAX_REPORT_CHARS}},
                "required": ["readout"],
                "additionalProperties": False,
            },
            "_meta": {"ui": {"visibility": ["app"]}},
        },
        {
            "name": "probe_result",
            "title": "Probe result",
            "description": (
                "Read the probe's result: what the panel found and what the server saw. Call it "
                "after mic_probe, and show the text to the person exactly."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


class ProbeServer(StdioServer):
    def __init__(self, port: int | None = None) -> None:
        super().__init__()
        self.port = port or 0
        self.client: dict[str, Any] = {}
        self.client_ui: Any = None
        self.view_reads = 0
        self.http_hits = 0
        self.ws_hits = 0
        self.report = ""
        self._echo: asyncio.base_events.Server | None = None

    # -- the localhost echo endpoint ----------------------------------------------

    async def start(self) -> None:
        want = self.port or int(os.environ.get("MOCKBOARD_PROBE_PORT", FIRST_PORT))
        for port in range(want, want + PORT_TRIES):
            try:
                self._echo = await asyncio.start_server(self._serve_http, "127.0.0.1", port)
            except OSError:
                continue
            self.port = self._echo.sockets[0].getsockname()[1]
            logger.warning("probe: echo endpoint on http://127.0.0.1:%s", self.port)
            return
        logger.warning("probe: no free port from %s; fetch and WebSocket will show blocked", want)

    async def aclose(self) -> None:
        if self._echo is not None:
            self._echo.close()
            await self._echo.wait_closed()
            self._echo = None

    async def _serve_http(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = head.decode("latin-1").split("\r\n")
            method, path = [*lines[0].split(" "), "", ""][:2]
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (line.partition(":") for line in lines[1:] if ":" in line)}
            cors = ("Access-Control-Allow-Origin: *\r\n"
                    "Access-Control-Allow-Private-Network: true\r\n")
            if method == "OPTIONS":
                writer.write(("HTTP/1.1 204 No Content\r\n" + cors +
                              "Access-Control-Allow-Methods: GET\r\n"
                              "Access-Control-Allow-Headers: *\r\nContent-Length: 0\r\n\r\n"
                              ).encode())
            elif path == "/ping" and method == "GET":
                self.http_hits += 1
                writer.write(("HTTP/1.1 200 OK\r\n" + cors + "Content-Type: text/plain\r\n"
                              "Cache-Control: no-store\r\nContent-Length: 4\r\n\r\npong").encode())
            elif path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
                await self._websocket(reader, writer, headers.get("sec-websocket-key", ""))
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    async def _websocket(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         key: str) -> None:
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        text = await asyncio.wait_for(read_text_frame(reader), 5)
        self.ws_hits += 1
        if text == "ping":
            writer.write(text_frame("pong"))
        writer.write(b"\x88\x00")  # close
        await writer.drain()

    # -- MCP ------------------------------------------------------------------------

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            info = params.get("clientInfo")
            self.client = info if isinstance(info, dict) else {}
            caps = params.get("capabilities")
            ext = caps.get("extensions") if isinstance(caps, dict) else None
            self.client_ui = ext.get(UI_EXTENSION) if isinstance(ext, dict) else None
            result = await super()._dispatch(method, params)
            result["serverInfo"] = {"name": "mockboard-probe", "title": "Mock Board probe",
                                    "version": VERSION}
            result["capabilities"] = {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                # UNVERIFIED shape: the spec text we read gives the client's form (mimeTypes).
                "extensions": {UI_EXTENSION: {"mimeTypes": [APP_MIME]}},
            }
            result["instructions"] = "Call mic_probe, then probe_result, and show the result."
            return result
        if method == "tools/list":
            return {"tools": probe_tools()}
        if method == "tools/call":
            return self._call(params)
        if method == "resources/list":
            return {"resources": [{"uri": VIEW_URI, "name": "probe", "title": "Mock Board probe",
                                   "mimeType": APP_MIME, "_meta": view_meta(self.port)}]}
        if method == "resources/read":
            if params.get("uri") != VIEW_URI:
                raise RpcError(-32602, f"unknown resource: {params.get('uri')}")
            self.view_reads += 1
            html = PAGE.replace("__PORT__", str(self.port)).replace("__VERSION__", VERSION)
            return {"contents": [{"uri": VIEW_URI, "mimeType": APP_MIME, "text": html,
                                  "_meta": view_meta(self.port)}]}
        if method == "prompts/list":
            return {"prompts": []}
        return await super()._dispatch(method, params)

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "mic_probe":
            text = (
                "The Mock Board probe panel should appear above. It runs by itself in a few "
                "seconds and posts its readout into the chat. If no panel appears, the view did "
                "not render: call probe_result to see what the server saw."
            )
            return {"content": [{"type": "text", "text": text}],
                    "structuredContent": {"port": self.port, "version": VERSION}}
        if name == "probe_report":
            readout = args.get("readout") if isinstance(args, dict) else None
            if not isinstance(readout, str):
                raise RpcError(-32602, "probe_report needs a readout")
            self.report = readout[:MAX_REPORT_CHARS]
            logger.warning("probe: view report\n%s", self.report)
            return {"content": [{"type": "text", "text": "stored"}]}
        if name == "probe_result":
            return {"content": [{"type": "text", "text": self.result_text()}]}
        raise RpcError(-32602, f"unknown tool: {name}")

    def result_text(self) -> str:
        client = f"{self.client.get('name', '?')} {self.client.get('version', '')}".strip()
        ui = ("yes " + json.dumps(self.client_ui, ensure_ascii=False)[:200]
              if self.client_ui is not None else "no")
        lines = [
            f"MOCK BOARD PROBE {VERSION} (server side)",
            f"client (MCP initialize): {client}",
            f"client declares the ui extension: {ui}",
            f"view resource fetched by the host: {self.view_reads} time(s)",
            f"localhost hits seen by the server: fetch {self.http_hits}, websocket {self.ws_hits}",
            f"echo endpoint: http://127.0.0.1:{self.port}" if self.port else
            "echo endpoint: not running",
        ]
        if self.report:
            lines += ["", "panel readout:", self.report]
        elif self.view_reads:
            lines += ["", "panel readout: none yet (fetched but no report: the view may not "
                          "have started, or tools/call from the view is blocked)"]
        else:
            lines += ["", "panel readout: none (the host never fetched the view: it did not "
                          "render; see docs/MCP_PROBE.md, issue #1069)"]
        return "\n".join(lines)


# --- WebSocket frames (RFC 6455), just enough for one short text message ------------------------


async def read_text_frame(reader: asyncio.StreamReader) -> str:
    b0, b1 = await reader.readexactly(2)
    if b0 & 0x0F not in (0x1, 0x0):
        return ""
    length = b1 & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        raise ValueError("frame too large for the probe")
    if length > 4096:
        raise ValueError("frame too large for the probe")
    mask = await reader.readexactly(4) if b1 & 0x80 else b"\0\0\0\0"
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(length)))
    return data.decode("utf-8", "replace")


def text_frame(text: str) -> bytes:
    data = text.encode()
    if len(data) > 125:
        raise ValueError("the probe only sends short frames")
    return bytes([0x81, len(data)]) + data


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    run_stdio(ProbeServer)


if __name__ == "__main__":
    main()
